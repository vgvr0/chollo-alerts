"""The first discovery cycle: feed state **and** what the alerts can prove.

The bootstrap used to be a blind snapshot: the whole window was recorded as
seen and nothing was evaluated, so every chollo published between the creation
of an alert and the first daemon cycle was lost. The rule is temporal and
per-alert:

    deal.published_at <= alert.created_at  -> never announced for that alert
    deal.published_at  > alert.created_at  -> eligible, first cycle or not

The fixtures reuse the scripted feed of `test_graphql_discovery`.
"""

from datetime import timedelta
from decimal import Decimal

import test_graphql_discovery as discovery

from chollometro_alerts.alert_rule import AlertConstraints
from chollometro_alerts.models import Deal
from chollometro_alerts.service import AlertService

T0 = discovery.T0
MINUTE = timedelta(minutes=1)


def price_only_rule(**constraints):
    return AlertConstraints(**{"max_price": Decimal(15), **constraints})


def product_extractor(products):
    """Provider stand-in: the model only knows the product type per deal."""

    def extractor(product_text, deal_id=None):
        return {"product_type": products[deal_id]} if deal_id in products else {}

    return extractor


def html_deal(deal_id, price, title=None, published_at=None):
    """A deal as the HTML provider reports it: usually without a timestamp."""
    text = title or f"Producto {deal_id}"
    return Deal(
        deal_id,
        text,
        f"https://www.chollometro.com/ofertas/{deal_id}",
        Decimal(price),
        "Amazon",
        120,
        "generic",
        published_at,
        product_text=text,
    )


def test_first_scan_ignores_deals_published_before_alert(tmp_path):
    feed = discovery.FakeFeed(
        [
            discovery.make_deal("A", T0 - timedelta(minutes=10), price="5.00"),
            discovery.make_deal("B", discovery.BEFORE, price="5.00"),
        ]
    )
    service, repository, notifier, extractor = discovery.make_service(tmp_path, feed)
    rule_id = discovery.add_rule(
        repository, query="despertador", constraints=price_only_rule()
    )

    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert extractor.calls == 0
    assert service.last_summary.classified == 0
    assert service.last_summary.before_alert == 2
    # A deal the alert cannot prove to be new leaves no verdict behind.
    assert repository.rule_observations(rule_id) == []
    assert repository.seen_feed_thread_ids() == {"A", "B"}


def test_first_scan_ignores_deal_published_exactly_at_alert_creation(tmp_path):
    """`published_at == created_at` is not "newer": the comparison is strict."""
    feed = discovery.FakeFeed([discovery.make_deal("B", T0, price="5.00")])
    service, repository, notifier, extractor = discovery.make_service(tmp_path, feed)
    rule_id = discovery.add_rule(
        repository, created_at=T0, constraints=price_only_rule()
    )

    assert service.run_active_rules() == 0

    assert notifier.sent == [] and extractor.calls == 0
    assert repository.rule_observations(rule_id) == []
    assert repository.seen_feed_thread_ids() == {"B"}


def test_first_scan_evaluates_deal_published_after_alert(tmp_path):
    """One second after the alert is enough: the first cycle evaluates it."""
    feed = discovery.FakeFeed(
        [
            discovery.make_deal("C", T0 + timedelta(seconds=1), price="25.00"),
            discovery.make_deal("D", T0 + timedelta(minutes=5), price="7.95"),
        ]
    )
    service, repository, notifier, extractor = discovery.make_service(tmp_path, feed)
    rule_id = discovery.add_rule(repository, constraints=price_only_rule())

    assert service.run_active_rules() == 1

    assert service.last_summary.classified == 2
    assert extractor.calls == 2
    too_expensive = repository.get_rule_observation(rule_id, "C")
    assert (too_expensive[5], too_expensive[6]) == (0, "REJECTED_PRICE")
    assert repository.get_rule_observation(rule_id, "D")[5] == 1
    assert notifier.sent_ids == ["D"]


def test_first_scan_notifies_matching_deal_published_after_alert(tmp_path):
    feed = discovery.FakeFeed(
        [
            discovery.make_deal("B", T0, price="5.00"),
            discovery.make_deal(
                "C",
                T0 + timedelta(minutes=5),
                price="7.95",
                title="Luz despertador GRUNDIG que simula un amanecer",
            ),
        ]
    )
    service, repository, notifier, _extractor = discovery.make_service(tmp_path, feed)
    discovery.add_rule(
        repository,
        query="despertador",
        created_at=T0,
        constraints=price_only_rule(),
    )

    assert service.run_active_rules() == 1

    assert notifier.sent_ids == ["C"]
    assert "Luz despertador GRUNDIG" in notifier.messages[0]


def test_first_scan_records_entire_feed_as_seen(tmp_path):
    """Whatever the eligibility, the whole window becomes the seen state."""
    deals = [
        discovery.make_deal("A", T0 - timedelta(minutes=10), price="5.00"),
        discovery.make_deal("B", T0, price="5.00"),
        discovery.make_deal("C", T0 + timedelta(minutes=5), price="7.95"),
        discovery.make_deal("D", T0 + timedelta(minutes=6), price="25.00"),
    ]
    feed = discovery.FakeFeed(list(deals), list(deals))
    service, repository, _notifier, _extractor = discovery.make_service(tmp_path, feed)
    discovery.add_rule(repository, constraints=price_only_rule())

    assert service.run_active_rules() == 1

    assert repository.seen_feed_thread_ids() == {"A", "B", "C", "D"}
    assert repository.feed_is_initialized() is True
    assert service.last_feed_received == 4
    # Every thread of the window is new for the seen store on the first cycle.
    assert service.last_feed_new == 4
    runs = discovery.feed_scan_runs(repository)
    assert (runs[0]["status"], runs[0]["new_items"], runs[0]["notifications_sent"]) == (
        "SUCCESS",
        4,
        1,
    )


def test_second_scan_does_not_duplicate_notifications(tmp_path):
    deals = [
        discovery.make_deal("A", T0 - timedelta(minutes=10), price="5.00"),
        discovery.make_deal("B", T0, price="5.00"),
        discovery.make_deal("C", T0 + timedelta(minutes=5), price="7.95"),
        discovery.make_deal("D", T0 + timedelta(minutes=6), price="25.00"),
    ]
    feed = discovery.FakeFeed(list(deals), list(deals), list(deals))
    service, repository, notifier, extractor = discovery.make_service(tmp_path, feed)
    discovery.add_rule(repository, constraints=price_only_rule())

    assert service.run_active_rules() == 1
    calls_after_first_cycle = extractor.calls
    assert service.run_active_rules() == 0
    assert service.run_active_rules() == 0

    assert notifier.sent_ids == ["C"]
    assert service.last_feed_new == 0
    assert service.last_summary.classified == 0
    assert extractor.calls == calls_after_first_cycle == 2
    assert repository.pending_rule_notifications() == []


def test_temporal_eligibility_is_per_alert(tmp_path):
    """Alert A (10:00) takes the deal, alert B (10:10) must not see it."""
    feed = discovery.FakeFeed(
        [discovery.make_deal("deal", T0 + timedelta(minutes=5), price="7.95")]
    )
    service, repository, notifier, _extractor = discovery.make_service(tmp_path, feed)
    older = discovery.add_rule(
        repository, query="despertador", created_at=T0, constraints=price_only_rule()
    )
    newer = discovery.add_rule(
        repository,
        query="leche",
        created_at=T0 + timedelta(minutes=10),
        constraints=price_only_rule(),
    )

    assert service.run_active_rules() == 1

    assert notifier.sent_ids == ["deal"]
    assert repository.get_rule_observation(older, "deal")[5] == 1
    # The newer alert is younger than the deal: no observation, no alert.
    assert repository.get_rule_observation(newer, "deal") is None
    assert service.last_summary.before_alert == 1
    assert repository.pending_rule_notifications() == []


def test_a_deal_published_after_every_alert_is_eligible_for_all_of_them(tmp_path):
    """The oldest alert does not shadow the newer ones."""
    feed = discovery.FakeFeed(
        [discovery.make_deal("deal", T0 + timedelta(minutes=20), price="7.95")]
    )
    service, repository, notifier, _extractor = discovery.make_service(tmp_path, feed)
    for query, created_at in (
        ("despertador", T0),
        ("despertador barato", T0 + timedelta(minutes=10)),
    ):
        discovery.add_rule(
            repository,
            query=query,
            created_at=created_at,
            constraints=price_only_rule(),
        )

    assert service.run_active_rules() == 2

    assert sorted(notifier.sent_ids) == ["deal", "deal"]
    assert len(notifier.sent) == 2


def test_end_to_end_first_scan_notifies_and_never_duplicates(tmp_path):
    """Alert -> later deal -> first scan -> match -> no duplicate.

    The exact scenario of the requirement:

        alert created 10:00
        A 09:50 (before)  no evaluation, no notification
        B 10:00 (equal)   no evaluation, no notification
        C 10:05 (after)   evaluated, matches, Telegram
        D 10:06 (after)   evaluated, rejected
        A B C D           recorded as seen
        second scan       nothing evaluated, nothing notified
    """
    deals = [
        discovery.make_deal("A", T0 - timedelta(minutes=10), price="5.00"),
        discovery.make_deal("B", T0, price="5.00"),
        discovery.make_deal(
            "C",
            T0 + timedelta(minutes=5),
            price="7.95",
            title="Luz despertador GRUNDIG que simula un amanecer",
        ),
        discovery.make_deal(
            "D", T0 + timedelta(minutes=6), price="25.00", title="Cafetera de goteo"
        ),
    ]
    feed = discovery.FakeFeed(list(deals), list(deals), list(deals))
    notifier = discovery.RecordingNotifier()
    extractor = product_extractor({"C": "despertador"})
    service, repository, _notifier, _extractor = discovery.make_service(
        tmp_path, feed, notifier=notifier, extractor=extractor
    )
    rule_id = discovery.add_rule(
        repository,
        query="despertador",
        product="despertador",
        created_at=T0,
        constraints=price_only_rule(),
    )

    # First scan: only C and D are evaluated, only C is announced.
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == ["C"]
    observation = repository.get_rule_observation(rule_id, "D")
    assert (observation[5], observation[6]) == (0, "REJECTED_PRODUCT")
    assert (
        repository.rule_observations(rule_id)
        and len(repository.rule_observations(rule_id)) == 2
    )
    assert repository.seen_feed_thread_ids() == {"A", "B", "C", "D"}
    assert repository.pending_rule_notifications() == []

    # Second and third cycles with the identical feed: nothing new at all.
    assert service.run_active_rules() == 0
    assert service.run_active_rules() == 0
    assert notifier.sent_ids == ["C"]
    assert service.last_summary.classified == 0
    assert service.last_feed_new == 0


def test_html_fallback_relies_on_the_rule_baseline_not_on_timestamps(tmp_path):
    """The HTML path cannot compare `published_at`; its baseline does the job.

    The cards the HTML provider returns usually carry no timestamp at all, so
    the fallback keeps the original guarantee instead of inventing a comparison:
    the deals already in the page when the alert was created are claimed as the
    rule's baseline and are never announced, while a deal discovered later is
    evaluated. The feed's strict temporal window is therefore not duplicated
    here; this test pins that documented difference.
    """

    class ScriptedClient:
        def __init__(self, *pages):
            self.pages = [list(page) for page in pages]
            self.calls = []
            self.last_scan = None
            self.last_search = {}

        def recent(self, queries, pages):
            self.calls.append((tuple(queries), pages))
            return list(self.pages.pop(0)) if self.pages else []

    known = html_deal("known", "5.00", title="Luz despertador ya publicado")
    fresh = html_deal("fresh", "7.95", title="Luz despertador recién publicado")
    client = ScriptedClient([known], [known, fresh])
    repository = discovery.DealRepository(tmp_path / "html-fallback.db")
    notifier = discovery.RecordingNotifier()
    service = AlertService(client, repository, notifier)
    rule_id = discovery.add_rule(
        repository, query="despertador", constraints=price_only_rule()
    )

    # Creating the alert takes the baseline of the page as it is right now.
    assert service.baseline_rule(rule_id, "despertador") == 1
    assert service.run_active_rules() == 1

    assert notifier.sent_ids == ["fresh"]
    observation = repository.get_rule_observation(rule_id, "known")
    assert (observation[4], observation[5]) == (1, None)
