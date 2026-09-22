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


def category_for(title: str, query: str = "") -> str:
    text = title.casefold()
    if any(k in text for k in MILK):
        return "milk"
    if any(k in text for k in BEER):
        return "beer"
    return "generic"


def relevant(deal: Deal) -> bool:
    return True


@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    reason: str

    @property
    def matched(self):
        return self.accepted


class InterestEngine:
    def evaluate(self, deal: Deal, rule: InterestRule) -> FilterResult:
        return apply_rule(deal, rule)


def apply_rule(deal: Deal, rule: InterestRule) -> FilterResult:
    text = deal.title.casefold()
    merchant = (deal.merchant or "").casefold()
    extraction = deal.product_extraction
    if rule.product_type and (
        not extraction
        or not extraction.product_type
        or extraction.product_type.casefold() != rule.product_type.casefold()
    ):
        return FilterResult(False, "REJECTED_PRODUCT")
    if rule.brand and (
        not extraction
        or not extraction.brand
        or extraction.brand.casefold() != rule.brand.casefold()
    ):
        return FilterResult(False, "REJECTED_BRAND")
    if rule.exclude_keywords and any(k in text for k in rule.exclude_keywords):
        return FilterResult(False, "REJECTED_KEYWORD")
    if rule.include_keywords and not any(k in text for k in rule.include_keywords):
        return FilterResult(False, "REJECTED_KEYWORD")
    if rule.max_price_per_liter is not None and deal.total_volume_l is None:
        return FilterResult(False, "REJECTED_UNKNOWN_VOLUME")
    if rule.max_price_per_liter is not None and (
        deal.price_per_liter is None or deal.price_per_liter >= rule.max_price_per_liter
    ):
        return FilterResult(False, "REJECTED_PRICE_PER_LITER")
    if rule.max_price_per_kilogram is not None:
        extraction = deal.product_extraction
        total_weight = getattr(extraction, "total_weight_kg", None)
        if total_weight is None or deal.price is None:
            return FilterResult(False, "REJECTED_UNKNOWN_WEIGHT")
        if deal.price / total_weight >= rule.max_price_per_kilogram:
            return FilterResult(False, "REJECTED_PRICE_PER_KILOGRAM")
    if rule.min_quantity is not None:
        if deal.units is None:
            return FilterResult(False, "REJECTED_UNKNOWN_QUANTITY")
        if deal.units < rule.min_quantity:
            return FilterResult(False, "REJECTED_QUANTITY")
    if rule.min_volume_l is not None:
        if deal.total_volume_l is None:
            return FilterResult(False, "REJECTED_UNKNOWN_VOLUME")
        if deal.total_volume_l < rule.min_volume_l:
            return FilterResult(False, "REJECTED_VOLUME")
    if rule.max_price_per_unit is not None:
        # Same exclusive semantics as max_price/max_price_per_liter: the deal
        # must be strictly cheaper, so equality is rejected.
        if deal.units is None:
            return FilterResult(False, "REJECTED_UNKNOWN_QUANTITY")
        if (
            deal.price_per_unit is None
            or deal.price_per_unit >= rule.max_price_per_unit
        ):
            return FilterResult(False, "REJECTED_PRICE_PER_UNIT")
    if (
        rule.max_price is not None
        and deal.price is not None
        and deal.price >= rule.max_price
    ):
        return FilterResult(False, "REJECTED_PRICE")
    if rule.min_temperature is not None and (
        deal.temperature is None or deal.temperature < rule.min_temperature
    ):
        return FilterResult(False, "REJECTED_TEMPERATURE")
    if rule.exclude_merchants and any(m in merchant for m in rule.exclude_merchants):
        return FilterResult(False, "REJECTED_MERCHANT")
    if rule.include_merchants and not any(
        m in merchant for m in rule.include_merchants
    ):
        return FilterResult(False, "REJECTED_MERCHANT")
    return FilterResult(True, "ACCEPTED")
