"""Offline tests of the GraphQL discovery cycle in `AlertService`.

The feed, the HTML provider and Telegram are scripted; no test touches the
network. Together they pin the contract the integration must satisfy:

* one feed fetch per cycle, shared by every active alert;
* only threads never seen before are evaluated;
* an alert never announces a deal published before the alert was created;
* a delivery that fails stays pending and is retried, even after the deal has
  left the provider window.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.errors import ChollometroHTTPError
from chollometro_alerts.graphql_feed import FeedBatch
from chollometro_alerts.models import Deal
from chollometro_alerts.product import extract_product
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.runtime import run_daemon
from chollometro_alerts.service import (
    FEED_FALLBACK,
    FEED_QUERY_LABEL,
    FEED_SKIPPED,
    AlertService,
)
from chollometro_alerts.telegram import format_message

# A fixed "now" keeps the alert window deterministic: no test depends on when
# the suite runs.
T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
AFTER = T0 + timedelta(minutes=1)
BEFORE = T0 - timedelta(minutes=1)
# The production request carries no `limit` argument, so the endpoint answers
# with its own window: 30 threads. The scripted feed mirrors that default.
WINDOW_SIZE = 30


class FakeFeed:
    """Scripted discovery feed that counts the fetches of the cycle."""

    def __init__(self, *batches, window_limit=None):
        self.batches = list(batches)
        self.window_limit = window_limit
        self.calls = 0
        self.last_feed = None
        self.last_http_status = 200

    def latest(self, limit=None):
        self.calls += 1
        if not self.batches:
            raise AssertionError("feed script exhausted")
        result = self.batches.pop(0)
        if isinstance(result, Exception):
            raise result
        self.last_feed = FeedBatch(
            deals=tuple(result),
            window_limit=self.window_limit,
            xsrf_present=True,
            fetched_at=datetime.now(UTC),
        )
        return list(result)


class HtmlClient:
    """The untouched HTML provider, scripted."""

    def __init__(self, deals=()):
        self.deals = list(deals)
        self.calls = []
        self.last_scan = None
        self.last_search = {}

    def recent(self, queries, pages):
        self.calls.append((tuple(queries), pages))
        return list(self.deals)


class RecordingNotifier:
    dry_run = False

    def __init__(self, failing_sends=0):
        self.sent = []
        self.evidences = []
        self.alerts = []
        self.failing_sends = failing_sends

    def send(self, deal, evidence=None):
        if self.failing_sends > 0:
            self.failing_sends -= 1
            raise RuntimeError("telegram down")
        self.sent.append(deal)
        self.evidences.append(evidence)

    def send_system_alert(self, error_type, component, message, run_id):
        self.alerts.append((error_type, component, message))

    @property
    def sent_ids(self):
        return [deal.deal_id for deal in self.sent]

    @property
    def messages(self):
        """Exactly what Telegram would have received, in order."""
        return [format_message(deal) for deal in self.sent]


class CountingExtractor:
    """Deterministic stand-in that records every LLM call a cycle would spend."""

    def __init__(self):
        self.calls = 0

    def __call__(self, product_text, deal_id=None):
        self.calls += 1
        return extract_product(product_text)


def make_deal(deal_id, published_at, price="10.00", title=None):
    return Deal(
        deal_id,
        title or f"Producto {deal_id}",
        f"https://www.chollometro.com/ofertas/producto-{deal_id}",
        Decimal(price),
        "Amazon",
        120,
        "generic",
        published_at,
        product_text=title or f"Producto {deal_id}",
    )


def historical(deal_id, *, published_at=BEFORE, price="10.00", title=None):
    """A deal published before the alert: baseline material, never an alert.

    The first cycle still records it as seen, but `published_at > created_at`
    is false, so no alert created afterwards may ever announce it.
    """
    return make_deal(deal_id, published_at, price=price, title=title)


def make_service(tmp_path, feed, html=None, notifier=None, extractor=None):
    repository = DealRepository(tmp_path / "discovery.db")
    notifier = notifier if notifier is not None else RecordingNotifier()
    extractor = extractor if extractor is not None else CountingExtractor()
    service = AlertService(
        html if html is not None else HtmlClient(),
        repository,
        notifier,
        extractor,
        feed=feed,
    )
    return service, repository, notifier, extractor


def add_rule(repository, query="leche", created_at=T0, constraints=None, product=None):
    """Persist an alert and pin its real creation timestamp."""
    rule_id = repository.save_alert_rule(
        AlertRule(
            query=query,
            product=product,
            constraints=constraints or AlertConstraints(),
        ),
        query,
    )
    repository.db.execute(
        "UPDATE alert_rules SET created_at=? WHERE id=?",
        (created_at.isoformat(), rule_id),
    )
    repository.db.commit()
    return rule_id


def feed_scan_runs(repository):
    columns = [row[1] for row in repository.db.execute("PRAGMA table_info(scan_runs)")]
    return [
        dict(zip(columns, row))
        for row in repository.db.execute(
            "SELECT * FROM scan_runs WHERE query=? ORDER BY rowid", (FEED_QUERY_LABEL,)
        )
    ]


# --- Baseline and steady state ------------------------------------------- #


def test_the_first_cycle_is_a_reference_for_deals_older_than_the_alert(tmp_path):
    """A B C predate the alert: the first cycle records them and stays silent."""
    feed = FakeFeed(
        [
            historical("A", published_at=T0 - timedelta(minutes=10)),
            historical("B", published_at=BEFORE),
            historical("C", published_at=T0),
        ]
    )
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert repository.seen_feed_thread_ids() == {"A", "B", "C"}
    assert repository.feed_is_initialized() is True
    assert service.last_feed_received == 3
    # Every thread of the window is seen for the first time.
    assert service.last_feed_new == 3
    assert service.last_feed_status == "OK"
    assert service.last_summary.classified == 0
    runs = feed_scan_runs(repository)
    assert len(runs) == 1
    assert (runs[0]["status"], runs[0]["fetched_items"], runs[0]["new_items"]) == (
        "SUCCESS",
        3,
        3,
    )
    assert runs[0]["notifications_sent"] == 0


def test_the_first_cycle_records_the_full_thirty_thread_baseline(tmp_path):
    """The widest window the endpoint gives: 30 threads, all of them historical."""
    window = [historical(str(2012000 + index)) for index in range(WINDOW_SIZE)]
    feed = FakeFeed(list(window))
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert service.last_feed_received == WINDOW_SIZE
    assert service.last_feed_new == WINDOW_SIZE
    assert len(repository.seen_feed_thread_ids()) == WINDOW_SIZE
    assert repository.feed_is_initialized() is True
    runs = feed_scan_runs(repository)
    assert runs[0]["fetched_items"] == WINDOW_SIZE
    assert runs[0]["new_items"] == WINDOW_SIZE
    assert runs[0]["notifications_sent"] == 0


def test_a_repeated_thread_id_is_evaluated_and_notified_only_once(tmp_path):
    baseline = [historical("A")]
    deal_d = make_deal("D", AFTER + timedelta(minutes=5))
    feed = FakeFeed(list(baseline), [deal_d, deal_d, *baseline])
    service, repository, notifier, extractor = make_service(tmp_path, feed)
    add_rule(repository, query="leche")

    service.run_active_rules()  # baseline: A predates the alert
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == ["D"]
    assert service.last_feed_new == 1
    assert service.last_summary.found == 2
    assert extractor.calls == 1
    assert repository.pending_rule_notifications() == []


def test_second_run_without_changes_notifies_nothing(tmp_path):
    deals = [historical("A"), historical("B"), historical("C")]
    feed = FakeFeed(list(deals), list(deals))
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    service.run_active_rules()
    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert service.last_feed_new == 0
    assert feed.calls == 2


def test_a_new_matching_deal_is_notified_once(tmp_path):
    # A B C predate the alert; D is published after it and must be announced.
    baseline = [historical("A"), historical("B"), historical("C")]
    deal_d = make_deal("D", AFTER + timedelta(minutes=5))
    feed = FakeFeed(list(baseline), [deal_d, *baseline], [deal_d, *baseline])
    service, repository, notifier, extractor = make_service(tmp_path, feed)
    add_rule(repository, query="leche")

    service.run_active_rules()  # baseline
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == ["D"]
    assert service.last_feed_new == 1
    assert service.last_summary.found == 4
    assert service.last_summary.telegram_sent == 1

    # Next cycle: D is still in the window and must not be announced again.
    assert service.run_active_rules() == 0
    assert notifier.sent_ids == ["D"]
    assert service.last_feed_new == 0
    assert extractor.calls == 1


def test_a_deal_published_before_the_alert_is_never_notified(tmp_path):
    feed = FakeFeed([historical("A")], [make_deal("Z", BEFORE), historical("A")])
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    service.run_active_rules()  # baseline

    assert service.run_active_rules() == 0
    assert notifier.sent == []
    # It was new for the seen store, but not new for the alert.
    assert service.last_feed_new == 1
    assert service.last_summary.before_alert == 1
    assert repository.seen_feed_thread_ids() == {"A", "Z"}


def test_the_window_is_logged_on_every_cycle(tmp_path, caplog):
    deals = [historical("A"), historical("B")]
    feed = FakeFeed(list(deals), list(deals))
    service, repository, _notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    with caplog.at_level(logging.INFO):
        service.run_active_rules()
        service.run_active_rules()

    windows = [
        r.message for r in caplog.records if r.message.startswith("feed_window ")
    ]
    assert len(windows) == 2
    assert "received=2" in windows[0]
    # The first cycle sees the whole window for the first time, the second none.
    assert "new=2" in windows[0]
    assert "new=0" in windows[1]
    assert "oldest_age_seconds=" in windows[0]
    assert service.last_feed_oldest_age_seconds is not None


def test_a_saturated_default_window_is_not_a_risk_by_itself(tmp_path, caplog):
    """The endpoint always fills its 30-thread window: that alone proves nothing."""
    window = [historical(str(index)) for index in range(WINDOW_SIZE)]
    feed = FakeFeed(list(window), list(window))
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    # INFO level: the saturated-window notice is informational, not a risk.
    with caplog.at_level(logging.INFO):
        service.run_active_rules()
        service.run_active_rules()

    risks = [
        r.message for r in caplog.records if r.message.startswith("feed_window_risk")
    ]
    assert risks == []
    saturated = [
        r.message
        for r in caplog.records
        if r.message.startswith("feed_window_saturated")
    ]
    assert len(saturated) == 2
    assert f"window={WINDOW_SIZE}" in saturated[0]
    assert service.last_feed_received == WINDOW_SIZE
    # The second window is the same as the recorded baseline: full overlap.
    assert service.last_feed_overlap == WINDOW_SIZE
    assert service.last_feed_new == 0
    assert notifier.sent == []
    # A full *default* window is not a full explicit window.
    assert service.last_feed_window_full is False


def test_a_window_without_overlap_is_the_risk_signal(tmp_path, caplog):
    """Zero overlap with a non-empty history means the tail was missed."""
    first = [historical(f"old-{index}") for index in range(3)]
    second = [
        make_deal(f"new-{index}", AFTER + timedelta(minutes=45)) for index in range(3)
    ]
    feed = FakeFeed(first, second)
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    service.run_active_rules()  # baseline: nothing is a risk yet
    with caplog.at_level(logging.WARNING):
        service.run_active_rules()

    risks = [
        r.message for r in caplog.records if r.message.startswith("feed_window_risk")
    ]
    assert len(risks) == 1
    assert "reason=no_overlap" in risks[0]
    assert "overlap=0" in risks[0]
    assert "received=3" in risks[0]
    assert service.last_feed_overlap == 0
    # The three new threads are evaluated and matched by the plain test rule.
    assert notifier.sent_ids == ["new-0", "new-1", "new-2"]


def test_a_new_thread_arriving_inside_the_window_is_not_a_risk(tmp_path, caplog):
    """Partial overlap is the healthy steady state: new threads plus known ones."""
    baseline = [historical(f"known-{index}") for index in range(3)]
    fresh = make_deal("fresh", AFTER + timedelta(minutes=5))
    feed = FakeFeed(baseline, [fresh, *baseline])
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository, query="leche")

    service.run_active_rules()
    with caplog.at_level(logging.WARNING):
        assert service.run_active_rules() == 1

    risks = [
        r.message for r in caplog.records if r.message.startswith("feed_window_risk")
    ]
    assert risks == []
    assert service.last_feed_overlap == 3
    assert notifier.sent_ids == ["fresh"]


def test_a_gap_between_cycles_is_reported(tmp_path, caplog):
    """The oldest thread of a cycle is newer than the previous watermark."""
    first = [make_deal("A", AFTER)]
    second = [make_deal("B", AFTER + timedelta(hours=2))]
    feed = FakeFeed(first, second)
    service, repository, _notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    service.run_active_rules()
    with caplog.at_level(logging.WARNING):
        service.run_active_rules()

    gaps = [
        r.message for r in caplog.records if r.message.startswith("feed_window_gap")
    ]
    assert len(gaps) == 1
    assert "gap_seconds=7200" in gaps[0]


def test_no_active_alert_skips_the_feed_entirely(tmp_path):
    feed = FakeFeed([make_deal("A", AFTER)])
    service, repository, _notifier, _extractor = make_service(tmp_path, feed)

    assert service.run_active_rules() == 0

    assert feed.calls == 0
    assert service.last_feed_status == FEED_SKIPPED
    assert repository.feed_is_initialized() is False


def test_an_empty_feed_is_a_valid_answer_not_a_failure(tmp_path):
    """`{"data": {"threads": []}}` is an explicit empty answer, not an outage."""
    feed = FakeFeed([])
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    assert service.run_active_rules() == 0

    assert service.last_feed_status == "OK"
    assert service.last_feed_received == 0
    assert service.last_feed_oldest_age_seconds is None
    assert notifier.sent == [] and notifier.alerts == []
    assert repository.seen_feed_thread_ids() == set()
    assert repository.feed_is_initialized() is True
    assert feed_scan_runs(repository)[0]["status"] == "SUCCESS"


# --- Several alerts, one single fetch ------------------------------------ #


def test_one_fetch_per_cycle_evaluates_every_active_alert(tmp_path):
    baseline = [historical("A")]
    deal_d = make_deal("D", AFTER + timedelta(minutes=5))
    feed = FakeFeed(list(baseline), [deal_d, *baseline])
    html = HtmlClient([make_deal("html-1", AFTER)])
    service, repository, notifier, _extractor = make_service(tmp_path, feed, html=html)
    for query in ("leche", "cerveza", "zapatillas"):
        add_rule(repository, query=query)

    service.run_active_rules()  # baseline
    assert service.run_active_rules() == 3

    # One fetch for three alerts: never one GraphQL request per alert.
    assert feed.calls == 2
    assert html.calls == []
    assert sorted(notifier.sent_ids) == ["D", "D", "D"]
    assert service.last_summary.telegram_sent == 3


def test_each_alert_only_sees_deals_newer_than_itself(tmp_path):
    """A deal between two alert creations belongs to the newer alert only."""
    published_between = make_deal("D", T0 + timedelta(minutes=2))
    feed = FakeFeed([historical("A")], [published_between])
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    older_alert = add_rule(repository, query="leche", created_at=T0)
    newer_alert = add_rule(
        repository, query="cerveza", created_at=T0 + timedelta(minutes=5)
    )

    service.run_active_rules()  # baseline
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == ["D"]
    assert repository.get_rule_observation(older_alert, "D") is not None
    # The newer alert is younger than the deal, so it never evaluates it.
    assert repository.get_rule_observation(newer_alert, "D") is None
    assert repository.pending_rule_notifications() == []


def test_a_rejected_deal_is_recorded_and_not_re_evaluated(tmp_path):
    baseline = [historical("A")]
    expensive = make_deal("D", AFTER + timedelta(minutes=1), price="10.00")
    feed = FakeFeed(list(baseline), [expensive, *baseline], [expensive, *baseline])
    service, repository, notifier, extractor = make_service(tmp_path, feed)
    rule_id = add_rule(
        repository, constraints=AlertConstraints(max_price=Decimal("1.00"))
    )

    service.run_active_rules()
    assert service.run_active_rules() == 0

    observation = repository.get_rule_observation(rule_id, "D")
    assert observation[5] == 0 and observation[6] == "REJECTED_PRICE"
    assert service.last_summary.rejected == 1

    assert service.run_active_rules() == 0
    assert service.last_summary.classified == 0
    assert notifier.sent == [] and extractor.calls == 1


# --- Controlled degradation --------------------------------------------- #


def test_graphql_failure_falls_back_to_the_html_scans(tmp_path):
    feed = FakeFeed(ChollometroHTTPError("boom", status_code=503))
    html_deal = make_deal("html-1", AFTER)
    html = HtmlClient([html_deal])
    service, repository, notifier, _extractor = make_service(tmp_path, feed, html=html)
    add_rule(repository)

    assert service.run_active_rules() == 1

    assert notifier.sent_ids == ["html-1"]
    assert html.calls and html.calls[0] == (("leche",), 1)
    assert service.last_feed_status == FEED_FALLBACK
    assert service.last_feed_error_type == "HTTP_503"
    assert [alert[0] for alert in notifier.alerts] == ["HTTP_503"]
    # The fallback never touches the discovery state.
    assert repository.seen_feed_thread_ids() == set()
    assert repository.feed_is_initialized() is False
    assert feed_scan_runs(repository)[0]["status"] == "FAILED"


def test_a_telegram_failure_is_retried_from_the_pending_state(tmp_path):
    baseline = [historical("A")]
    deal_d = make_deal("D", AFTER + timedelta(minutes=1))
    feed = FakeFeed(list(baseline), [deal_d, *baseline], list(baseline))
    notifier = RecordingNotifier(failing_sends=1)
    service, repository, _notifier, _extractor = make_service(
        tmp_path, feed, notifier=notifier
    )
    rule_id = add_rule(repository)

    service.run_active_rules()  # baseline
    # The delivery fails: the match is durable, the notification is not.
    assert service.run_active_rules() == 0
    assert notifier.sent == []
    assert service.last_summary.errors == 1
    assert repository.pending_rule_notifications() == [(rule_id, "D")]
    assert [alert[0] for alert in notifier.alerts] == ["TELEGRAM_ERROR"]
    assert "D" in repository.seen_feed_thread_ids()

    # Next cycle D already left the window: the pending alert is still sent.
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == ["D"]
    assert repository.pending_rule_notifications() == []
    assert service.last_summary.telegram_sent == 1


def test_a_rule_baseline_never_hides_a_newer_deal(tmp_path):
    """The baseline snapshot covers the past, not a deal newer than the alert."""
    deal_x = make_deal("X", AFTER)
    feed = FakeFeed([historical("A")], [deal_x, historical("A")])
    html = HtmlClient([deal_x])
    service, repository, notifier, _extractor = make_service(tmp_path, feed, html=html)
    rule_id = add_rule(repository)
    # Creating an alert takes a baseline with the HTML provider: the same deal
    # is claimed as a reference, but it was published after the alert.
    assert service.baseline_rule(rule_id, "leche") == 1

    service.run_active_rules()  # bootstrap: records the current feed only
    assert notifier.sent == []

    assert service.run_active_rules() == 1
    assert notifier.sent_ids == ["X"]
    observation = repository.get_rule_observation(rule_id, "X")
    assert observation[5] == 1 and observation[4] == 0
    assert repository.pending_rule_notifications() == []


def test_an_interrupted_evaluation_is_evaluated_again(tmp_path):
    """A claim without a verdict (a crash mid-cycle) is retried, not dropped."""
    deal_d = make_deal("D", AFTER)
    feed = FakeFeed([historical("A")], [deal_d])
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    rule_id = add_rule(repository)
    service.run_active_rules()  # bootstrap
    # Simulate a cycle that claimed the pair and died before evaluating it.
    assert repository.claim_rule_observation(rule_id, "D") is True

    assert service.run_active_rules() == 1

    assert notifier.sent_ids == ["D"]
    assert repository.get_rule_observation(rule_id, "D")[5] == 1


def test_a_failed_cycle_does_not_resend_an_already_delivered_alert(tmp_path):
    baseline = [historical("A")]
    deal_d = make_deal("D", AFTER + timedelta(minutes=1))
    feed = FakeFeed(list(baseline), [deal_d, *baseline], [deal_d, *baseline])
    service, repository, notifier, _extractor = make_service(tmp_path, feed)
    add_rule(repository)

    service.run_active_rules()
    assert service.run_active_rules() == 1

    assert service.run_active_rules() == 0
    assert notifier.sent_ids == ["D"]
    assert repository.pending_rule_notifications() == []


# --- Daemon wiring ------------------------------------------------------- #


class NoWaitEvent(threading.Event):
    """Timed waits return immediately: the daemon test never sleeps."""

    def wait(self, timeout=None):
        if self.is_set():
            return True
        super().wait(0)
        return False


class StubController:
    def __init__(self, repository):
        self.repository = repository

    def listen_forever(self, stop_event=None):
        if stop_event is not None:
            stop_event.wait()


class CyclingFeedService(AlertService):
    """Stops the daemon after a fixed number of discovery cycles."""

    def __init__(self, *args, stop_event, cycles, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_event = stop_event
        self.cycles = cycles
        self.scans = 0

    def run_active_rules(self, pages=1):
        self.scans += 1
        result = super().run_active_rules(pages=pages)
        if self.scans >= self.cycles:
            self.stop_event.set()
        return result


def test_the_daemon_runs_exactly_one_feed_fetch_per_cycle(tmp_path):
    baseline = [make_deal("A", AFTER)]
    feed = FakeFeed(list(baseline), list(baseline), list(baseline))
    repository = DealRepository(tmp_path / "daemon.db")
    add_rule(repository)
    stop = NoWaitEvent()
    service = CyclingFeedService(
        HtmlClient(),
        repository,
        RecordingNotifier(),
        CountingExtractor(),
        stop_event=stop,
        cycles=3,
        feed=feed,
    )

    run_daemon(
        StubController(repository),
        service,
        interval_minutes=1,
        pages=1,
        stop_event=stop,
    )

    assert service.scans == 3
    assert feed.calls == 3
    assert len(feed_scan_runs(repository)) == 3
