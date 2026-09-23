from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .alert_rule import AlertConstraints, AlertRule, NotificationWindow
from .schedule import default_timezone, parse_time, validate_timezone


class AlertIntent(BaseModel):
    """Operational Telegram intent.

    The field descriptions are part of the JSON schema sent to the provider, so
    they define the contract the model has to fill: the semantics of
    `price_unit` are stated there instead of being left to the model's guess.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "update", "delete", "enable", "disable", "list"] = Field(
        description=(
            "create: alerta nueva. update: cambiar el precio de una alerta "
            "existente. delete/enable/disable: gestionar una alerta existente. "
            "list: listar las alertas guardadas."
        )
    )
    query: str | None = Field(
        default=None,
        description=(
            "Solo el término de búsqueda del producto, corto y sin relleno "
            "(por ejemplo 'zapatillas'). Nunca incluyas precio, moneda ni la "
            "frase completa del usuario."
        ),
    )
    product_type: str | None = Field(
        default=None,
        description=(
            "Tipo de producto en singular y sin adjetivos (por ejemplo "
            "'zapatillas'), o null si no se menciona."
        ),
    )
    brand: str | None = Field(
        default=None, description="Marca mencionada explícitamente, o null."
    )
    max_price: Decimal | None = Field(
        default=None,
        ge=0,
        description="Precio máximo mencionado, sin símbolo de moneda.",
    )
    price_unit: Literal["absolute", "liter", "kilogram", "unit"] | None = Field(
        default=None,
        description=(
            "'absolute' cuando el precio es el precio total de la oferta, que "
            "es el caso por defecto si el usuario no dice 'por unidad', 'por "
            "litro' ni 'por kilo'. 'unit' solo si dice explícitamente 'por "
            "unidad' o 'por ud'. 'liter' para 'por litro'. 'kilogram' para "
            "'por kilo' o 'por kilogramo'."
        ),
    )
    include_merchants: list[str] | None = Field(
        default=None,
        description=(
            "Tiendas permitidas, con los nombres tal y como los escribió el "
            "usuario (por ejemplo ['Amazon', 'PcComponentes']). null o [] si "
            "no menciona ninguna: entonces vale cualquier tienda salvo las "
            "excluidas."
        ),
    )
    exclude_merchants: list[str] | None = Field(
        default=None,
        description=(
            "Tiendas excluidas, con los nombres tal y como los escribió el "
            "usuario (por ejemplo ['AliExpress']). null o [] si no menciona "
            "ninguna. Tienen prioridad sobre las permitidas."
        ),
    )
    notify_window_start: str | None = Field(
        default=None,
        description=(
            "Hora local de inicio del horario de avisos en formato HH:MM "
            "(por ejemplo '08:00'), o null si el usuario no pide un horario. "
            "Solo acepta horas concretas: si dice 'por la noche' sin horas, "
            "deja los dos campos en null."
        ),
    )
    notify_window_end: str | None = Field(
        default=None,
        description=(
            "Hora local de fin del horario de avisos en formato HH:MM. Puede "
            "ser menor que el inicio (por ejemplo '22:00' → '07:00' cruza "
            "medianoche). null si no hay horario."
        ),
    )
    notify_timezone: str | None = Field(
        default=None,
        description=(
            "Timezone IANA del horario (por ejemplo 'Europe/Madrid'). null "
            "usa la configurada por defecto. Nunca interpretes las horas como "
            "UTC."
        ),
    )
    rule: AlertRule | None = Field(
        default=None,
        description=(
            "Déjalo en null: la regla estructurada la construye el sistema a "
            "partir de los campos anteriores."
        ),
    )

    @field_validator("query", "product_type", "brand")
    @classmethod
    def clean_text(cls, value):
        return value.strip() if value else value

    @field_validator("include_merchants", "exclude_merchants")
    @classmethod
    def clean_merchants(cls, value):
        """Keep the operator's spelling, drop the blanks, never keep an empty list."""
        if not value:
            return None
        names = [str(name).strip() for name in value if str(name).strip()]
        return names or None


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
    """Convert an operational Telegram intent to the canonical domain rule.

    The explicit intent fields are authoritative. A provider that also echoes a
    partially filled `rule` cannot silently drop the product, brand or price
    the user actually stated; `rule` is only used when the intent carries no
    textual target at all.
    """
    query = intent.query or intent.product_type or intent.brand
    if intent.rule is not None and not query:
        return intent.rule
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
        include_merchants=tuple(intent.include_merchants or ()),
        exclude_merchants=tuple(intent.exclude_merchants or ()),
        notification_window=notification_window(intent),
        constraints=constraints,
    )


def notification_window(intent: AlertIntent) -> NotificationWindow | None:
    """The schedule the intent really asks for, or None (= notify immediately).

    Only concrete hours are accepted: an intent that carries one of the two
    hours is an incomplete schedule and asks for a clarification instead of
    being silently turned into a window.
    """
    start, end = intent.notify_window_start, intent.notify_window_end
    if start is None and end is None:
        return None
    if not start or not end:
        raise ValueError("Falta una de las dos horas del horario")
    timezone = intent.notify_timezone or default_timezone()
    return NotificationWindow(
        start=parse_time(start),
        end=parse_time(end),
        timezone=validate_timezone(timezone),
    )
