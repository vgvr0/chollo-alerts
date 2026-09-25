from decimal import Decimal

import pytest

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.evaluation import interest_rule_from_alert
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.intent import AlertIntent, intent_to_rule, validate_intent
from chollometro_alerts.models import Deal
from chollometro_alerts.product import ProductExtraction
from chollometro_alerts.telegram_rules import TelegramRuleController


def deal(title, *, price="1.00", extraction=None, merchant="Amazon"):
    return Deal(
        deal_id=title,
        title=title,
        url="https://example.test/deal",
        price=Decimal(price) if price is not None else None,
        merchant=merchant,
        temperature=10,
        category="generic",
        published_at=None,
        product_extraction=extraction,
    )


@pytest.mark.parametrize(
    "query,product_type,brand",
    [
        ("Fairy", None, "Fairy"),
        ("LEGO", None, "LEGO"),
        ("zapatillas ASICS", "zapatillas", "ASICS"),
    ],
)
def test_specific_alerts_are_valid_without_quantitative_constraints(
    query, product_type, brand
):
    intent = validate_intent(
        AlertIntent(
            action="create", query=query, product_type=product_type, brand=brand
        )
    )
    rule = intent_to_rule(intent)
    assert rule.constraints == AlertConstraints()


@pytest.mark.parametrize("query", ["quiero ofertas", "cosas baratas", "chollos"])
def test_generic_alerts_still_require_clarification(query):
    with pytest.raises(ValueError, match="específico"):
        validate_intent(AlertIntent(action="create", query=query))


def test_fairy_query_only_matching_is_relevant_and_has_no_price_or_temperature_check():
    rule = interest_rule_from_alert(AlertRule(query="Fairy"))
    result = apply_rule(deal("Lavavajillas Fairy", price="0.01"), rule)
    assert result.accepted
    assert [check.code for check in result.checks] == ["RELEVANCE"]


def test_fairy_does_not_match_ariel_or_unrelated_cheap_products():
    rule = interest_rule_from_alert(AlertRule(query="Fairy"))
    assert apply_rule(deal("Detergente Ariel", price="0.01"), rule).reason == (
        "REJECTED_RELEVANCE"
    )
    assert apply_rule(
        deal("Producto de limpieza genérico", price="0.01"), rule
    ).reason == ("REJECTED_RELEVANCE")


def test_product_only_beer_alert_matches_beer_and_rejects_unrelated_cheap_deals():
    rule = interest_rule_from_alert(
        intent_to_rule(
            validate_intent(
                AlertIntent(action="create", query="cerveza", product_type="cerveza")
            )
        )
    )
    assert apply_rule(
        deal(
            "Pack cerveza Mahou",
            price="1.00",
            extraction=ProductExtraction(product_type="cerveza"),
        ),
        rule,
    ).accepted
    assert apply_rule(deal("Coca-Cola 12x330 ml", price="0.01"), rule).reason == (
        "REJECTED_PRODUCT"
    )


def test_relevance_only_listing_and_creation_do_not_invent_price():
    controller = TelegramRuleController(
        bot_token="token", authorized_chat_id="1", repository=None, translator=None
    )
    intent = AlertIntent(action="create", query="Fairy", brand="Fairy")
    reply = controller._format(intent, [])
    assert reply == "✅ Alerta creada: Fairy\n🔎 Cualquier oferta nueva relevante"


def test_existing_price_alert_remains_quantitative():
    rule = interest_rule_from_alert(
        AlertRule(
            query="leche",
            constraints=AlertConstraints(max_price_per_liter=Decimal("0.79")),
        )
    )
    result = apply_rule(deal("Leche", price="0.70"), rule)
    assert result.reason == "REJECTED_UNKNOWN_VOLUME"
