"""Provider-neutral structured alert rules."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AlertConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_price: Decimal | None = Field(default=None, ge=0)
    max_price_per_liter: Decimal | None = Field(default=None, ge=0)
    max_price_per_unit: Decimal | None = Field(default=None, ge=0)
    min_quantity: Decimal | None = Field(default=None, gt=0)
    min_volume_l: Decimal | None = Field(default=None, gt=0)
    min_temperature: int | None = None


class AlertRule(BaseModel):
    """A complete, persisted interpretation of a natural-language alert."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    product: str | None = None
    brand: str | None = None
    category: str | None = None
    store: str | None = None
    constraints: AlertConstraints = Field(default_factory=AlertConstraints)
    schema_version: int = Field(default=1, ge=1)

    @field_validator("query", "product", "brand", "category", "store")
    @classmethod
    def clean_text(cls, value):
        return value.strip() if value else value
