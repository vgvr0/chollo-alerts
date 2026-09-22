from decimal import Decimal

import pytest

from chollometro_alerts.product import extract_product


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Pack Mahou 6x1L", (6, Decimal(1), Decimal(6))),
        ("Agua mineral, pack 6 x 1 L", (6, Decimal(1), Decimal(6))),
        ("Caja de 6 litros de leche", (6, Decimal(1), Decimal(6))),
        ("6 briks de 1 litro de Pascual", (6, Decimal(1), Decimal(6))),
        ("6 botellas de 1L", (6, Decimal(1), Decimal(6))),
        ("Pack 12x330ml", (12, Decimal("0.330"), Decimal("3.960"))),
        ("24 latas de 33cl", (24, Decimal("0.33"), Decimal("7.92"))),
    ],
)
def test_product_volume_variants(text, expected):
    result = extract_product(text)
    assert (result.units, result.unit_volume_l, result.total_volume_l) == expected
    assert result.extraction_source == "deterministic"


def test_llm_completes_missing_fields_and_is_validated():
    calls = []

    def llm(text):
        calls.append(text)
        return {
            "product_type": "café",
            "brand": "Marca",
            "variant": None,
            "units": None,
            "unit_volume_l": None,
            "total_volume_l": None,
            "confidence": 0.7,
        }

    result = extract_product("Café molido natural", llm)
    assert result.product_type == "café"
    assert result.brand == "Marca"
    assert result.extraction_source == "llm"
    assert len(calls) == 1


def test_high_confidence_volume_does_not_call_llm():
    def fail(_):
        raise AssertionError("LLM must not be called")

    assert extract_product("Pack 12x330ml", fail).total_volume_l == Decimal("3.960")
