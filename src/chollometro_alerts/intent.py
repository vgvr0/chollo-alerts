from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .alert_rule import AlertConstraints, AlertRule


class AlertIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "update", "delete", "enable", "disable", "list"]
    query: str | None = None
    product_type: str | None = None
    brand: str | None = None
    max_price: Decimal | None = Field(default=None, ge=0)
    price_unit: Literal["absolute", "liter", "kilogram", "unit"] | None = None
    rule: AlertRule | None = None

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


def intent_to_rule(intent: AlertIntent) -> AlertRule:
    """Convert an operational Telegram intent to the canonical domain rule."""
    if intent.rule is not None:
        return intent.rule
    query = intent.query or intent.product_type or intent.brand
    if not query:
        raise ValueError("Falta el producto o la marca")
    constraints = AlertConstraints()
    if intent.max_price is not None:
        kwargs = {"max_price": intent.max_price}
        if intent.price_unit == "liter":
            kwargs = {"max_price_per_liter": intent.max_price}
        elif intent.price_unit == "unit":
            kwargs = {"max_price_per_unit": intent.max_price}
        constraints = AlertConstraints(**kwargs)
    return AlertRule(
        query=query,
        product=intent.product_type,
        brand=intent.brand,
        constraints=constraints,
    )
