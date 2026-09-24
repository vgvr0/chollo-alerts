"""Chollometro temperature as one more filter of the existing alert engine.

The temperature is a provider fact the deal already carries (GraphQL and the
HTML parser both expose it); the alert only says which values it wants. It is
therefore one more AND condition of `InterestRule`: no second scanner, no second
evaluation path, no duplicated `Deal` field. Everything the engine already does
— baseline, "never announce a deal older than the alert", deduplication,
notification windows, merchant lists, evidence — keeps working with it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.alert_text import (
    extract_temperature_mentions,
    merge_intent,
    merge_rule,
)
from chollometro_alerts.config import InterestRule
from chollometro_alerts.evaluation import interest_rule_from_alert
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.graphql_feed import FeedBatch, thread_to_deal
from chollometro_alerts.intent import AlertIntent, intent_to_rule, validate_intent
from chollometro_alerts.merchants import MERCHANT_EXCLUDED, MERCHANT_NOT_ALLOWED
from chollometro_alerts.models import Deal
from chollometro_alerts.parser import parse_search
from chollometro_alerts.product import extract_product
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.service import AlertService
from chollometro_alerts.telegram import format_message
from chollometro_alerts.telegram_rules import TelegramRuleController

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
BEFORE = T0 - timedelta(minutes=1)
AFTER = T0 + timedelta(minutes=1)


def a_deal(
    temperature=425,
    *,
    deal_id="2012039",
    price="649.00",
    merchant="Amazon",
    title="Portátil gaming ASUS TUF",
    published_at=None,
):
    return Deal(
        deal_id,
        title,
        f"https://www.chollometro.com/ofertas/{deal_id}",
        Decimal(price) if price is not None else None,
        merchant,
        temperature,
        "generic",
        published_at,
        product_text=title,
    )


def a_rule(**overrides):
    fields = {"category": "generic"}
    fields.update(overrides)
    return InterestRule(**fields)


def codes(result):
    return [check.code for check in result.checks]


# --- The filter ------------------------------------------------------------- #


def test_a_minimum_temperature_is_reached_not_surpassed():
    assert apply_rule(a_deal(300), a_rule(temperature_min=300)).accepted
    assert apply_rule(a_deal(425), a_rule(temperature_min=300)).accepted
    # The threshold is inclusive: "al menos 500°" includes 500 itself.
    assert apply_rule(a_deal(500), a_rule(temperature_min=500)).accepted
    assert apply_rule(a_deal(299), a_rule(temperature_min=300)).reason == (
        "REJECTED_TEMPERATURE"
    )
    assert apply_rule(a_deal(499), a_rule(temperature_min=500)).reason == (
        "REJECTED_TEMPERATURE"
    )


def test_a_maximum_temperature_is_a_ceiling():
    assert apply_rule(a_deal(99), a_rule(temperature_max=100)).accepted
    assert apply_rule(a_deal(100), a_rule(temperature_max=100)).accepted
    assert apply_rule(a_deal(101), a_rule(temperature_max=100)).reason == (
        "REJECTED_TEMPERATURE"
    )


def test_a_temperature_range_requires_both_bounds():
    rule = a_rule(temperature_min=100, temperature_max=500)

    assert apply_rule(a_deal(100), rule).accepted
    assert apply_rule(a_deal(300), rule).accepted
    assert apply_rule(a_deal(500), rule).accepted
    assert apply_rule(a_deal(99), rule).reason == "REJECTED_TEMPERATURE"
    assert apply_rule(a_deal(501), rule).reason == "REJECTED_TEMPERATURE"
    assert codes(apply_rule(a_deal(300), rule)) == [
        "MIN_TEMPERATURE",
        "MAX_TEMPERATURE",
    ]


def test_a_deal_without_a_temperature_cannot_prove_a_temperature_rule():
    for rule in (
        a_rule(temperature_min=100),
        a_rule(temperature_max=500),
        a_rule(temperature_min=100, temperature_max=500),
    ):
        assert apply_rule(a_deal(None), rule).reason == "REJECTED_TEMPERATURE"
    # A rule without a temperature window claims nothing about it.
    assert apply_rule(a_deal(None), a_rule(max_price=Decimal(700))).accepted


def test_the_temperature_is_combined_with_the_price_by_and():
    rule = a_rule(max_price=Decimal(700), temperature_min=300)

    matched = apply_rule(a_deal(425, price="649.00"), rule)
    assert matched.accepted
    assert codes(matched) == ["MAX_PRICE", "MIN_TEMPERATURE"]
    # Cheap but cold: the temperature is not reached.
    assert apply_rule(a_deal(250, price="649.00"), rule).reason == (
        "REJECTED_TEMPERATURE"
    )
    # Hot but expensive: the price is not reached either.
    assert apply_rule(a_deal(425, price="800.00"), rule).reason == "REJECTED_PRICE"


def test_the_temperature_is_combined_with_the_merchants_by_and():
    allowed = a_rule(include_merchants=("Amazon",), temperature_min=300)
    assert apply_rule(a_deal(425, merchant="Amazon"), allowed).accepted
    assert apply_rule(a_deal(425, merchant="MediaMarkt"), allowed).reason == (
        MERCHANT_NOT_ALLOWED
    )
    assert apply_rule(a_deal(200, merchant="Amazon"), allowed).reason == (
        "REJECTED_TEMPERATURE"
    )

    excluded = a_rule(exclude_merchants=("AliExpress",), temperature_min=300)
    assert apply_rule(a_deal(425, merchant="AliExpress"), excluded).reason == (
        MERCHANT_EXCLUDED
    )
    assert apply_rule(a_deal(425, merchant="Amazon"), excluded).accepted


def test_the_rule_carries_the_temperature_window_into_the_engine():
    alert_rule = AlertRule(
        query="portátil gaming",
        constraints=AlertConstraints(
            max_price=Decimal(700), temperature_min=300, temperature_max=500
        ),
    )
    interest = interest_rule_from_alert(alert_rule)

    assert (interest.temperature_min, interest.temperature_max) == (300, 500)
    assert apply_rule(a_deal(425), interest).accepted
    assert apply_rule(a_deal(501), interest).reason == "REJECTED_TEMPERATURE"


# --- Natural language ------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "minimum", "maximum"),
    [
        ("Avísame de cualquier chollo con más de 500 grados", 500, None),
        ("Avísame si supera los 1000°", 1000, None),
        ("al menos 500°", 500, None),
        ("Amazon con más de 300 grados", 300, None),
        ("Portátiles por menos de 700€ y al menos 250°", 250, None),
        ("No quiero chollos por debajo de 100 grados", 100, None),
        ("menos de 100 grados", None, 100),
        ("no más de 250 grados", None, 250),
        ("como máximo 300°", None, 300),
        ("entre 100 y 500 grados", 100, 500),
        ("entre 100° y 500°", 100, 500),
        ("de 250 grados a 750°", 250, 750),
        ("más de 500 grados de temperatura", 500, None),
        ("más de 500,5 grados", 500.5, None),
    ],
)
def test_the_sentence_states_the_temperature_window(text, minimum, maximum):
    mentions = extract_temperature_mentions(text)
    assert (mentions.minimum, mentions.maximum) == (minimum, maximum)


@pytest.mark.parametrize(
    "text",
    [
        "Avísame de portátiles por menos de 700 €",
        "Avísame de cualquier chollo con más de 500 € de descuento",
        "No quiero chollos de más de 1000 €",
        "Amazon con más de 300 € de ahorro",
    ],
)
def test_a_price_is_never_read_as_a_temperature(text):
    assert extract_temperature_mentions(text).empty


def test_the_sentence_wins_over_the_provider_answer():
    answered = AlertIntent(
        action="create",
        query="portátiles",
        max_price=Decimal(700),
        price_unit="absolute",
        temperature_min=10,
    )

    merged = merge_intent(answered, "Portátiles por menos de 700 € y al menos 250°")

    assert merged.temperature_min == 250
    assert merged.temperature_max is None
    # Nothing about the temperature was said: the provider answer is kept.
    untouched = merge_intent(answered, "Portátiles por menos de 700 €")
    assert untouched is answered


def test_the_rule_of_the_parse_path_keeps_the_temperature_of_the_sentence():
    from_provider = AlertRule(
        query="portátiles",
        constraints=AlertConstraints(max_price=Decimal(700), temperature_min=10),
    )

    merged = merge_rule(from_provider, "Portátiles con más de 500 grados")

    assert merged.constraints.temperature_min == 500
    assert merged.constraints.max_price == Decimal(700)
    # The rest of the provider answer is untouched.
    assert merge_rule(from_provider, "Portátiles por menos de 700 €") is from_provider


def test_a_contradictory_temperature_window_asks_for_a_clarification():
    with pytest.raises(ValueError):
        AlertConstraints(temperature_min=500, temperature_max=100)
    with pytest.raises(ValueError):
        merge_rule(AlertRule(query="portátiles"), "entre 500 y 100 grados")


def test_a_temperature_only_alert_is_a_complete_alert():
    intent = validate_intent(
        merge_intent(
            AlertIntent(action="create", query="portátiles"),
            "Avísame de cualquier chollo con más de 500 grados",
        )
    )
    rule = intent_to_rule(intent)

    assert rule.constraints.temperature_min == 500
    assert rule.constraints.max_price is None
    assert apply_rule(a_deal(600), interest_rule_from_alert(rule)).accepted
    # An alert with neither a price nor a temperature is still incomplete.
    with pytest.raises(ValueError):
        validate_intent(
            AlertIntent(action="create", query="zapatillas", product_type="zapatillas")
        )


def test_the_creation_reply_states_the_temperature_condition():
    controller = TelegramRuleController(
        bot_token="token",
        authorized_chat_id="123",
        repository=None,
        translator=None,
    )
    intent = AlertIntent(
        action="create",
        query="portátiles",
        max_price=Decimal(700),
        price_unit="absolute",
        temperature_min=250,
    )

    reply = controller._format(intent, [])

    assert reply.startswith(
        "✅ Alerta creada: portátiles por debajo de 700,00 € y al menos 250°"
    )
    assert "🌡️ Temperatura: al menos 250°" in reply


def test_a_temperature_only_alert_reads_back_with_its_condition():
    controller = TelegramRuleController(
        bot_token="token",
        authorized_chat_id="123",
        repository=None,
        translator=None,
    )
    intent = AlertIntent(
        action="create", query="portátiles", temperature_min=500, temperature_max=900
    )

    reply = controller._format(intent, [])

    assert reply.startswith("✅ Alerta creada: portátiles con entre 500° y 900°")
    assert "🌡️ Temperatura: entre 500° y 900°" in reply


# --- The temperature of the provider --------------------------------------- #


def test_the_graphql_feed_already_carries_the_temperature():
    thread = {
        "threadId": "2012039",
        "title": "Portátil gaming ASUS TUF",
        "url": "https://www.chollometro.com/ofertas/portatil-gaming",
        "price": 649.0,
        "publishedAt": 1790112449,
        "temperature": 425.4,
    }
    from_graphql = thread_to_deal(thread)

    assert from_graphql.temperature == 425
    assert apply_rule(from_graphql, a_rule(temperature_min=300)).accepted
    assert apply_rule(from_graphql, a_rule(temperature_min=500)).reason == (
        "REJECTED_TEMPERATURE"
    )


def test_the_html_parser_already_carries_the_temperature():
    html = (
        '<article id="thread_2012039">'
        '<a class="thread-title" href="/ofertas/portatil-gaming">'
        "Portátil gaming ASUS TUF</a>"
        '<span class="thread-price">649,00€</span>'
        '<span class="thread-temperature">425°</span>'
        "</article>"
    )
    from_html = parse_search(html, "portátil gaming")[0]

    assert from_html.temperature == 425
    assert apply_rule(from_html, a_rule(temperature_min=300)).accepted


# --- Backward compatibility ------------------------------------------------- #


def test_the_original_min_temperature_field_still_works():
    legacy = AlertConstraints.model_validate({"min_temperature": 300})
    assert legacy.temperature_min == 300
    assert legacy.min_temperature == 300

    engine = InterestRule("beer", min_temperature=300)
    assert engine.temperature_min == 300
    assert engine.min_temperature == 300
    # The positional shape of the engine rule is unchanged as well.
    assert InterestRule("milk", Decimal(5), 90).temperature_min == 90


def test_the_stored_rule_has_a_single_temperature_field(tmp_path):
    repository = DealRepository(tmp_path / "legacy.db")
    rule_id = repository.save_alert_rule(
        AlertRule(query="cerveza", constraints=AlertConstraints(temperature_min=300)),
        "cerveza con más de 300 grados",
    )

    stored = repository.db.execute(
        "SELECT structured_rule FROM alert_rules WHERE id=?", (rule_id,)
    ).fetchone()[0]
    assert '"temperature_min":300' in stored.replace(" ", "")
    assert "min_temperature" not in stored
    assert repository.load_alert_rule(rule_id).constraints.temperature_min == 300


def test_a_rule_stored_by_the_previous_version_keeps_filtering(tmp_path):
    repository = DealRepository(tmp_path / "upgraded.db")
    rule_id = repository.save_alert_rule(
        AlertRule(query="cerveza"), "cerveza con más de 300 grados"
    )
    # The JSON the previous version wrote: the floor under its original name.
    repository.db.execute(
        "UPDATE alert_rules SET structured_rule=? WHERE id=?",
        (
            (
                '{"query":"cerveza","constraints":{"min_temperature":300},'
                '"schema_version":1}'
            ),
            rule_id,
        ),
    )
    repository.db.commit()

    loaded = repository.rule_by_id(rule_id)

    assert loaded.constraints.temperature_min == 300
    assert apply_rule(a_deal(250), interest_rule_from_alert(loaded)).reason == (
        "REJECTED_TEMPERATURE"
    )
    assert apply_rule(a_deal(425), interest_rule_from_alert(loaded)).accepted


# --- The whole pipeline ----------------------------------------------------- #


class Feed:
    """Scripted discovery windows, one per cycle."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.last_feed = None
        self.last_http_status = 200

    def latest(self, limit=None):
        deals = self.batches.pop(0) if self.batches else []
        self.last_feed = FeedBatch(
            deals=tuple(deals),
            window_limit=None,
            xsrf_present=True,
            fetched_at=datetime.now(UTC),
        )
        return list(deals)


class RecordingNotifier:
    dry_run = False

    def __init__(self):
        self.sent = []
        self.evidences = []

    def send(self, deal, evidence=None):
        self.sent.append(deal)
        self.evidences.append(evidence)

    def send_system_alert(self, *arguments):
        pass

    @property
    def messages(self):
        return [
            format_message(deal, evidence)
            for deal, evidence in zip(self.sent, self.evidences, strict=True)
        ]


def make_service(tmp_path, feed, notifier=None):
    repository = DealRepository(tmp_path / "temperature.db")
    notifier = notifier if notifier is not None else RecordingNotifier()
    extractor = lambda product_text, deal_id=None: extract_product(product_text)
    service = AlertService(None, repository, notifier, extractor, feed=feed)
    return service, repository, notifier


def add_rule(repository, *, constraints, created_at=T0, query="portátil gaming"):
    rule_id = repository.save_alert_rule(
        AlertRule(query=query, constraints=constraints), query
    )
    repository.db.execute(
        "UPDATE alert_rules SET created_at=? WHERE id=?",
        (created_at.isoformat(), rule_id),
    )
    repository.db.commit()
    return rule_id


class Translator:
    """Stands in for the provider: it returns the intent of the test."""

    def __init__(self, intent):
        self.intent = intent

    def interpret_alert(self, text):
        return self.intent


class Controller(TelegramRuleController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.replies = []

    def send_message(self, text):
        self.replies.append(text)


def test_a_temperature_only_alert_is_created_and_listed(tmp_path):
    """The legacy price columns are NOT NULL, and a temperature-only alert has
    no price: the stored rule is what says so, for the reply and the listing."""
    repository = DealRepository(tmp_path / "chat.db")
    controller = Controller(
        bot_token="token",
        authorized_chat_id="7",
        repository=repository,
        translator=Translator(AlertIntent(action="create", query="portátiles")),
    )

    reply = controller.process_update(
        {
            "update_id": 1,
            "message": {
                "chat": {"id": 7},
                "text": "Avísame de cualquier chollo con más de 500 grados",
            },
        }
    )

    assert reply.startswith("✅ Alerta creada: portátiles con al menos 500°")
    (row,) = repository.list_alert_rules()
    stored = repository.rule_from_listing(row)
    assert stored.constraints.temperature_min == 500
    assert stored.constraints.max_price is None

    listing = controller._format(
        AlertIntent(action="list"), repository.list_alert_rules()
    )
    assert "🔥 Temperatura mínima: 500°" in listing
    assert "0,00 €" not in listing


def test_the_telegram_message_explains_the_temperature_condition(tmp_path):
    reference = a_deal(425, deal_id="feed-old", published_at=BEFORE)
    hot = a_deal(425, price="649.00", published_at=AFTER)
    feed = Feed([reference], [reference, hot])
    service, repository, notifier = make_service(tmp_path, feed)
    add_rule(
        repository,
        constraints=AlertConstraints(max_price=Decimal(700), temperature_min=300),
    )

    service.run_active_rules()  # bootstrap: the window is only a reference
    assert notifier.sent == []

    assert service.run_active_rules() == 1

    message = notifier.messages[0]
    assert "✅ Cumple:" in message
    assert "• Precio máximo: 649 € < 700 €" in message
    assert "• Temperatura mínima: 425° ≥ 300°" in message
    assert "🔥 Temperatura: 425°" in message


def test_a_deal_that_does_not_reach_the_temperature_is_never_notified(tmp_path):
    cold = a_deal(250, published_at=AFTER)
    feed = Feed([cold], [cold])
    service, repository, notifier = make_service(tmp_path, feed)
    rule_id = add_rule(
        repository,
        constraints=AlertConstraints(max_price=Decimal(700), temperature_min=300),
    )

    service.run_active_rules()  # bootstrap
    assert service.run_active_rules() == 0

    assert notifier.sent == []
    observation = repository.get_rule_observation(rule_id, cold.deal_id)
    assert (observation[5], observation[6]) == (0, "REJECTED_TEMPERATURE")
    assert repository.pending_rule_notifications() == []


def test_a_temperature_match_older_than_the_alert_is_never_announced(tmp_path):
    """The baseline guarantee survives the new filter, unchanged."""
    old = a_deal(425, deal_id="feed-old", published_at=BEFORE)
    new = a_deal(425, deal_id="feed-new", published_at=AFTER)
    feed = Feed([old], [old, new])
    service, repository, notifier = make_service(tmp_path, feed)
    add_rule(repository, constraints=AlertConstraints(temperature_min=300))

    service.run_active_rules()  # bootstrap: the deal predating the alert
    assert notifier.sent == []

    assert service.run_active_rules() == 1
    assert [deal.deal_id for deal in notifier.sent] == ["feed-new"]
    assert repository.seen_feed_thread_ids(["feed-old"]) == {"feed-old"}


def test_the_status_of_the_last_cycle_is_exposed(tmp_path):
    """The optional status surface, fed by the counters the cycle already keeps."""
    reference = a_deal(425, deal_id="feed-old", published_at=BEFORE)
    hot = a_deal(425, deal_id="feed-new", published_at=AFTER)
    feed = Feed([reference], [reference, hot])
    service, repository, _notifier = make_service(tmp_path, feed)
    add_rule(repository, constraints=AlertConstraints(temperature_min=300))

    assert service.status_snapshot()["last_scan"] is None

    service.run_active_rules()  # bootstrap
    service.run_active_rules()  # one new, matching deal

    status = service.status_snapshot()
    assert status["last_scan"] is not None
    assert status["last_scan_status"] == "SUCCESS"
    assert status["last_error"] is None
    assert status["deals_seen"] == 2
    assert status["deals_matched"] == 1
    assert status["notifications_sent"] == 1
