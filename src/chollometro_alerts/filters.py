from dataclasses import dataclass

from .config import InterestRule
from .models import Deal, format_amount, format_number
from .product import product_type_matches

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
class ConditionCheck:
    """One rule condition that was really evaluated for one deal.

    `detail` quotes the values the engine compared, so it can never claim a
    condition that was not checked: a fact the rule cannot prove (an unknown
    price, an unknown quantity) produces no check instead of a "passed" line.
    """

    code: str
    label: str
    detail: str

    def as_dict(self) -> dict:
        return {"code": self.code, "label": self.label, "detail": self.detail}

    @classmethod
    def from_dict(cls, payload) -> "ConditionCheck | None":
        if not isinstance(payload, dict):
            return None
        code, detail = payload.get("code"), payload.get("detail")
        if not code or not isinstance(detail, str):
            return None
        return cls(str(code), str(payload.get("label") or code), detail)


@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    reason: str
    # Evidence of every condition that was really evaluated, in rule order.
    checks: tuple[ConditionCheck, ...] = ()

    @property
    def matched(self):
        return self.accepted


class InterestEngine:
    def evaluate(self, deal: Deal, rule: InterestRule) -> FilterResult:
        return apply_rule(deal, rule)


def apply_rule(deal: Deal, rule: InterestRule) -> FilterResult:
    """Decide one deal and keep the evidence of everything it really checked."""
    text = deal.title.casefold()
    merchant = (deal.merchant or "").casefold()
    extraction = deal.product_extraction
    checks: list[ConditionCheck] = []

    def verdict(accepted, reason):
        return FilterResult(accepted, reason, tuple(checks))

    if rule.product_type:
        extracted = getattr(extraction, "product_type", None)
        if not product_type_matches(rule.product_type, extracted):
            return verdict(False, "REJECTED_PRODUCT")
        checks.append(
            ConditionCheck(
                "PRODUCT",
                "Producto buscado",
                f"«{rule.product_type}» (detectado: «{extracted}»)",
            )
        )
    if rule.brand:
        brand = getattr(extraction, "brand", None) if extraction else None
        if not brand or brand.casefold() != rule.brand.casefold():
            return verdict(False, "REJECTED_BRAND")
        checks.append(
            ConditionCheck("BRAND", "Marca", f"«{rule.brand}» (detectada: «{brand}»)")
        )
    if rule.exclude_keywords:
        if any(k in text for k in rule.exclude_keywords):
            return verdict(False, "REJECTED_KEYWORD")
        checks.append(
            ConditionCheck(
                "KEYWORD_EXCLUDED",
                "Sin palabras excluidas",
                ", ".join(rule.exclude_keywords),
            )
        )
    if rule.include_keywords:
        hit = next((k for k in rule.include_keywords if k in text), None)
        if hit is None:
            return verdict(False, "REJECTED_KEYWORD")
        checks.append(ConditionCheck("KEYWORD_INCLUDED", "Palabra clave", f"«{hit}»"))
    if rule.max_price_per_liter is not None and deal.total_volume_l is None:
        return verdict(False, "REJECTED_UNKNOWN_VOLUME")
    if rule.max_price_per_liter is not None:
        if (
            deal.price_per_liter is None
            or deal.price_per_liter >= rule.max_price_per_liter
        ):
            return verdict(False, "REJECTED_PRICE_PER_LITER")
        checks.append(
            ConditionCheck(
                "MAX_PRICE_PER_LITER",
                "Precio por litro",
                f"{format_amount(deal.price_per_liter)}/L ≤ "
                f"{format_amount(rule.max_price_per_liter)}/L",
            )
        )
    if rule.max_price_per_kilogram is not None:
        extraction = deal.product_extraction
        total_weight = getattr(extraction, "total_weight_kg", None)
        if total_weight is None or deal.price is None:
            return verdict(False, "REJECTED_UNKNOWN_WEIGHT")
        price_per_kilogram = deal.price / total_weight
        if price_per_kilogram >= rule.max_price_per_kilogram:
            return verdict(False, "REJECTED_PRICE_PER_KILOGRAM")
        checks.append(
            ConditionCheck(
                "MAX_PRICE_PER_KILOGRAM",
                "Precio por kilo",
                f"{format_amount(price_per_kilogram)}/kg ≤ "
                f"{format_amount(rule.max_price_per_kilogram)}/kg",
            )
        )
    if rule.min_quantity is not None:
        if deal.units is None:
            return verdict(False, "REJECTED_UNKNOWN_QUANTITY")
        if deal.units < rule.min_quantity:
            return verdict(False, "REJECTED_QUANTITY")
        checks.append(
            ConditionCheck(
                "MIN_QUANTITY",
                "Cantidad mínima",
                f"{format_number(deal.units)} ≥ {format_number(rule.min_quantity)}",
            )
        )
    if rule.min_volume_l is not None:
        if deal.total_volume_l is None:
            return verdict(False, "REJECTED_UNKNOWN_VOLUME")
        if deal.total_volume_l < rule.min_volume_l:
            return verdict(False, "REJECTED_VOLUME")
        checks.append(
            ConditionCheck(
                "MIN_VOLUME",
                "Volumen mínimo",
                f"{format_number(deal.total_volume_l)} L ≥ "
                f"{format_number(rule.min_volume_l)} L",
            )
        )
    if rule.max_price_per_unit is not None:
        # Same exclusive semantics as max_price/max_price_per_liter: the deal
        # must be strictly cheaper, so equality is rejected.
        if deal.units is None:
            return verdict(False, "REJECTED_UNKNOWN_QUANTITY")
        if (
            deal.price_per_unit is None
            or deal.price_per_unit >= rule.max_price_per_unit
        ):
            return verdict(False, "REJECTED_PRICE_PER_UNIT")
        checks.append(
            ConditionCheck(
                "MAX_PRICE_PER_UNIT",
                "Precio por unidad",
                f"{format_amount(deal.price_per_unit)}/ud ≤ "
                f"{format_amount(rule.max_price_per_unit)}/ud",
            )
        )
    # A deal without a price cannot prove this condition: the rule keeps its
    # existing semantics (no rejection) and the notification stays silent about
    # the price instead of claiming a comparison that never happened.
    if rule.max_price is not None and deal.price is not None:
        if deal.price >= rule.max_price:
            return verdict(False, "REJECTED_PRICE")
        checks.append(
            ConditionCheck(
                "MAX_PRICE",
                "Precio máximo",
                f"{format_amount(deal.price)} ≤ {format_amount(rule.max_price)}",
            )
        )
    if rule.min_temperature is not None:
        if deal.temperature is None or deal.temperature < rule.min_temperature:
            return verdict(False, "REJECTED_TEMPERATURE")
        checks.append(
            ConditionCheck(
                "MIN_TEMPERATURE",
                "Temperatura mínima",
                f"{deal.temperature}° ≥ {rule.min_temperature}°",
            )
        )
    if rule.exclude_merchants:
        if any(m in merchant for m in rule.exclude_merchants):
            return verdict(False, "REJECTED_MERCHANT")
        checks.append(
            ConditionCheck(
                "MERCHANT_EXCLUDED",
                "Sin tiendas excluidas",
                ", ".join(rule.exclude_merchants),
            )
        )
    if rule.include_merchants:
        if not any(m in merchant for m in rule.include_merchants):
            return verdict(False, "REJECTED_MERCHANT")
        checks.append(
            ConditionCheck(
                "MERCHANT_INCLUDED", "Tienda permitida", deal.merchant or "N/D"
            )
        )
    return verdict(True, "ACCEPTED")
