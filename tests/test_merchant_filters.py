"""Merchants allowed/excluded by an alert: deterministic, before the LLM.

The filter is part of the rule, not of the model: `allowed = []` means "any
shop except the excluded ones", a non-empty `allowed` means "only these", and
the exclusion list always wins. The comparison normalises case, accents,
whitespace and punctuation but never matches a longer name by substring, so
`Amazon Marketplace XYZ` is not `Amazon`.
"""

from datetime import UTC, datetime
from decimal import Decimal

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.config import InterestRule
from chollometro_alerts.evaluation import interest_rule_from_alert
from chollometro_alerts.filters import apply_rule, merchant_decision
from chollometro_alerts.graphql_feed import thread_to_deal
from chollometro_alerts.merchants import (
    MERCHANT_EXCLUDED,
    MERCHANT_NOT_ALLOWED,
    merchant_verdict,
    normalize_merchant,
)
from chollometro_alerts.models import Deal
from chollometro_alerts.parser import parse_search
from chollometro_alerts.product import extract_product
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.service import AlertService

PUBLISHED = 1790112449


def deal(merchant="Amazon", price="899", title="Portátil gaming ASUS TUF"):
    return Deal(
        "2012039",
        title,
        "https://www.chollometro.com/ofertas/portatil-gaming",
        Decimal(price),
        merchant,
        350,
        "generic",
        datetime(2026, 9, 22, 21, 27, 29, tzinfo=UTC),
        product_text=title,
    )


def rule(**overrides):
    fields = {"category": "generic", "max_price": Decimal(1000)}
    fields.update(overrides)
    return InterestRule(**fields)


def reason(merchant, **overrides):
    return apply_rule(deal(merchant), rule(**overrides)).reason


# --- Semantics -------------------------------------------------------------- #


def test_an_empty_allow_list_accepts_every_shop():
    for merchant in ("Amazon", "PcComponentes", "MediaMarkt", "Lidl"):
        assert merchant_verdict(merchant, [], []).accepted
        assert apply_rule(deal(merchant), rule()).accepted


def test_a_shop_of_the_allow_list_passes():
    assert reason("Amazon", include_merchants=("Amazon",)) == "ACCEPTED"
    assert reason("PcComponentes", include_merchants=("Amazon", "PcComponentes")) == (
        "ACCEPTED"
    )


def test_a_shop_outside_the_allow_list_is_rejected():
    assert reason("PcComponentes", include_merchants=("Amazon",)) == (
        MERCHANT_NOT_ALLOWED
    )
    assert reason("MediaMarkt", include_merchants=("Amazon", "PcComponentes")) == (
        MERCHANT_NOT_ALLOWED
    )


def test_an_excluded_shop_is_rejected():
    assert reason("AliExpress", exclude_merchants=("AliExpress",)) == MERCHANT_EXCLUDED
    assert reason("AliExpress", include_merchants=("Amazon",)) == MERCHANT_NOT_ALLOWED


def test_the_exclusion_list_has_priority_over_the_allow_list():
    verdict = merchant_verdict("AliExpress", ["AliExpress", "Amazon"], ["AliExpress"])
    assert (verdict.accepted, verdict.reason) == (False, MERCHANT_EXCLUDED)
    assert (
        reason(
            "AliExpress",
            include_merchants=("AliExpress", "Amazon"),
            exclude_merchants=("AliExpress",),
        )
        == MERCHANT_EXCLUDED
    )


def test_the_comparison_ignores_case_and_spaces():
    assert reason("AMAZON", include_merchants=("amazon",)) == "ACCEPTED"
    assert reason("  pc   componentes  ", include_merchants=("PcComponentes",)) == (
        "ACCEPTED"
    )
    assert reason("PcComponentes", include_merchants=("Pc Componentes",)) == (
        "ACCEPTED"
    )
    assert reason("  Ali Express  ", exclude_merchants=("AliExpress",)) == (
        MERCHANT_EXCLUDED
    )


def test_the_comparison_folds_accents_without_fuzzy_matching():
    assert normalize_merchant("Showroomprivé") == normalize_merchant("showroomprive")
    assert reason("Showroomprivé", include_merchants=("Showroomprive",)) == "ACCEPTED"
    # A different shop is never the same shop, however similar the name is.
    assert reason("Amazon.de", include_merchants=("Amazon",)) == (MERCHANT_NOT_ALLOWED)
    assert reason("Amazon Marketplace XYZ", include_merchants=("Amazon",)) == (
        MERCHANT_NOT_ALLOWED
    )
    assert reason("Amazon", exclude_merchants=("Amazon.de",)) == "ACCEPTED"


def test_a_deal_without_a_merchant_cannot_prove_an_allow_list():
    assert reason(None, include_merchants=("Amazon",)) == MERCHANT_NOT_ALLOWED
    # ... but it cannot prove an exclusion either.
    assert reason(None, exclude_merchants=("AliExpress",)) == "ACCEPTED"


def test_the_engine_explains_the_merchant_it_allowed():
    verdict, checks = merchant_decision(
        "PcComponentes",
        rule(include_merchants=("PcComponentes",), exclude_merchants=("AliExpress",)),
    )
    assert verdict.accepted
    assert [(check.code, check.detail) for check in checks] == [
        ("MERCHANT_EXCLUDED", "AliExpress"),
        ("MERCHANT_INCLUDED", "PcComponentes"),
    ]


# --- Where the merchant name comes from ------------------------------------- #


def test_the_graphql_merchant_name_feeds_the_filter():
    thread = {
        "threadId": "2012039",
        "title": "Portátil gaming ASUS TUF",
        "url": "https://www.chollometro.com/ofertas/portatil-gaming",
        "price": 899.0,
        "publishedAt": PUBLISHED,
        "merchant": {"merchantId": "7383", "merchantName": "PcComponentes"},
    }
    from_graphql = thread_to_deal(thread)

    assert from_graphql.merchant == "PcComponentes"
    assert apply_rule(from_graphql, rule(include_merchants=("PcComponentes",))).accepted
    assert (
        apply_rule(from_graphql, rule(exclude_merchants=("PcComponentes",))).reason
        == MERCHANT_EXCLUDED
    )


def test_the_html_merchant_name_feeds_the_filter():
    html = (
        '<article id="thread_2012039">'
        '<a class="thread-title" href="/ofertas/portatil-gaming">'
        "Portátil gaming ASUS TUF</a>"
        '<span class="thread-price">899,00€</span>'
        '<a data-t="merchantLink">Amazon</a>'
        "</article>"
    )
    from_html = parse_search(html, "portátil gaming")[0]

    assert from_html.merchant == "Amazon"
    assert apply_rule(from_html, rule(include_merchants=("Amazon",))).accepted
    assert (
        apply_rule(from_html, rule(exclude_merchants=("Amazon",))).reason
        == MERCHANT_EXCLUDED
    )


# --- The filter runs before the model --------------------------------------- #


class Feed:
    """Scripted discovery windows, one per cycle."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.last_feed = None
        self.last_http_status = 200
        self.calls = 0

    def latest(self, limit=None):
        from chollometro_alerts.graphql_feed import FeedBatch

        self.calls += 1
        deals = self.batches.pop(0) if self.batches else []
        self.last_feed = FeedBatch(
            deals=tuple(deals),
            window_limit=None,
            xsrf_present=True,
            fetched_at=datetime.now(UTC),
        )
        return list(deals)


class Notifier:
    dry_run = False

    def __init__(self):
        self.sent = []

    def send(self, sent_deal, evidence=None):
        self.sent.append(sent_deal)

    def send_system_alert(self, *arguments):
        pass


class CountingExtractor:
    """Records every provider call a cycle would spend."""

    def __init__(self):
        self.calls = 0

    def __call__(self, product_text, deal_id=None):
        self.calls += 1
        return extract_product(product_text)


def make_service(tmp_path, feed, notifier=None, extractor=None):
    repository = DealRepository(tmp_path / "merchants.db")
    notifier = notifier if notifier is not None else Notifier()
    extractor = extractor if extractor is not None else CountingExtractor()
    service = AlertService(
        None,
        repository,
        notifier,
        extractor,
        feed=feed,
    )
    return service, repository, notifier, extractor


def add_rule(repository, *, include=(), exclude=(), query="portátil gaming"):
    created = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    rule_id = repository.save_alert_rule(
        AlertRule(
            query=query,
            include_merchants=tuple(include),
            exclude_merchants=tuple(exclude),
            constraints=AlertConstraints(max_price=Decimal(1000)),
        ),
        query,
    )
    repository.db.execute(
        "UPDATE alert_rules SET created_at=? WHERE id=?", (created.isoformat(), rule_id)
    )
    repository.db.commit()
    return rule_id


def test_the_merchant_filter_rejects_before_the_llm(tmp_path):
    feed = Feed([], [deal("AliExpress")])
    service, repository, notifier, extractor = make_service(tmp_path, feed)
    rule_id = add_rule(repository, include=("Amazon",), exclude=("AliExpress",))

    service.run_active_rules()  # bootstrap
    assert service.run_active_rules() == 0

    assert notifier.sent == []
    assert extractor.calls == 0
    observation = repository.get_rule_observation(rule_id, "2012039")
    assert (observation[5], observation[6]) == (0, MERCHANT_EXCLUDED)
    assert repository.pending_rule_notifications() == []


def test_an_allowed_shop_is_evaluated_and_notified(tmp_path):
    feed = Feed([], [deal("PcComponentes")])
    service, repository, notifier, extractor = make_service(tmp_path, feed)
    add_rule(repository, include=("Amazon", "PcComponentes"), exclude=("AliExpress",))

    service.run_active_rules()  # bootstrap
    assert service.run_active_rules() == 1

    assert [sent.deal_id for sent in notifier.sent] == ["2012039"]
    assert extractor.calls == 1


def test_the_rule_carries_the_merchants_into_the_engine():
    alert_rule = AlertRule(
        query="portátil gaming",
        include_merchants=("Amazon", "PcComponentes"),
        exclude_merchants=("AliExpress",),
    )
    interest = interest_rule_from_alert(alert_rule)

    assert interest.include_merchants == ("Amazon", "PcComponentes")
    assert interest.exclude_merchants == ("AliExpress",)
    assert apply_rule(deal("MediaMarkt"), interest).reason == MERCHANT_NOT_ALLOWED
    assert apply_rule(deal("AliExpress"), interest).reason == MERCHANT_EXCLUDED
    assert apply_rule(deal("Amazon"), interest).accepted
