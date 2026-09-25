"""Shop lists and notification hours read from the sentence itself.

The model interprets the alert, but these two facts are also read
deterministically: they decide whether Telegram is sent at all and when, so
they must never be an invention. A vague period ("por la noche") is recognised
and answered with a request for concrete hours instead of invented bounds.
"""

from datetime import UTC, datetime, time
from decimal import Decimal

import pytest

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.alert_text import (
    deterministic_price_alert,
    extract_merchant_mentions,
    extract_notification_window,
    extract_unit_price_mention,
    merge_intent,
    merge_rule,
    vague_period,
)
from chollometro_alerts.evaluation import MatchEvidence, interest_rule_from_alert
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.intent import AlertIntent, intent_to_rule, validate_intent
from chollometro_alerts.models import Deal
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.telegram import format_message
from chollometro_alerts.telegram_rules import TelegramRuleController

COMPLETE = (
    "Avísame de portátiles gaming por menos de 1000 € de Amazon o PcComponentes, "
    "pero no AliExpress"
)


# --- Unit prices ------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("text", "value", "unit"),
    [
        ("leche por debajo de 0.8€ el litro", "0.8", "liter"),
        ("leche por debajo de 0,8€ el litro", "0.8", "liter"),
        ("leche por debajo de 0.8 €/L", "0.8", "liter"),
        ("leche por debajo de 0,8 €/L", "0.8", "liter"),
        ("leche por debajo de 0,80 €/L", "0.80", "liter"),
        ("leche a menos de 0.8 euros el litro", "0.8", "liter"),
        ("leche a menos de 0.8 euros por litro", "0.8", "liter"),
        ("leche a menos de 80 céntimos el litro", "0.8", "liter"),
        ("leche a menos de 80 centimos por litro", "0.8", "liter"),
        ("leche por debajo de 80 ct/L", "0.8", "liter"),
        ("leche máximo 0.8 €/l", "0.8", "liter"),
        ("arroz por debajo de 0,8 €/kg", "0.8", "kilogram"),
        ("pilas por debajo de 80 céntimos por unidad", "0.8", "unit"),
    ],
)
def test_conversational_unit_prices_are_normalized(text, value, unit):
    mention = extract_unit_price_mention(text)
    assert mention is not None
    assert mention.value == Decimal(value)
    assert mention.unit == unit


def test_total_price_is_not_reclassified_as_unit_price():
    assert extract_unit_price_mention("leche por menos de 5 €") is None


def test_basic_unit_price_alert_is_deterministic_without_provider():
    intent = deterministic_price_alert(
        "Quiero alertas de leche por debajo de 0.8€ el litro"
    )
    assert intent is not None
    assert intent.query == "leche"
    assert intent.max_price == Decimal("0.8")
    assert intent.price_unit == "liter"


def test_unit_price_repairs_incomplete_provider_intent_and_keeps_other_constraints():
    text = "leche de Amazon por debajo de 0.8 €/L y temperatura mayor a 300"
    intent = merge_intent(
        AlertIntent(
            action="create",
            query="leche",
            max_price=Decimal("0.8"),
            price_unit=None,
        ),
        text,
    )
    rule = intent_to_rule(validate_intent(intent))
    assert rule.query == "leche"
    assert rule.include_merchants == ("Amazon",)
    assert rule.constraints.max_price_per_liter == Decimal("0.8")
    assert rule.constraints.temperature_min == 300


def test_ambiguous_price_without_currency_and_unit_still_needs_clarification():
    intent = AlertIntent(action="create", query="leche", max_price=Decimal("0.8"))
    with pytest.raises(ValueError, match="precio máximo y su unidad"):
        validate_intent(merge_intent(intent, "leche por debajo de 0.8"))


# --- Shops ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("text", "allowed", "excluded"),
    [
        (COMPLETE, ("Amazon", "PcComponentes"), ("AliExpress",)),
        ("Avísame de portátiles gaming de Amazon", ("Amazon",), ()),
        ("Avísame de portátiles gaming, solo Amazon", ("Amazon",), ()),
        (
            "Avísame de portátiles gaming de Amazon y PcComponentes",
            ("Amazon", "PcComponentes"),
            (),
        ),
        ("Avísame de portátiles, no AliExpress", (), ("AliExpress",)),
        ("Avísame de portátiles, excepto AliExpress", (), ("AliExpress",)),
        ("Avísame de portátiles, excluir AliExpress", (), ("AliExpress",)),
        ("Avísame de portátiles gaming de MediaMarkt", ("MediaMarkt",), ()),
    ],
)
def test_the_shop_lists_are_read_from_the_sentence(text, allowed, excluded):
    mentions = extract_merchant_mentions(text)
    assert mentions.allowed == allowed
    assert mentions.excluded == excluded


@pytest.mark.parametrize(
    "text",
    [
        "Avísame de Zapatillas Nike por menos de 100 €",
        "Cerveza Mahou y Coca-Cola por menos de 20 €",
        "Avísame de portátiles gaming por menos de 1000 €",
        "Avísame de ASUS ROG por menos de 1000 €",
        "Leche Pascual y Puleva por menos de 5 €",
    ],
)
def test_a_product_phrase_is_never_read_as_a_shop(text):
    assert extract_merchant_mentions(text).empty


def test_the_shops_of_the_sentence_win_over_the_provider_answer():
    intent = AlertIntent(
        action="create",
        query="portátil gaming",
        max_price=Decimal(1000),
        price_unit="absolute",
        include_merchants=["MediaMarkt"],
    )
    merged = merge_intent(intent, COMPLETE)

    assert merged.include_merchants == ["Amazon", "PcComponentes"]
    assert merged.exclude_merchants == ["AliExpress"]


def test_the_provider_answer_is_kept_when_the_sentence_says_nothing():
    intent = AlertIntent(
        action="create",
        query="portátiles",
        max_price=Decimal(1000),
        price_unit="absolute",
        include_merchants=["amazon"],
    )
    assert merge_intent(intent, "Avísame de portátiles por menos de 1000 €") is intent


# --- Notification hours ----------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "start", "end", "zone"),
    [
        (
            "Avísame de portátiles solo entre las 08:00 y las 23:00",
            "08:00",
            "23:00",
            "Europe/Madrid",
        ),
        ("Avísame de portátiles de 8:00 a 23:00", "08:00", "23:00", "Europe/Madrid"),
        ("Avísame de portátiles 08:00-23:00", "08:00", "23:00", "Europe/Madrid"),
        ("Avísame de portátiles de 22:00 a 07:00", "22:00", "07:00", "Europe/Madrid"),
        (
            "Avísame de portátiles entre las 09:00 y las 21:00 Europe/Madrid",
            "09:00",
            "21:00",
            "Europe/Madrid",
        ),
        (
            "Avísame de portátiles de 9:00 a 21:00 (hora peninsular)",
            "09:00",
            "21:00",
            "Europe/Madrid",
        ),
    ],
)
def test_concrete_hours_become_a_window(text, start, end, zone):
    window = extract_notification_window(text)
    assert window is not None
    assert (f"{window.start:%H:%M}", f"{window.end:%H:%M}", window.timezone) == (
        start,
        end,
        zone,
    )


def test_a_window_that_crosses_midnight_is_kept_as_written():
    window = extract_notification_window("Avísame de portátiles de 22:00 a 07:00")
    assert window.crosses_midnight is True
    assert window.allows(datetime(2026, 9, 22, 21, 0, tzinfo=UTC)) is True  # 23:00
    assert window.allows(datetime(2026, 9, 22, 3, 0, tzinfo=UTC)) is True  # 05:00
    assert window.allows(datetime(2026, 9, 22, 10, 0, tzinfo=UTC)) is False  # 12:00


def test_a_vague_period_never_becomes_invented_hours():
    text = "Avísame de portátiles por menos de 1000 € pero no me avises por la noche"

    assert vague_period(text) == "por la noche"
    assert extract_notification_window(text) is None
    with pytest.raises(ValueError, match="por la noche"):
        merge_intent(
            AlertIntent(
                action="create",
                query="portátiles",
                max_price=Decimal(1000),
                price_unit="absolute",
            ),
            text,
        )


def test_the_model_cannot_invent_a_window_for_a_vague_period():
    """A provider answer with invented hours is discarded, not stored."""
    text = "Avísame de portátiles por la noche"
    with pytest.raises(ValueError):
        merge_rule(
            AlertRule(
                query="portátiles",
                notification_window={
                    "start": "22:00",
                    "end": "08:00",
                    "timezone": "Europe/Madrid",
                },
            ),
            text,
        )


def test_an_unknown_timezone_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        AlertRule(
            query="portátiles",
            notification_window={
                "start": "08:00",
                "end": "23:00",
                "timezone": "Europe/Atlantis",
            },
        )


# --- The intent → rule boundary --------------------------------------------- #


def test_the_intent_carries_shops_and_schedule_into_the_rule():
    intent = merge_intent(
        AlertIntent(
            action="create",
            query="portátil gaming",
            max_price=Decimal(1000),
            price_unit="absolute",
        ),
        COMPLETE + " y avísame solo entre las 08:00 y las 23:00",
    )
    rule = intent_to_rule(validate_intent(intent))

    assert rule.include_merchants == ("Amazon", "PcComponentes")
    assert rule.exclude_merchants == ("AliExpress",)
    assert rule.notification_window is not None
    assert rule.notification_window.start == time(8, 0)
    assert rule.notification_window.end == time(23, 0)
    assert rule.notification_window.timezone == "Europe/Madrid"


def test_an_incomplete_schedule_asks_for_a_clarification():
    intent = AlertIntent(
        action="create",
        query="portátiles",
        max_price=Decimal(1000),
        price_unit="absolute",
        notify_window_start="08:00",
    )
    with pytest.raises(ValueError, match="horario"):
        intent_to_rule(validate_intent(intent))


def test_a_rule_without_shops_or_window_keeps_the_old_behaviour():
    rule = intent_to_rule(
        validate_intent(
            AlertIntent(
                action="create",
                query="portátiles",
                max_price=Decimal(1000),
                price_unit="absolute",
            )
        )
    )
    assert rule.include_merchants == ()
    assert rule.exclude_merchants == ()
    assert rule.notification_window is None


# --- Telegram, end to end --------------------------------------------------- #


CHAT_ID = "42"


class Translator:
    """Stands in for the provider: it returns the intent it was given."""

    def __init__(self, intent):
        self.intent = intent
        self.seen = []

    def interpret_alert(self, text):
        self.seen.append(text)
        return self.intent


class Controller(TelegramRuleController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.replies = []

    def send_message(self, text):
        self.replies.append(text)


def create_intent(**overrides):
    fields = {
        "action": "create",
        "query": "portátil gaming",
        "max_price": Decimal(1000),
        "price_unit": "absolute",
    }
    fields.update(overrides)
    return AlertIntent(**fields)


def process(controller, text, update_id=1):
    return controller.process_update(
        {"update_id": update_id, "message": {"chat": {"id": CHAT_ID}, "text": text}}
    )


def test_basic_unit_price_telegram_flow_persists_without_llm(tmp_path):
    path = tmp_path / "alerts.db"
    repository = DealRepository(path)
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=repository,
        translator=None,
    )

    reply = process(controller, "Quiero alertas de leche por debajo de 0.8€ el litro")
    assert "0,80 €/L" in reply
    row = repository.list_alert_rules(user_id=controller.current_user_id)[0]
    rule_id = row[0]
    assert repository.load_alert_rule(
        rule_id, user_id=controller.current_user_id
    ).constraints.max_price_per_liter == Decimal("0.8")

    repository.close()
    reopened = DealRepository(path)
    loaded = reopened.load_alert_rule(rule_id, user_id=controller.current_user_id)
    assert loaded is not None
    assert loaded.query == "leche"
    assert loaded.constraints.max_price_per_liter == Decimal("0.8")
    assert reopened.get_rule(rule_id, user_id=controller.current_user_id)[6] == 1


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("Quiero alertas de cerveza", "cerveza"),
        ("Avísame de leche", "leche"),
        ("Quiero alertas de mini PC", "mini PC"),
    ],
)
def test_product_only_telegram_alerts_are_created_without_constraints(
    tmp_path, text, query
):
    repository = DealRepository(tmp_path / f"{query.replace(' ', '_')}.db")
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=repository,
        translator=None,
    )

    reply = process(controller, text)
    row = repository.list_alert_rules(user_id=controller.current_user_id)[0]
    loaded = repository.load_alert_rule(row[0], user_id=controller.current_user_id)

    assert "Alerta creada" in reply
    assert query == row[1] == row[2]
    assert loaded is not None
    assert loaded.query == query
    assert loaded.product == query
    assert loaded.brand is None
    assert loaded.constraints == AlertConstraints()


@pytest.mark.parametrize("text", ["Quiero una alerta", "Crear alerta"])
def test_productless_creation_still_requests_clarification(tmp_path, text):
    repository = DealRepository(tmp_path / "incomplete.db")
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=repository,
        translator=None,
    )

    reply = process(controller, text)

    assert reply.startswith("Necesito una aclaración:")
    assert repository.list_alert_rules() == []


def test_the_confirmation_shows_the_shops_and_the_schedule(tmp_path):
    repository = DealRepository(tmp_path / "alerts.db")
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=repository,
        translator=Translator(create_intent()),
    )

    reply = process(
        controller,
        COMPLETE + " y avísame solo entre las 08:00 y las 23:00",
    )

    assert "🏪 Tiendas: Amazon, PcComponentes (excepto AliExpress)" in reply
    assert "⏱️ Avisos: 08:00–23:00 Europe/Madrid." in reply
    assert "no se pierden" in reply
    stored = repository.rule_by_id(repository.list_alert_rules()[0][0])
    assert stored.include_merchants == ("Amazon", "PcComponentes")
    assert stored.exclude_merchants == ("AliExpress",)
    assert stored.notification_window.end == time(23, 0)


def test_a_vague_period_is_answered_with_a_clarification(tmp_path):
    repository = DealRepository(tmp_path / "alerts.db")
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=repository,
        translator=Translator(create_intent()),
    )

    reply = process(controller, "Avísame de portátiles por la noche")

    assert reply.startswith("Necesito una aclaración:")
    assert "por la noche" in reply
    assert repository.list_alert_rules() == []


def test_an_alert_without_a_window_still_confirms_without_extra_lines():
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=None,
        translator=Translator(create_intent()),
    )
    assert controller._format(create_intent(), []) == (
        "✅ Alerta creada: portátil gaming por debajo de 1000,00 €"
    )


# --- The notification explains the shop ------------------------------------- #


def test_the_notification_names_the_shop_and_the_condition_it_checked():
    deal = Deal(
        "2012039",
        "ASUS TUF Gaming RTX 5070",
        "https://www.chollometro.com/ofertas/asus-tuf",
        Decimal(899),
        "PcComponentes",
        350,
        "generic",
        datetime(2026, 9, 22, 21, 27, 29, tzinfo=UTC),
        product_text="ASUS TUF Gaming RTX 5070",
    )
    rule = AlertRule(
        query="portátil gaming",
        include_merchants=("Amazon", "PcComponentes"),
        exclude_merchants=("AliExpress",),
        constraints=AlertConstraints(max_price=Decimal(1000)),
    )
    result = apply_rule(deal, interest_rule_from_alert(rule))
    assert result.accepted

    message = format_message(
        deal,
        MatchEvidence(
            rule_id=1,
            alert_text="Portátil gaming < 1000 €, Amazon o PcComponentes",
            query="portátil gaming",
            method="deterministic",
            checks=result.checks,
        ),
    )

    assert "🏪 Tienda: PcComponentes" in message
    assert "• Tienda permitida: PcComponentes" in message
    assert "• Sin tiendas excluidas: AliExpress" in message
    assert "🧠 Evaluación: deterministic" in message
