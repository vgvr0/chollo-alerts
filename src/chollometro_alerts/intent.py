from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AlertIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "update", "delete", "enable", "disable", "list"]
    query: str | None = None
    product_type: str | None = None
    brand: str | None = None
    max_price: Decimal | None = Field(default=None, ge=0)
    price_unit: Literal["liter", "unit"] | None = None

    @field_validator("query", "product_type", "brand")
    @classmethod
    def clean_text(cls, value):
        return value.strip() if value else value


def validate_intent(intent: AlertIntent) -> AlertIntent:
    if intent.action == "list":
        return intent
    if not (intent.query or intent.product_type or intent.brand):
        raise ValueError("Falta el producto o la marca")
    if intent.action in {"create", "update"} and (
        intent.max_price is None or intent.price_unit is None
    ):
        raise ValueError("Falta el precio máximo y su unidad")
    return intent
