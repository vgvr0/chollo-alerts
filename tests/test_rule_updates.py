"""Canonical update path: Telegram -> AlertIntent(update) -> AlertRule -> DB.

Rule #2 of the local database was persisted before `price_unit = "absolute"`
existed, so its legacy `price_unit = "unit"` column turns every absolute price
into a price per unit. These tests pin the supported way to correct it and the
price semantics of the natural-language contract that feeds it.
"""

from decimal import Decimal

import pytest

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.intent import AlertIntent, intent_to_rule, validate_intent
from chollometro_alerts.models import Deal
from chollometro_alerts.product import ProductExtraction
from chollometro_alerts.replay import ReplayDecision, ReplayEngine
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.telegram_rules import TelegramRuleController

CHAT_ID = "42"


class Translator:
    """Stands in for the provider: it returns the intent captured live."""

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


def intent(**overrides):
    fields = {
        "action": "create",
        "query": "zapatillas",
        "product_type": "zapatillas",
        "brand": "ASICS",
        "max_price": Decimal(200),
        "price_unit": "absolute",
    }
    fields.update(overrides)
    return AlertIntent(**fields)


def insert_legacy_absolute_shoe_rule(repository):
    """The exact shape of persisted rule #2: no structured rule, unit column."""
    repository.db.execute(
        """INSERT INTO alert_rules
        (query,product_type,brand,max_price,price_unit,enabled,state,created_at,updated_at)
        VALUES ('zapatillas','zapatillas','ASICS','200','unit',1,'ACTIVE','x','x')"""
    )
    repository.db.commit()
    return repository.db.execute("SELECT last_insert_rowid()").fetchone()[0]


def store_deal(repository, deal_id, title, price, extraction):
    deal = Deal(
        deal_id,
        title,
        f"https://example.test/{deal_id}",
        Decimal(price),
        "Zalando",
        65,
        "generic",
        None,
        product_text=title,
    )
    assert repository.upsert(deal)
    repository.save_extraction(deal_id, extraction.model_dump(mode="json"))


# --- Natural language contract ----------------------------------------------


def test_natural_language_absolute_price_is_not_a_price_per_unit():
    rule = intent_to_rule(validate_intent(intent()))
    assert rule.constraints.max_price == Decimal(200)
    assert rule.constraints.max_price_per_unit is None
    assert rule.constraints.max_price_per_liter is None


def test_natural_language_absolute_price_keeps_its_legacy_column_semantics(tmp_path):
    """`rule_from_row` must render "absolute" as max_price, never per unit."""
    repository = DealRepository(tmp_path / "rules.sqlite3")
    row = (2, "zapatillas", "zapatillas", "ASICS", "200", "absolute", 1, None)
    rule = repository.rule_from_row(row)
    assert rule.constraints.max_price == Decimal(200)
    assert rule.constraints.max_price_per_unit is None


def test_natural_language_unit_price_still_produces_price_per_unit():
    rule = intent_to_rule(
        validate_intent(
            intent(
                query="Coca-Cola",
                product_type="refresco",
                brand="Coca-Cola",
                max_price=Decimal("0.50"),
                price_unit="unit",
            )
        )
    )
    assert rule.constraints.max_price_per_unit == Decimal("0.50")
    assert rule.constraints.max_price is None


def test_natural_language_liter_price_still_produces_price_per_liter():
    rule = intent_to_rule(
        validate_intent(intent(max_price=Decimal("0.79"), price_unit="liter"))
    )
    assert rule.constraints.max_price_per_liter == Decimal("0.79")
    assert rule.constraints.max_price is None


def test_the_explicit_intent_fields_win_over_a_provider_echoed_rule():
    echoed = AlertRule(
        query="zapatillas",
        product=None,
        constraints=AlertConstraints(max_price=Decimal(200)),
    )
    rule = intent_to_rule(
        validate_intent(intent(rule=echoed)),
    )
    assert rule.product == "zapatillas"
    assert rule.brand == "ASICS"


def test_a_missing_price_unit_is_still_an_ambiguous_intent():
    with pytest.raises(ValueError):
        validate_intent(intent(price_unit=None))


# --- The supported correction of rule #2 ------------------------------------


def test_telegram_update_rewrites_the_rule_to_an_absolute_price(tmp_path):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    rule_id = insert_legacy_absolute_shoe_rule(repository)
    store_deal(
        repository,
        "2011041",
        "Zapatillas Asics GEL-QUANTUM 360 VIII",
        "63.00",
        ProductExtraction(
            product_type="Zapatillas", brand="Asics", extraction_source="llm"
        ),
    )
    store_deal(
        repository,
        "2011781",
        "ASICS NOVABLAST 6 - Zapatillas running asfalto. (Última versión)",
        "108.75",
        ProductExtraction(
            product_type="Zapatillas running asfalto",
            brand="ASICS",
            extraction_source="llm",
        ),
    )
    assert repository.rule_by_id(rule_id).constraints.max_price_per_unit == Decimal(200)

    before = ReplayEngine(repository).replay(rule_id)
    assert (before.matched, before.not_evaluable) == (0, 2)

    text = "Cambia la alerta 2 para avisarme de zapatillas ASICS por menos de 200 €"
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=repository,
        translator=Translator(intent(action="update")),
    )
    reply = controller.process_update(
        {"update_id": 1, "message": {"chat": {"id": CHAT_ID}, "text": text}}
    )

    assert reply == "✅ Alerta actualizada: zapatillas por debajo de 200,00 €"
    updated = repository.rule_by_id(rule_id)
    assert updated.constraints.max_price == Decimal(200)
    assert updated.constraints.max_price_per_unit is None
    assert updated.product == "zapatillas"
    assert updated.brand == "ASICS"
    # The legacy columns and the structured rule stay in sync.
    assert repository.get_rule(rule_id)[4:6] == ("200", "absolute")

    after = ReplayEngine(repository).replay(rule_id)
    assert (after.matched, after.rejected, after.not_evaluable) == (2, 0, 0)
    assert {entry.deal_id: entry.decision for entry in after.results} == {
        "2011041": ReplayDecision.MATCH,
        "2011781": ReplayDecision.MATCH,
    }


def test_the_update_reply_reports_the_price_dimension_it_really_applies(tmp_path):
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=DealRepository(tmp_path / "rules.sqlite3"),
        translator=Translator(intent()),
    )
    assert controller._format(intent(), []) == (
        "✅ Alerta creada: zapatillas por debajo de 200,00 €"
    )
    assert controller._format(intent(price_unit="unit"), []) == (
        "✅ Alerta creada: zapatillas por debajo de 200,00 €/ud"
    )
    assert controller._format(intent(price_unit="liter"), []) == (
        "✅ Alerta creada: zapatillas por debajo de 200,00 €/L"
    )
    assert controller._format(intent(price_unit="kilogram"), []) == (
        "✅ Alerta creada: zapatillas por debajo de 200,00 €/kg"
    )
