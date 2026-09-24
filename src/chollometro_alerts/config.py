import math
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_project_dotenv() -> None:
    """Load the project file unless tests/operators explicitly disable dotenv."""
    if os.getenv("PYTHON_DOTENV_DISABLED", "").strip().casefold() != "true":
        load_dotenv(PROJECT_ROOT / ".env")


class ConfigurationError(ValueError):
    """Invalid application configuration, detected before processing deals."""


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str
    authorized_chat_id: str

    @classmethod
    def from_env(cls):
        load_project_dotenv()
        missing = [
            name
            for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
            if not os.getenv(name, "").strip()
        ]
        if missing:
            raise ConfigurationError("faltan " + ", ".join(missing))
        return cls(
            bot_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
            authorized_chat_id=os.environ["TELEGRAM_CHAT_ID"].strip(),
        )


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool = False
    provider: str = "deepseek"
    api_key: str = ""
    model: str = "deepseek-flash"
    timeout: float = 20
    retries: int = 2

    @classmethod
    def from_env(cls):
        load_project_dotenv()
        enabled = os.getenv("LLM_ENABLED", "false").strip().casefold()
        if enabled not in {"true", "false"}:
            raise ConfigurationError("LLM_ENABLED debe ser true o false")
        if enabled == "false":
            return cls()
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        provider = os.getenv("LLM_PROVIDER", "deepseek").strip().casefold()
        if provider != "deepseek":
            raise ConfigurationError(f"LLM_PROVIDER no soportado: {provider}")
        if not key:
            raise ConfigurationError(
                "DEEPSEEK_API_KEY es obligatoria cuando LLM_ENABLED=true"
            )
        try:
            timeout = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "20"))
            retries = int(os.getenv("DEEPSEEK_MAX_RETRIES", "2"))
        except ValueError as exc:
            raise ConfigurationError(
                "DEEPSEEK_TIMEOUT_SECONDS y DEEPSEEK_MAX_RETRIES deben ser numéricos"
            ) from exc
        if not math.isfinite(timeout) or timeout <= 0 or retries < 0:
            raise ConfigurationError(
                "DEEPSEEK_TIMEOUT_SECONDS debe ser positivo y DEEPSEEK_MAX_RETRIES >= 0"
            )
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
        if not model:
            raise ConfigurationError("DEEPSEEK_MODEL no puede estar vacío")
        return cls(True, provider, key, model, timeout, retries)


def _float(name, default):
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError as exc:
        raise ConfigurationError(f"{name} debe ser numérico") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{name} debe ser un número positivo")
    return value


def _non_negative_int(name, default):
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError as exc:
        raise ConfigurationError(f"{name} debe ser un entero") from exc
    if value < 0:
        raise ConfigurationError(f"{name} debe ser >= 0")
    return value


# Minutes between two operational alerts of the same kind (per error type and
# component). Documented in `.env.example` since the beginning; read from the
# environment here so the operator can actually change it.
ERROR_ALERT_COOLDOWN_ENV = "ERROR_ALERT_COOLDOWN_MINUTES"
DEFAULT_ERROR_ALERT_COOLDOWN_MINUTES = 60


def error_alert_cooldown_minutes() -> int:
    """Cooldown of the operational alerts, in minutes (60 by default).

    Zero would turn every failure into its own alert, which is exactly the
    spam the cooldown exists to prevent, so the minimum is one minute.
    """
    load_project_dotenv()
    try:
        value = int(
            os.getenv(ERROR_ALERT_COOLDOWN_ENV, "")
            or DEFAULT_ERROR_ALERT_COOLDOWN_MINUTES
        )
    except ValueError as exc:
        raise ConfigurationError(
            f"{ERROR_ALERT_COOLDOWN_ENV} debe ser un entero"
        ) from exc
    if value < 1:
        raise ConfigurationError(f"{ERROR_ALERT_COOLDOWN_ENV} debe ser >= 1")
    return value


@dataclass(frozen=True)
class ChollometroSettings:
    """Centralised HTTP policy for every request to Chollometro.

    One timeout, one bounded retry budget and one exponential backoff source,
    so no request can wait forever and no call site invents its own numbers.
    """

    timeout: float = 20.0
    retries: int = 2
    backoff_seconds: float = 0.5
    max_backoff_seconds: float = 30.0

    @classmethod
    def from_env(cls):
        load_project_dotenv()
        timeout = _float("CHOLLOMETRO_TIMEOUT_SECONDS", 20.0)
        retries = _non_negative_int("CHOLLOMETRO_MAX_RETRIES", 2)
        backoff = _float("CHOLLOMETRO_RETRY_BACKOFF_SECONDS", 0.5)
        max_backoff = _float("CHOLLOMETRO_MAX_RETRY_BACKOFF_SECONDS", 30.0)
        return cls(timeout, retries, backoff, max_backoff)


# Window behaviour of `threads(filter: {}, limit: N)`, measured against the live
# endpoint (2026-09-23):
#
#   * `limit` omitted (or null) -> 30 threads: the server's own default window.
#   * 1 <= limit <= 20          -> exactly that many threads.
#   * limit >= 21               -> silently clamped to 20, with no GraphQL error.
#
# A single request therefore returns at most 30 threads, and the widest legal
# window is the default one: asking for `limit: 30` is NOT the same request as
# omitting the argument, it returns 20.
GRAPHQL_DEFAULT_WINDOW = 30
GRAPHQL_MAX_EXPLICIT_WINDOW = 20


@dataclass(frozen=True)
class GraphQLFeedSettings:
    """Policy of the GraphQL discovery feed (one fetch per scan cycle).

    The endpoint is the site's internal Pepper GraphQL API. `window_limit` is
    the whole visibility window of the scanner, so promotions that fall out of
    it are never evaluated. It is `None` by default, which means the request
    carries no `limit` argument at all: the live endpoint then answers with its
    own default window of 30 threads, the widest answer it gives. Setting it to
    1..20 asks for that many explicitly; a larger value is rejected because the
    server would silently clamp it to 20.
    """

    enabled: bool = True
    window_limit: int | None = None
    path: str = "/graphql"

    @classmethod
    def from_env(cls):
        load_project_dotenv()
        raw = os.getenv("CHOLLOMETRO_GRAPHQL_DISCOVERY", "true").strip().casefold()
        if raw not in {"true", "false"}:
            raise ConfigurationError(
                "CHOLLOMETRO_GRAPHQL_DISCOVERY debe ser true o false"
            )
        enabled = raw == "true"
        window = _window_limit()
        path = os.getenv("CHOLLOMETRO_GRAPHQL_PATH", "/graphql").strip() or "/graphql"
        if not path.startswith("/"):
            raise ConfigurationError("CHOLLOMETRO_GRAPHQL_PATH debe empezar por /")
        return cls(enabled, window, path)


def _window_limit():
    """`None` (no `limit` argument) unless the operator asked for 1..20."""
    raw = os.getenv("CHOLLOMETRO_GRAPHQL_WINDOW_LIMIT", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            "CHOLLOMETRO_GRAPHQL_WINDOW_LIMIT debe ser un entero"
        ) from exc
    if not 1 <= value <= GRAPHQL_MAX_EXPLICIT_WINDOW:
        raise ConfigurationError(
            "CHOLLOMETRO_GRAPHQL_WINDOW_LIMIT debe estar entre 1 y "
            f"{GRAPHQL_MAX_EXPLICIT_WINDOW}: el endpoint recorta a 20 cualquier "
            "limit mayor, y omitirlo devuelve la ventana máxima de "
            f"{GRAPHQL_DEFAULT_WINDOW}"
        )
    return value


def _list(name):
    return tuple(
        x.strip().casefold() for x in os.getenv(name, "").split(",") if x.strip()
    )


@dataclass(frozen=True)
class InterestRule:
    category: str
    max_price: Decimal | None = None
    # Chollometro temperature window, in degrees. `temperature_min` is the old
    # `min_temperature` (kept as an accepted alias below): a deal must reach the
    # floor, and must not exceed the ceiling when one is configured.
    temperature_min: float | None = None
    temperature_max: float | None = None
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    include_merchants: tuple[str, ...] = ()
    exclude_merchants: tuple[str, ...] = ()
    max_price_per_liter: Decimal | None = None
    max_price_per_kilogram: Decimal | None = None
    max_price_per_unit: Decimal | None = None
    min_quantity: Decimal | None = None
    min_volume_l: Decimal | None = None
    product_type: str | None = None
    brand: str | None = None
    query: str | None = None
    category_include: tuple[str, ...] = ()
    category_exclude: tuple[str, ...] = ()
    max_age_minutes: float | None = None


_INTEREST_RULE_ALIASES = {"min_temperature": "temperature_min"}
_interest_rule_init = InterestRule.__init__


def _interest_rule_init_with_legacy_aliases(self, *args, **kwargs):
    """Accept the original field names next to the canonical ones.

    `min_temperature` was the only temperature dimension of a rule before the
    range existed. Existing configuration, call sites and tests keep using it;
    the canonical field is `temperature_min`.
    """
    for legacy, canonical in _INTEREST_RULE_ALIASES.items():
        if legacy in kwargs:
            kwargs.setdefault(canonical, kwargs.pop(legacy))
    _interest_rule_init(self, *args, **kwargs)


InterestRule.__init__ = _interest_rule_init_with_legacy_aliases  # type: ignore[method-assign]


@property  # type: ignore[misc]
def _temperature_min_alias(self):
    """`rule.min_temperature`, the original read name of the floor."""
    return self.temperature_min


InterestRule.min_temperature = _temperature_min_alias  # type: ignore[attr-defined]


def _decimal(name):
    value = os.getenv(name)
    if not value:
        return None
    try:
        parsed = Decimal(value)
    except ArithmeticError as exc:
        raise ConfigurationError(f"{name} debe ser un número decimal válido") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ConfigurationError(f"{name} debe ser un decimal finito no negativo")
    return parsed


def load_rules():
    load_project_dotenv()
    return {
        "milk": InterestRule(
            category="milk",
            max_price=_decimal("MILK_MAX_PRICE"),
            max_price_per_liter=_decimal("MILK_MAX_PRICE_PER_LITER") or Decimal("0.80"),
            include_keywords=_list("MILK_INCLUDE_KEYWORDS"),
            exclude_keywords=_list("MILK_EXCLUDE_KEYWORDS"),
            include_merchants=_list("MILK_INCLUDE_MERCHANTS"),
            exclude_merchants=_list("MILK_EXCLUDE_MERCHANTS"),
        ),
        "beer": InterestRule(
            category="beer",
            max_price=_decimal("BEER_MAX_PRICE"),
            # The environment variable keeps its original name: renaming the
            # field must not silently drop an operator's configured threshold.
            temperature_min=_int("BEER_MIN_TEMPERATURE"),
            include_keywords=_list("BEER_INCLUDE_KEYWORDS"),
            exclude_keywords=_list("BEER_EXCLUDE_KEYWORDS"),
            include_merchants=_list("BEER_INCLUDE_MERCHANTS"),
            exclude_merchants=_list("BEER_EXCLUDE_MERCHANTS"),
        ),
    }


def _int(name):
    value = os.getenv(name)
    return int(value) if value else None
