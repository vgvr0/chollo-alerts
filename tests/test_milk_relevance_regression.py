from decimal import Decimal

from chollometro_alerts.config import InterestRule
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.models import Deal
from chollometro_alerts.pricing import PricingEngine
from chollometro_alerts.product import ProductExtraction, extract_product

MILK = InterestRule("leche", product_type="leche", max_price_per_liter=Decimal("0.79"))


def evaluated(title, price, extraction=None):
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
    return apply_rule(PricingEngine().evaluate(deal, extraction), MILK)


def test_vevor_hydroponic_tower_is_not_milk_even_if_price_is_low():
    result = evaluated(
        "VEVOR Sistema de Cultivo Hidropónico, Torre Hidropónica Vertical de 10 Niveles con 50 Cápsulas, Bomba de Agua y Ruedas, Kit de Germinación",
        "67.64",
    )
    assert (result.accepted, result.reason) == (False, "REJECTED_PRODUCT")


def test_milk_one_liter_matches():
    assert evaluated("Leche entera 1 litro", "0.75").accepted


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
