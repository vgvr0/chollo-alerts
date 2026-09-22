from dataclasses import dataclass

from .config import InterestRule
from .models import Deal

MILK = ("leche", "central lechera asturiana", "puleva", "pascual", "kaiku")
BEER = (
    "cerveza",
    "cervezas",
    "estrella galicia",
    "mahou",
    "san miguel",
    "heineken",
    "amstel",
    "cruzcampo",
    "alhambra",
    "voll-damm",
    "1906",
)


def category_for(title: str, query: str = "") -> str | None:
    text = title.casefold()
    if any(k in text for k in MILK):
        return "milk"
    if any(k in text for k in BEER):
        return "beer"
    return None


def relevant(deal: Deal) -> bool:
    return deal.category in {"milk", "beer"}


@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    reason: str


class InterestEngine:
    def evaluate(self, deal: Deal, rule: InterestRule) -> FilterResult:
        return apply_rule(deal, rule)


def apply_rule(deal: Deal, rule: InterestRule) -> FilterResult:
    text = deal.title.casefold()
    merchant = (deal.merchant or "").casefold()
    if rule.exclude_keywords and any(k in text for k in rule.exclude_keywords):
        return FilterResult(False, "REJECTED_KEYWORD")
    if rule.include_keywords and not any(k in text for k in rule.include_keywords):
        return FilterResult(False, "REJECTED_KEYWORD")
    if (
        deal.category == "milk"
        and rule.max_price_per_liter is not None
        and deal.total_volume_l is None
    ):
        return FilterResult(False, "REJECTED_UNKNOWN_VOLUME")
    if (
        deal.category == "milk"
        and rule.max_price_per_liter is not None
        and (
            deal.price_per_liter is None
            or deal.price_per_liter >= rule.max_price_per_liter
        )
    ):
        return FilterResult(False, "REJECTED_PRICE_PER_LITER")
    if (
        rule.max_price is not None
        and deal.price is not None
        and deal.price > rule.max_price
    ):
        return FilterResult(False, "REJECTED_PRICE")
    if (
        (deal.category != "milk" or rule.max_price_per_liter is None)
        and rule.min_temperature is not None
        and (deal.temperature is None or deal.temperature < rule.min_temperature)
    ):
        return FilterResult(False, "REJECTED_TEMPERATURE")
    if rule.exclude_merchants and any(m in merchant for m in rule.exclude_merchants):
        return FilterResult(False, "REJECTED_MERCHANT")
    if rule.include_merchants and not any(
        m in merchant for m in rule.include_merchants
    ):
        return FilterResult(False, "REJECTED_MERCHANT")
    return FilterResult(True, "ACCEPTED")
