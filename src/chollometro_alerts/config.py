import os
from dataclasses import dataclass
from decimal import Decimal


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
