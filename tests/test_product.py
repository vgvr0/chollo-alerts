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
        ("Pack de cerveza Mahou 6x330ml", (6, Decimal("0.33"), Decimal("1.98"))),
        ("Pack Mahou 6 x 330 ml", (6, Decimal("0.33"), Decimal("1.98"))),
        (
            "24 latas Estrella Galicia de 33 cl",
            (24, Decimal("0.33"), Decimal("7.92")),
        ),
        ("Pack 12 botellas cerveza 25cl", (12, Decimal("0.25"), Decimal("3.00"))),
        (
            "6 bricks de leche Pascual de 1L",
            (6, Decimal(1), Decimal(6)),
        ),
        ("Leche entera 1 litro", (1, Decimal(1), Decimal(1))),
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


def test_ambiguous_product_without_volume_does_not_invent_values():
    result = extract_product("Cerveza artesanal sin formato indicado")
    assert result.units is None
    assert result.unit_volume_l is None
    assert result.total_volume_l is None
