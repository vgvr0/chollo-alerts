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


def _list(name):
    return tuple(
        x.strip().casefold() for x in os.getenv(name, "").split(",") if x.strip()
    )


@dataclass(frozen=True)
class InterestRule:
    category: str
    max_price: Decimal | None = None
    min_temperature: int | None = None
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    include_merchants: tuple[str, ...] = ()
    exclude_merchants: tuple[str, ...] = ()
    max_price_per_liter: Decimal | None = None


def _decimal(name):
    value = os.getenv(name)
    return Decimal(value) if value else None


def load_rules():
    load_project_dotenv()
    return {
        "milk": InterestRule(
            category="milk",
            max_price=_decimal("MILK_MAX_PRICE"),
            max_price_per_liter=_decimal("MILK_MAX_PRICE_PER_LITER") or Decimal("0.75"),
            include_keywords=_list("MILK_INCLUDE_KEYWORDS"),
            exclude_keywords=_list("MILK_EXCLUDE_KEYWORDS"),
            include_merchants=_list("MILK_INCLUDE_MERCHANTS"),
            exclude_merchants=_list("MILK_EXCLUDE_MERCHANTS"),
        ),
        "beer": InterestRule(
            category="beer",
            max_price=_decimal("BEER_MAX_PRICE"),
            min_temperature=_int("BEER_MIN_TEMPERATURE"),
            include_keywords=_list("BEER_INCLUDE_KEYWORDS"),
            exclude_keywords=_list("BEER_EXCLUDE_KEYWORDS"),
            include_merchants=_list("BEER_INCLUDE_MERCHANTS"),
            exclude_merchants=_list("BEER_EXCLUDE_MERCHANTS"),
        ),
    }


def _int(name):
    value = os.getenv(name)
    return int(value) if value else None
