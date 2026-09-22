from decimal import Decimal

from chollometro_alerts.config import InterestRule
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.models import Deal
from chollometro_alerts.pricing import PricingEngine
from chollometro_alerts.product import ProductExtraction, extract_product


def quantity(count):
    return ProductExtraction(units=count) if count is not None else ProductExtraction()


def priced(price, extraction, *, temperature=120, title="Pack Coca-Cola 24 latas"):
    deal = Deal(
        "coca-24",
        title,
        "https://example.test/coca-24",
        Decimal(price) if price is not None else None,
        "Carrefour",
        temperature,
        "generic",
        None,
    )
    return PricingEngine().evaluate(deal, extraction)


def test_pricing_engine_derives_price_per_unit_from_price_and_quantity():
    deal = priced("10.80", quantity(24))
    assert deal.units == 24
    assert deal.price_per_unit == Decimal("0.45")
    assert deal.price_per_unit * deal.units == deal.price


def test_deterministic_extraction_feeds_price_per_unit():
    extraction = extract_product("Pack Coca-Cola 24 latas 33 cl")
    assert (extraction.units, extraction.total_volume_l) == (24, Decimal("7.92"))
    deal = priced("10.80", extraction)
    assert deal.price_per_unit == Decimal("0.45")
    assert deal.price_per_liter == Decimal("10.80") / Decimal("7.92")


def test_max_price_per_unit_match():
    result = apply_rule(
        priced("10.80", quantity(24)),
        InterestRule("generic", max_price_per_unit=Decimal("0.50")),
    )
    assert result.accepted
    assert result.reason == "ACCEPTED"


def test_max_price_per_unit_reject():
    result = apply_rule(
        priced("10.80", quantity(24)),
        InterestRule("generic", max_price_per_unit=Decimal("0.40")),
    )
    assert not result.accepted
    assert result.reason == "REJECTED_PRICE_PER_UNIT"


def test_max_price_per_unit_boundary_keeps_existing_exclusive_semantics():
    # 24 units at 12.00 EUR is exactly 0.50 EUR per unit.
    deal = priced("12.00", quantity(24))
    assert deal.price_per_unit == Decimal("0.50")
    rule = InterestRule("generic", max_price_per_unit=Decimal("0.50"))
    assert not apply_rule(deal, rule).accepted
    assert apply_rule(deal, rule).reason == "REJECTED_PRICE_PER_UNIT"
    # The project already rejects equality for max_price / max_price_per_liter,
    # so max_price_per_unit is not looser than its sibling constraints.
    absolute = InterestRule("generic", max_price=Decimal("12.00"))
    assert apply_rule(deal, absolute).reason == "REJECTED_PRICE"
    assert apply_rule(priced("11.99", quantity(24)), rule).accepted


def test_max_price_per_unit_requires_a_reliable_quantity():
    deal = priced("10.80", quantity(None), title="Coca-Cola Zero pack familiar")
    assert deal.units is None
    # Quantity is never assumed to be 1, so 10.80 EUR is not compared to 0.50.
    assert deal.price_per_unit is None
    result = apply_rule(
        deal, InterestRule("generic", max_price_per_unit=Decimal("0.50"))
    )
    assert not result.accepted
    assert result.reason == "REJECTED_UNKNOWN_QUANTITY"


def test_max_price_per_unit_combines_with_other_constraints():
    rule = InterestRule(
        "generic",
        max_price_per_unit=Decimal("0.50"),
        min_quantity=Decimal(24),
        min_temperature=100,
    )
    assert apply_rule(priced("10.80", quantity(24), temperature=100), rule).accepted
    assert (
        apply_rule(priced("13.20", quantity(24)), rule).reason
        == "REJECTED_PRICE_PER_UNIT"
    )
    assert apply_rule(priced("5.40", quantity(12)), rule).reason == "REJECTED_QUANTITY"
    assert (
        apply_rule(priced("10.80", quantity(24), temperature=99), rule).reason
        == "REJECTED_TEMPERATURE"
    )
    assert (
        apply_rule(priced("5.40", quantity(None)), rule).reason
        == "REJECTED_UNKNOWN_QUANTITY"
    )


def test_min_volume_constraint_uses_the_known_total_volume():
    deal = priced("5.00", extract_product("Pack 6 x 1L"), title="Pack 6 x 1L")
    assert deal.total_volume_l == Decimal(6)
    assert apply_rule(deal, InterestRule("generic", min_volume_l=Decimal(6))).accepted
    assert (
        apply_rule(deal, InterestRule("generic", min_volume_l=Decimal(7))).reason
        == "REJECTED_VOLUME"
    )
    assert apply_rule(deal, InterestRule("generic", min_volume_l=Decimal(1))).accepted


def test_legacy_price_constraints_keep_working_alongside_price_per_unit():
    deal = priced("10.80", quantity(24))
    assert apply_rule(deal, InterestRule("generic", max_price=Decimal(11))).accepted
    assert (
        apply_rule(deal, InterestRule("generic", max_price=Decimal("10.80"))).reason
        == "REJECTED_PRICE"
    )
    # No constraints means no rejection, as before.
    assert apply_rule(deal, InterestRule("generic")).accepted
    assert (
        apply_rule(deal, InterestRule("generic", max_price_per_liter=Decimal(1))).reason
        == "REJECTED_UNKNOWN_VOLUME"
    )
