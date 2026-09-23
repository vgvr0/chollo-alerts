"""The notification window decides *when* Telegram is sent, never what matches.

A deal that matches outside the window is not lost: the match is persisted and
left pending (`NOTIFICATION_SCHEDULE`) until a cycle runs inside the window.
The window is read in the alert's own timezone, so it survives DST changes.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from chollometro_alerts.alert_rule import (
    AlertConstraints,
    AlertRule,
)
from chollometro_alerts.alert_rule import (
    NotificationWindow as RuleWindow,
)
from chollometro_alerts.graphql_feed import FeedBatch
from chollometro_alerts.models import Deal
from chollometro_alerts.product import extract_product
from chollometro_alerts.repository import (
    PENDING_NOTIFICATION_SCHEDULE,
    PENDING_TELEGRAM_FAILURE,
    DealRepository,
)
from chollometro_alerts.schedule import NotificationWindow
from chollometro_alerts.service import AlertService

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
DEAL_ID = "2012039"

# Windows used by the tests, in the default timezone of the product.
DAY = RuleWindow(start=time(8, 0), end=time(23, 0), timezone="Europe/Madrid")
NIGHT = RuleWindow(start=time(22, 0), end=time(7, 0), timezone="Europe/Madrid")


class Feed:
    """Scripted discovery windows: the same list every cycle unless scripted."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.last_feed = None
        self.last_http_status = 200
        self.calls = 0

    def latest(self, limit=None):
        self.calls += 1
        deals = self.batches.pop(0) if self.batches else []
        self.last_feed = FeedBatch(
            deals=tuple(deals),
            window_limit=None,
            xsrf_present=True,
            fetched_at=datetime.now(UTC),
        )
        return list(deals)


class HtmlClient:
    """The HTML provider, scripted (used by the fallback and by `run` mode)."""

    def __init__(self, deals=()):
        self.deals = list(deals)
        self.calls = []
        self.last_scan = None
        self.last_search = {}

    def recent(self, queries, pages):
        self.calls.append((tuple(queries), pages))
        return list(self.deals)


class Notifier:
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

    def send_system_alert(self, error_type, component, message, run_id=None):
        self.alerts.append((error_type, component, message))

    @property
    def sent_ids(self):
        return [deal.deal_id for deal in self.sent]


class Extractor:
    def __init__(self):
        self.calls = 0

    def __call__(self, product_text, deal_id=None):
        self.calls += 1
        return extract_product(product_text)


def make_deal(
    deal_id=DEAL_ID,
    published_at=T0 + timedelta(minutes=5),
    merchant="Amazon",
    price="899",
    title="Portátil gaming ASUS TUF",
):
    return Deal(
        deal_id,
        title,
        f"https://www.chollometro.com/ofertas/{deal_id}",
        Decimal(price),
        merchant,
        350,
        "generic",
        published_at,
        product_text=title,
    )


def make_service(tmp_path, feed=None, notifier=None, html=None, name="schedule.db"):
    repository = DealRepository(tmp_path / name)
    notifier = notifier if notifier is not None else Notifier()
    moment = [T0]
    service = AlertService(
        html if html is not None else HtmlClient(),
        repository,
        notifier,
        Extractor(),
        feed=feed,
        clock=lambda: moment[0],
    )
    return service, repository, notifier, moment


def add_rule(
    repository,
    *,
    query="portátil gaming",
    window=None,
    include=(),
    exclude=(),
    created_at=T0 - timedelta(hours=1),
    max_price=Decimal(1000),
):
    rule_id = repository.save_alert_rule(
        AlertRule(
            query=query,
            include_merchants=tuple(include),
            exclude_merchants=tuple(exclude),
            notification_window=window,
            constraints=AlertConstraints(max_price=max_price),
        ),
        query,
    )
    repository.db.execute(
        "UPDATE alert_rules SET created_at=? WHERE id=?",
        (created_at.isoformat(), rule_id),
    )
    repository.db.commit()
    return rule_id


def pending_rows(repository):
    return repository.pending_notification_rows()


# --- Without a window: the previous behaviour ------------------------------- #


def test_without_a_window_a_night_match_is_notified_immediately(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    add_rule(repository)
    moment[0] = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)  # 03:00 in Madrid

    service.run_active_rules()  # bootstrap
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == [DEAL_ID]
    assert pending_rows(repository) == []
    assert service.last_summary.deferred == 0


def test_a_legacy_rule_without_a_window_keeps_notifying(tmp_path):
    """A row persisted before the windows existed must not change behaviour."""
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    created_at = (T0 - timedelta(hours=1)).isoformat()
    repository.db.execute(
        """INSERT INTO alert_rules
        (query,product_type,brand,max_price,price_unit,enabled,state,created_at,
        updated_at) VALUES ('portátil gaming',NULL,NULL,'1000',
        'absolute',1,'ACTIVE',?,?)""",
        (created_at, created_at),
    )
    repository.db.commit()
    moment[0] = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)

    service.run_active_rules()  # bootstrap
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == [DEAL_ID]
    assert pending_rows(repository) == []


# --- Normal windows --------------------------------------------------------- #


def test_a_deal_inside_the_window_is_notified(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    add_rule(repository, window=DAY)
    moment[0] = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)  # 12:00 in Madrid

    service.run_active_rules()
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == [DEAL_ID]
    assert pending_rows(repository) == []
    assert service.last_summary.deferred == 0


def test_a_deal_outside_the_window_is_persisted_as_pending(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    rule_id = add_rule(repository, window=DAY)
    moment[0] = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)  # 03:00 in Madrid

    service.run_active_rules()
    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert service.last_summary.deferred == 1
    assert pending_rows(repository) == [
        (rule_id, DEAL_ID, PENDING_NOTIFICATION_SCHEDULE)
    ]
    observation = repository.get_rule_observation(rule_id, DEAL_ID)
    assert observation[5] == 1 and observation[7] is None
    # The chollo is not lost: the deal is known and the match is durable.
    assert DEAL_ID in repository.seen_feed_thread_ids()
    assert repository.get_deal(DEAL_ID) is not None


def test_a_pending_match_is_delivered_when_the_window_opens(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    rule_id = add_rule(repository, window=DAY)
    moment[0] = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)  # 03:00 in Madrid
    service.run_active_rules()
    service.run_active_rules()
    assert notifier.sent == []

    moment[0] = datetime(2026, 9, 22, 6, 30, tzinfo=UTC)  # 08:30 in Madrid
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == [DEAL_ID]
    assert pending_rows(repository) == []
    assert repository.get_rule_observation(rule_id, DEAL_ID)[7] is not None


# --- Compatibility with databases and rules created before this change ------ #


def test_an_existing_database_gains_the_pending_reason_column(tmp_path):
    """A database created before the windows existed is migrated in place."""
    import sqlite3

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE rule_deal_observations (
        rule_id INTEGER NOT NULL, deal_id TEXT NOT NULL,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        baseline INTEGER NOT NULL DEFAULT 0, matched INTEGER,
        rejection_reason TEXT, notified_at TEXT, evidence TEXT,
        PRIMARY KEY (rule_id, deal_id))"""
    )
    connection.execute(
        "INSERT INTO rule_deal_observations VALUES (1,'d','x','x',1,1,NULL,NULL,NULL)"
    )
    connection.commit()
    connection.close()

    repository = DealRepository(path)

    columns = {
        row[1]
        for row in repository.db.execute("PRAGMA table_info(rule_deal_observations)")
    }
    assert "pending_reason" in columns
    # The rows that already existed keep their values and a NULL reason.
    assert repository.pending_notification_rows() == []
    assert repository.rule_observation_pending_reason(1, "d") is None


def test_a_rule_stored_before_the_new_fields_keeps_its_behaviour(tmp_path):
    """An old `structured_rule` has no shops and no window: notify as before."""
    repository = DealRepository(tmp_path / "old.db")
    created_at = (T0 - timedelta(hours=1)).isoformat()
    repository.db.execute(
        """INSERT INTO alert_rules
        (query,product_type,brand,max_price,price_unit,enabled,state,created_at,
        updated_at,structured_rule,schema_version) VALUES ('leche','leche',NULL,
        '0.80','liter',1,'ACTIVE',?,?,?,1)""",
        (
            created_at,
            created_at,
            (
                '{"query":"leche","product":"leche","constraints":'
                '{"max_price_per_liter":"0.80"},"schema_version":1}'
            ),
        ),
    )
    repository.db.commit()

    stored = repository.rule_by_id(1)

    assert stored.include_merchants == ()
    assert stored.exclude_merchants == ()
    assert stored.notification_window is None


def test_a_delivered_pending_match_is_never_sent_twice(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    add_rule(repository, window=DAY)
    moment[0] = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
    service.run_active_rules()
    service.run_active_rules()

    moment[0] = datetime(2026, 9, 22, 7, 0, tzinfo=UTC)  # 09:00 in Madrid
    assert service.run_active_rules() == 1
    assert service.run_active_rules() == 0
    assert service.run_active_rules() == 0

    assert notifier.sent_ids == [DEAL_ID]
    assert pending_rows(repository) == []


# --- Windows that cross midnight -------------------------------------------- #


def test_a_cross_midnight_window_allows_the_evening(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    add_rule(repository, window=NIGHT)
    moment[0] = datetime(2026, 9, 22, 21, 0, tzinfo=UTC)  # 23:00 in Madrid

    service.run_active_rules()
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == [DEAL_ID]


def test_a_cross_midnight_window_allows_the_early_morning(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    add_rule(repository, window=NIGHT)
    moment[0] = datetime(2026, 9, 22, 3, 0, tzinfo=UTC)  # 05:00 in Madrid

    service.run_active_rules()
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == [DEAL_ID]


def test_a_cross_midnight_window_blocks_the_middle_of_the_day(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    rule_id = add_rule(repository, window=NIGHT)
    moment[0] = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)  # 12:00 in Madrid

    service.run_active_rules()
    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert pending_rows(repository) == [
        (rule_id, DEAL_ID, PENDING_NOTIFICATION_SCHEDULE)
    ]


# --- Timezone and DST ------------------------------------------------------- #


def test_the_window_is_read_in_the_alert_timezone_not_in_utc(tmp_path):
    """06:30 UTC is 08:30 in Madrid: inside the window, and inside only there."""
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    add_rule(repository, window=DAY)
    moment[0] = datetime(2026, 7, 15, 6, 30, tzinfo=UTC)

    service.run_active_rules()
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == [DEAL_ID]
    window = NotificationWindow(time(8, 0), time(23, 0), "Europe/Madrid")
    assert window.local_time(moment[0]) == time(8, 30)
    assert NotificationWindow(time(8, 0), time(23, 0), "UTC").allows(moment[0]) is False


def test_the_alert_can_name_another_timezone(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    add_rule(
        repository,
        window=RuleWindow(
            start=time(8, 0), end=time(23, 0), timezone="America/New_York"
        ),
    )
    moment[0] = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)  # 08:30 in New York

    service.run_active_rules()
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == [DEAL_ID]


def test_the_window_follows_the_dst_change_of_the_zone():
    window = NotificationWindow(time(8, 0), time(23, 0), "Europe/Madrid")

    # Winter: Madrid is UTC+1, so 08:00 local is 07:00 UTC.
    assert window.allows(datetime(2026, 1, 15, 7, 30, tzinfo=UTC)) is True
    assert window.allows(datetime(2026, 1, 15, 6, 30, tzinfo=UTC)) is False
    # Summer: the same wall clock is UTC+2, so 06:30 UTC is already 08:30 local.
    assert window.allows(datetime(2026, 7, 15, 6, 30, tzinfo=UTC)) is True


def test_the_window_handles_the_skipped_hour_of_the_spring_change():
    """On 2026-03-29 Madrid jumps from 02:00 to 03:00: 02:00-03:00 does not exist."""
    skipped = NotificationWindow(time(2, 0), time(3, 0), "Europe/Madrid")
    after = NotificationWindow(time(3, 0), time(4, 0), "Europe/Madrid")
    instant = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)

    assert skipped.local_time(instant) == time(3, 30)
    assert skipped.allows(instant) is False
    assert after.allows(instant) is True


def test_the_window_handles_the_repeated_hour_of_the_autumn_change():
    """On 2026-10-25 Madrid goes back to 02:00: 02:30 happens twice."""
    early = NotificationWindow(time(2, 0), time(3, 0), "Europe/Madrid")
    late = NotificationWindow(time(3, 0), time(4, 0), "Europe/Madrid")
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 CET

    assert early.local_time(first) == early.local_time(second) == time(2, 30)
    assert early.allows(first) is True and early.allows(second) is True
    assert late.allows(first) is False and late.allows(second) is False


def test_a_match_found_before_the_dst_change_is_delivered_after_it(tmp_path):
    """The window is compared with the real offset of each instant."""
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()])
    )
    add_rule(repository, window=DAY)
    moment[0] = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)  # 00:30 CET, closed

    service.run_active_rules()
    assert service.run_active_rules() == 0
    assert notifier.sent == []

    moment[0] = datetime(2026, 10, 26, 7, 30, tzinfo=UTC)  # 08:30 CET, open
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == [DEAL_ID]


# --- Pending reasons -------------------------------------------------------- #


def test_a_telegram_failure_and_a_closed_window_stay_distinguishable(tmp_path):
    failing = Notifier(failing_sends=1)
    service, repository, _notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()]), notifier=failing
    )
    rule_id = add_rule(repository, window=DAY)
    moment[0] = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)  # inside the window

    service.run_active_rules()
    service.run_active_rules()

    assert pending_rows(repository) == [(rule_id, DEAL_ID, PENDING_TELEGRAM_FAILURE)]
    assert [alert[0] for alert in failing.alerts] == ["TELEGRAM_ERROR"]

    # The next cycle inside the window delivers it and clears the reason.
    assert service.run_active_rules() == 1
    assert failing.sent_ids == [DEAL_ID]
    assert pending_rows(repository) == []


def test_a_pending_row_is_not_delivered_outside_its_window(tmp_path):
    notifier = Notifier(failing_sends=1)
    service, repository, _notifier, moment = make_service(
        tmp_path, Feed([], [make_deal()]), notifier=notifier
    )
    rule_id = add_rule(repository, window=DAY)
    moment[0] = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)  # inside the window
    service.run_active_rules()
    service.run_active_rules()
    assert pending_rows(repository) == [(rule_id, DEAL_ID, PENDING_TELEGRAM_FAILURE)]

    # Telegram is healthy again, but the window is closed: still pending.
    moment[0] = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)  # 03:00 in Madrid
    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert pending_rows(repository) == [(rule_id, DEAL_ID, PENDING_TELEGRAM_FAILURE)]

    moment[0] = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)  # 10:00 in Madrid
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == [DEAL_ID]


# --- Merchants and schedules together --------------------------------------- #


def test_an_allowed_shop_inside_the_window_is_notified(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal(merchant="Amazon")])
    )
    add_rule(repository, window=DAY, include=("Amazon", "PcComponentes"))
    moment[0] = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)

    service.run_active_rules()
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == [DEAL_ID]


def test_an_allowed_shop_outside_the_window_waits(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal(merchant="Amazon")])
    )
    rule_id = add_rule(repository, window=DAY, include=("Amazon",))
    moment[0] = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)

    service.run_active_rules()
    assert service.run_active_rules() == 0
    assert pending_rows(repository) == [
        (rule_id, DEAL_ID, PENDING_NOTIFICATION_SCHEDULE)
    ]

    moment[0] = datetime(2026, 9, 22, 6, 30, tzinfo=UTC)
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == [DEAL_ID]


def test_an_excluded_shop_is_not_matched_even_inside_the_window(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal(merchant="AliExpress")])
    )
    rule_id = add_rule(repository, window=DAY, exclude=("AliExpress",))
    moment[0] = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)

    service.run_active_rules()
    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert pending_rows(repository) == []
    assert repository.get_rule_observation(rule_id, DEAL_ID)[6] == "REJECTED_MERCHANT"


def test_an_excluded_shop_outside_the_window_is_not_even_pending(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path, Feed([], [make_deal(merchant="AliExpress")])
    )
    rule_id = add_rule(repository, window=DAY, exclude=("AliExpress",))
    moment[0] = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)

    service.run_active_rules()
    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert pending_rows(repository) == []
    assert repository.get_rule_observation(rule_id, DEAL_ID)[6] == "REJECTED_MERCHANT"


def test_two_alerts_on_one_deal_behave_independently(tmp_path):
    service, repository, notifier, moment = make_service(
        tmp_path,
        Feed([], [make_deal(merchant="Amazon")]),
    )
    always = add_rule(repository, query="portátil gaming", max_price=Decimal(2000))
    night_only = add_rule(
        repository,
        query="portátil gaming",
        window=NIGHT,
        max_price=Decimal(1000),
    )
    moment[0] = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)  # only the first is open

    service.run_active_rules()
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == [DEAL_ID]
    assert pending_rows(repository) == [
        (night_only, DEAL_ID, PENDING_NOTIFICATION_SCHEDULE)
    ]
    assert repository.get_rule_observation(always, DEAL_ID)[7] is not None

    moment[0] = datetime(2026, 9, 22, 21, 0, tzinfo=UTC)  # 23:00, both open
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == [DEAL_ID, DEAL_ID]
    assert pending_rows(repository) == []


# --- The HTML path keeps the same guarantee --------------------------------- #


def test_the_html_path_honours_the_window_too(tmp_path):
    """Without the feed (fallback or `CHOLLOMETRO_GRAPHQL_DISCOVERY=false`)."""
    html = HtmlClient([make_deal()])
    service, repository, notifier, moment = make_service(
        tmp_path, feed=None, html=html, name="html.db"
    )
    rule_id = add_rule(repository, window=DAY)
    moment[0] = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)  # 03:00 in Madrid

    assert service.run_active_rules() == 0
    assert notifier.sent == []
    assert pending_rows(repository) == [
        (rule_id, DEAL_ID, PENDING_NOTIFICATION_SCHEDULE)
    ]

    moment[0] = datetime(2026, 9, 22, 6, 30, tzinfo=UTC)  # 08:30 in Madrid
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == [DEAL_ID]
    assert pending_rows(repository) == []
