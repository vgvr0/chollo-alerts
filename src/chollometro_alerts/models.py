from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from .categories import CategoryRef


@dataclass(frozen=True)
class Deal:
    deal_id: str
    title: str
    url: str
    price: Decimal | None
    merchant: str | None
    temperature: int | None
    category: str
    published_at: datetime | None
    units: int | None = None
    unit_volume_l: Decimal | None = None
    total_volume_l: Decimal | None = None
    price_per_liter: Decimal | None = None
    price_per_unit: Decimal | None = None
    product_extraction: object | None = None
    product_text: str = ""
    description: str = ""
    original_price: Decimal | None = None
    image: str | None = None
    source_query: str = ""
    unit_weight_kg: Decimal | None = None
    total_weight_kg: Decimal | None = None
    # Provider-specific fields the GraphQL feed can supply. They are additive:
    # the HTML parser leaves them unset and no rule consumes them yet.
    status: str | None = None
    is_expired: bool | None = None
    # Structured GraphQL groups.  The legacy `category` field remains intact
    # for HTML/old rules; new category rules use these stable provider facts.
    categories: tuple[CategoryRef, ...] = ()


def format_amount(value: Decimal | int | None) -> str:
    """Spanish money rendering of a price the deal or a rule really carries.

    Whole amounts stay whole (`15` -> `15 €`) and a fractional one is shown with
    the comma separator and at most two decimals (`7.95` -> `7,95 €`), so the
    notification can quote the exact number that was compared.
    """
    if value is None:
        return "N/D"
    value = value if isinstance(value, Decimal) else Decimal(value)
    if value == value.to_integral_value():
        return f"{format(value.normalize(), 'f')} €"
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:.2f}".replace(".", ",") + " €"


def format_number(value: Decimal | float | None) -> str:
    """Plain Spanish number for quantities and volumes: `1.98` -> `1,98`."""
    if value is None:
        return "N/D"
    if isinstance(value, float):
        # `Decimal(100.1)` would expose the binary representation of the float;
        # the shortest decimal that round-trips is what the rule really asked for.
        value = Decimal(str(value))
    elif not isinstance(value, Decimal):
        value = Decimal(value)
    return format(value.normalize(), "f").replace(".", ",")
