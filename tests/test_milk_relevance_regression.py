from decimal import Decimal

from chollometro_alerts.config import InterestRule
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.models import Deal
from chollometro_alerts.pricing import PricingEngine
from chollometro_alerts.product import ProductExtraction, extract_product

MILK = InterestRule("leche", product_type="leche", max_price_per_liter=Decimal("0.79"))
LEGACY_MILK = InterestRule(
    "leche", query="leche", product_type="leche", max_price_per_liter=Decimal("0.79")
)


def evaluated(title, price, extraction=None, rule=MILK):
    deal = Deal(
        "id",
        title,
        "https://example.test/id",
        Decimal(price),
        "Amazon",
        125,
        "generic",
        None,
    )
    extraction = extraction or extract_product(title)
    return apply_rule(PricingEngine().evaluate(deal, extraction), rule)


def test_vevor_hydroponic_tower_is_not_milk_even_if_price_is_low():
    result = evaluated(
        "VEVOR Sistema de Cultivo Hidropónico, Torre Hidropónica Vertical de 10 Niveles con 50 Cápsulas, Bomba de Agua y Ruedas, Kit de Germinación",
        "67.64",
    )
    assert (result.accepted, result.reason) == (False, "REJECTED_PRODUCT")


def test_milk_one_liter_matches():
    assert evaluated("Leche entera 1 litro", "0.75").accepted


def test_real_pepsi_false_positive_is_rejected_by_legacy_product_relevance():
    title = (
        "8 BOTELLAS de Pepsi zero Refresco de cola con cero azúcar "
        "1,75 litros - CON VARIAS SUSCRIPCIONES €7,06"
    )
    result = evaluated(title, "7.64", rule=LEGACY_MILK)
    assert (result.accepted, result.reason) == (False, "REJECTED_PRODUCT")
    assert result.checks == ()


def test_legacy_milk_query_is_an_and_condition_with_unit_price():
    result = evaluated("Leche entera 6x1L", "4.20", rule=LEGACY_MILK)
    assert result.accepted
    assert [check.code for check in result.checks] == ["PRODUCT", "MAX_PRICE_PER_LITER"]


def test_common_non_milk_drinks_cannot_use_the_milk_unit_price():
    for title in ("Coca-Cola 12x330 ml", "Agua 6x1.5 L", "Cerveza 24x330 ml"):
        result = evaluated(title, "1.00", rule=LEGACY_MILK)
        assert (result.accepted, result.reason) == (False, "REJECTED_PRODUCT")


def test_repellent_pack_is_rejected_and_has_no_volume_or_unit_price():
    title = (
        "Repelente Ultrasónico de Plagas, 6 Pack Electrónico Repelente "
        "Mosquitos Control de Plagas, Repelente Ultrasónico Mosquitos para Interiores"
    )
    extraction = extract_product(title)
    deal = Deal(
        "repellent",
        title,
        "https://example.test/repellent",
        Decimal("6.99"),
        "Amazon",
        34,
        "generic",
        None,
    )
    priced = PricingEngine().evaluate(deal, extraction)
    result = apply_rule(priced, LEGACY_MILK)

    assert (result.accepted, result.reason) == (False, "REJECTED_PRODUCT")
    assert extraction.units is None
    assert extraction.total_volume_l is None
    assert priced.price_per_liter is None


def test_relevant_milk_without_volume_cannot_match_price_per_liter():
    extraction = extract_product("Pack leche entera")
    deal = Deal(
        "milk-no-volume",
        "Pack leche entera",
        "https://example.test/milk-no-volume",
        Decimal("4.20"),
        "Amazon",
        34,
        "generic",
        None,
    )
    priced = PricingEngine().evaluate(deal, extraction)
    result = apply_rule(priced, MILK)

    assert (result.accepted, result.reason) == (False, "REJECTED_UNKNOWN_VOLUME")
    assert extraction.total_volume_l is None
    assert priced.price_per_liter is None


def test_milk_pack_matches_at_seventy_cents_per_liter():
    result = evaluated("Leche 6x1L", "4.20")
    assert result.accepted
    assert result.checks[-1].code == "MAX_PRICE_PER_LITER"


def test_milk_pack_above_limit_does_not_match():
    assert not evaluated("Leche 6x1L", "6.00").accepted


def test_oil_and_detergent_do_not_match_milk():
    assert evaluated("Aceite 5L", "3.00").reason == "REJECTED_PRODUCT"
    assert evaluated("Detergente 3L", "1.00").reason == "REJECTED_PRODUCT"


def test_non_volume_numbers_do_not_become_liters():
    extraction = extract_product(
        "VEVOR torre de 10 niveles con 50 cápsulas, bomba de agua"
    )
    assert extraction.total_volume_l is None
    assert extraction.unit_volume_l is None


def test_llm_cannot_invent_volume_without_explicit_unit():
    extraction = extract_product(
        "Torre hidropónica de 10 niveles con 50 cápsulas",
        llm=lambda text: ProductExtraction(
            product_type="torre hidropónica", units=10, unit_volume_l=Decimal(1)
        ),
    )
    assert extraction.total_volume_l is None


def test_llm_cannot_turn_pack_count_into_liters():
    extraction = extract_product(
        "Repelente Ultrasónico 6 Pack",
        llm=lambda text: ProductExtraction(
            product_type="repelente",
            units=6,
            unit_volume_l=Decimal(1),
            total_volume_l=Decimal(6),
            extraction_source="llm",
        ),
    )
    assert extraction.units == 6
    assert extraction.unit_volume_l is None
    assert extraction.total_volume_l is None
