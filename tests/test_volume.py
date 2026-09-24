from decimal import Decimal

import pytest

from chollometro_alerts.config import InterestRule
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.models import Deal
from chollometro_alerts.volume import extract_volume


def test_volume_formats():
    expected = (6, Decimal(1), Decimal(6))
    for text in ("6 x 1L", "6x1 L", "Pack 6 x 1L", "12 x 1 litro"):
        assert extract_volume(text) in (expected, (12, Decimal(1), Decimal(12)))
    assert extract_volume("8 x 1,5L") == (8, Decimal("1.5"), Decimal("12.0"))
    assert extract_volume("6 botellas de 1L") == expected
    assert extract_volume("6 x 750ml") == (6, Decimal("0.750"), Decimal("4.500"))


def test_milk_price_per_liter_rules():
    rule = InterestRule("milk", max_price_per_liter=Decimal("0.75"))

    def deal(price):
        return Deal(
            "x",
            "Puleva ECO 6x1L",
            "https://x",
            Decimal(price),
            None,
            1,
            "milk",
            None,
            6,
            Decimal(1),
            Decimal(6),
            Decimal(price) / 6,
        )

    assert apply_rule(deal("4.27"), rule).reason == "ACCEPTED"
    assert apply_rule(deal("5.70"), rule).reason == "REJECTED_PRICE_PER_LITER"
    assert apply_rule(deal("5.40"), rule).reason == "REJECTED_PRICE_PER_LITER"
    unknown = Deal(
        "y", "Leche Puleva", "https://y", Decimal(1), None, 999, "milk", None
    )
    assert apply_rule(unknown, rule).reason == "REJECTED_UNKNOWN_VOLUME"


@pytest.mark.parametrize(
    "text",
    (
        "6 Pack repelentes",
        "8 unidades pilas",
        "12 rollos papel",
        "10 bombillas",
        "4 enchufes",
    ),
)
def test_pack_count_is_not_volume(text):
    assert extract_volume(text) is None
