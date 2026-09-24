"""Provider-neutral structured alert rules."""

from datetime import time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .categories import normalize_category
from .schedule import default_timezone, validate_timezone


class NotificationWindow(BaseModel):
    """When Telegram may be sent for this alert.

    The window controls **delivery**, not matching: a deal found outside the
    window is still a match and is delivered as soon as the window opens.
    """

    model_config = ConfigDict(extra="forbid")

    start: time = Field(
        description="Hora local de inicio de la ventana, formato HH:MM."
    )
    end: time = Field(
        description=(
            "Hora local de fin de la ventana, formato HH:MM. Puede ser menor "
            "que el inicio: 22:00 → 07:00 cruza medianoche."
        )
    )
    timezone: str = Field(
        default_factory=default_timezone,
        description=(
            "Timezone IANA de la ventana (por ejemplo 'Europe/Madrid'). "
            "Nunca interpretes las horas como UTC."
        ),
    )

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        return validate_timezone(value or default_timezone())


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
    temperature_min: float | None = Field(
        default=None,
        description=(
            "Temperatura mínima de Chollometro, en grados ('más de 500 "
            "grados', 'al menos 500°', 'no quiero chollos por debajo de 100 "
            "grados'). null si el mensaje no pide un mínimo. Los grados nunca "
            "son euros: un precio no rellena este campo."
        ),
    )
    temperature_max: float | None = Field(
        default=None,
        description=(
            "Temperatura máxima de Chollometro, en grados ('menos de 100 "
            "grados', 'como máximo 300°'). null si el mensaje no pide un "
            "máximo."
        ),
    )
    category_include: tuple[str, ...] = Field(default=())
    category_exclude: tuple[str, ...] = Field(default=())
    max_age_minutes: float | None = Field(default=None, gt=0)

    @field_validator("category_include", "category_exclude", mode="before")
    @classmethod
    def clean_categories(cls, value):
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        return tuple(
            normalize_category(item) for item in value if normalize_category(item)
        )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_temperature(cls, data):
        """Keep `min_temperature` (the original field name) readable.

        Rules persisted before the temperature range existed store their floor
        under `min_temperature`. It is renamed here instead of being kept as a
        second field, so one fact never has two homes.
        """
        if isinstance(data, dict) and "min_temperature" in data:
            data = dict(data)
            legacy = data.pop("min_temperature")
            if legacy is not None:
                data.setdefault("temperature_min", legacy)
        return data

    @model_validator(mode="after")
    def ordered_temperature_range(self):
        if (
            self.temperature_min is not None
            and self.temperature_max is not None
            and self.temperature_min > self.temperature_max
        ):
            raise ValueError(
                "la temperatura mínima no puede superar a la máxima "
                f"({self.temperature_min} > {self.temperature_max})"
            )
        return self

    @property
    def min_temperature(self) -> float | None:
        """Read-only alias of `temperature_min`, its original name."""
        return self.temperature_min


class AlertRule(BaseModel):
    """A complete, persisted interpretation of a natural-language alert."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(
        default=None,
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
    include_merchants: tuple[str, ...] = Field(
        default=(),
        description=(
            "Tiendas permitidas (allowed_merchants). Vacío = cualquier tienda "
            "salvo las excluidas. Con valores, el deal tiene que ser de una de "
            "ellas."
        ),
    )
    exclude_merchants: tuple[str, ...] = Field(
        default=(),
        description=(
            "Tiendas excluidas (excluded_merchants). Tienen prioridad sobre "
            "las permitidas."
        ),
    )
    notification_window: NotificationWindow | None = Field(
        default=None,
        description=(
            "Horario en el que se puede enviar Telegram para esta alerta. "
            "null = envío inmediato, como hasta ahora. El horario NO decide si "
            "el deal hace match: los matches de fuera del horario quedan "
            "pendientes y se envían al abrirse la ventana."
        ),
    )
    constraints: AlertConstraints = Field(default_factory=AlertConstraints)
    momentum_enabled: bool = Field(
        default=False, description="Opt-in temperature momentum condition."
    )
    momentum_window_minutes: int = Field(default=15, ge=5)
    minimum_temperature_velocity: float = Field(default=3.0, ge=0)
    schema_version: int = Field(default=1, ge=1)

    @field_validator("query", "product", "brand", "category", "store")
    @classmethod
    def clean_text(cls, value):
        return value.strip() if value else value

    @field_validator("include_merchants", "exclude_merchants", mode="before")
    @classmethod
    def clean_merchants(cls, value):
        """Accept one name or a list, and never store blank entries."""
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        return tuple(str(item).strip() for item in value if str(item).strip())
