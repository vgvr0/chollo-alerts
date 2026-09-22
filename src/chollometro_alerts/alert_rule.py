"""Provider-neutral structured alert rules."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AlertConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_price: Decimal | None = Field(
        default=None,
        ge=0,
        description=(
            "Precio total máximo de la oferta. Es la restricción por defecto "
            "cuando el precio no se expresa por unidad, por litro ni por kilo."
        ),
    )
    max_price_per_liter: Decimal | None = Field(
        default=None,
        ge=0,
        description="Solo si el precio se expresa explícitamente por litro.",
    )
    max_price_per_unit: Decimal | None = Field(
        default=None,
        ge=0,
        description=(
            "Solo si el precio se expresa explícitamente por unidad o por ud. "
            "No la uses para un precio total de la oferta."
        ),
    )
    min_quantity: Decimal | None = Field(default=None, gt=0)
    min_volume_l: Decimal | None = Field(default=None, gt=0)
    min_temperature: int | None = None


class AlertRule(BaseModel):
    """A complete, persisted interpretation of a natural-language alert."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        description=(
            "Solo el término de búsqueda del producto, corto y sin relleno "
            "(por ejemplo 'zapatillas'). Nunca incluyas precio, moneda ni la "
            "frase completa del usuario."
        ),
    )
    product: str | None = Field(
        default=None,
        description=(
            "Tipo de producto en singular y sin adjetivos (por ejemplo "
            "'zapatillas'), o null si no se menciona."
        ),
    )
    brand: str | None = Field(
        default=None, description="Marca mencionada explícitamente, o null."
    )
    category: str | None = None
    store: str | None = None
    constraints: AlertConstraints = Field(default_factory=AlertConstraints)
    schema_version: int = Field(default=1, ge=1)

    @field_validator("query", "product", "brand", "category", "store")
    @classmethod
    def clean_text(cls, value):
        return value.strip() if value else value
