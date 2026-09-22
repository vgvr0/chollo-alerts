from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


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
