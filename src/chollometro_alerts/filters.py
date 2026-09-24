from dataclasses import dataclass

from .categories import category_matches, normalize_category
from .config import InterestRule
from .merchants import (
    MERCHANT_EXCLUDED,
    MERCHANT_NOT_ALLOWED,
    MerchantVerdict,
    merchant_verdict,
)
from .models import Deal, format_amount, format_number
from .product import product_tokens, product_type_matches

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


def merchant_decision(
    merchant: str | None, rule: InterestRule
) -> tuple[MerchantVerdict, tuple[ConditionCheck, ...]]:
    """The deterministic merchant verdict and the evidence behind it.

    Shared by `apply_rule` and by the pre-LLM gate of the evaluation pipeline,
    so the merchant filter is decided exactly once, in one place, and never by
    the model. The checks are only produced for lists the rule really carries:
    a rule without merchants claims nothing.
    """
    verdict = merchant_verdict(merchant, rule.include_merchants, rule.exclude_merchants)
    checks: list[ConditionCheck] = []
    if rule.exclude_merchants and verdict.reason != MERCHANT_EXCLUDED:
        checks.append(
            ConditionCheck(
                "MERCHANT_EXCLUDED",
                "Sin tiendas excluidas",
                ", ".join(rule.exclude_merchants),
            )
        )
    if rule.include_merchants and verdict.reason != MERCHANT_NOT_ALLOWED:
        checks.append(
            ConditionCheck("MERCHANT_INCLUDED", "Tienda permitida", merchant or "N/D")
        )
    return verdict, tuple(checks)


def query_matches(query: str, deal: Deal) -> bool:
    """Conservative lexical relevance for an alert without structured facts."""
    expected = product_tokens(query)
    actual = product_tokens(
        " ".join(part for part in (deal.title, deal.product_text) if part)
    )
    if not expected or len(actual) < len(expected):
        return False
    return any(
        actual[index : index + len(expected)] == expected
        for index in range(len(actual) - len(expected) + 1)
    )


_GENERIC_QUERY_WORDS = frozenset(
    {
        "cerveza",
        "leche",
        "móvil",
        "móviles",
        "movil",
        "moviles",
        "oferta",
        "ofertas",
        "portátil",
        "portátiles",
        "portatil",
        "portatiles",
        "zapatilla",
        "zapatillas",
    }
)


def apply_rule(deal: Deal, rule: InterestRule) -> FilterResult:
    """Decide one deal and keep the evidence of everything it really checked."""
    text = deal.title.casefold()
    extraction = deal.product_extraction
    checks: list[ConditionCheck] = []

    def verdict(accepted, reason):
        return FilterResult(accepted, reason, tuple(checks))

    if rule.category_exclude:
        if any(
            category_matches(deal.categories, value)
            or normalize_category(deal.category) == value
            for value in rule.category_exclude
        ):
            return verdict(False, "REJECTED_CATEGORY_EXCLUDED")
        checks.append(
            ConditionCheck(
                "CATEGORY_EXCLUDED",
                "Sin categorías excluidas",
                ", ".join(rule.category_exclude),
            )
        )
    if rule.category_include:
        matched = next(
            (
                value
                for value in rule.category_include
                if category_matches(deal.categories, value)
                or normalize_category(deal.category) == value
            ),
            None,
        )
        if matched is None:
            return verdict(False, "REJECTED_CATEGORY_NOT_ALLOWED")
        checks.append(
            ConditionCheck("CATEGORY_INCLUDED", "Categoría permitida", matched)
        )

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
    relevance_only = (
        rule.max_price is None
        and rule.max_price_per_liter is None
        and rule.max_price_per_kilogram is None
        and rule.max_price_per_unit is None
        and rule.min_quantity is None
        and rule.min_volume_l is None
        and rule.temperature_min is None
        and rule.temperature_max is None
        and not rule.category_include
        and not rule.category_exclude
        and rule.max_age_minutes is None
    )
    specific_query = rule.query and (
        len(product_tokens(rule.query)) > 1
        or rule.query.casefold() not in _GENERIC_QUERY_WORDS
    )
    if (
        relevance_only
        and not rule.product_type
        and not rule.brand
        and rule.query is not None
        and specific_query
    ):
        if not query_matches(rule.query, deal):
            return verdict(False, "REJECTED_RELEVANCE")
        checks.append(
            ConditionCheck(
                "RELEVANCE",
                "Producto/marca relevante",
                f"«{rule.query}» aparece en el producto",
            )
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
                f"{format_amount(deal.price_per_liter)}/L < "
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
                f"{format_amount(price_per_kilogram)}/kg < "
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
                f"{format_amount(deal.price_per_unit)}/ud < "
                f"{format_amount(rule.max_price_per_unit)}/ud",
            )
        )
    # An unknown required value is never a match.  In particular, a deal with
    # no price cannot satisfy an absolute price ceiling by accident.
    if rule.max_price is not None:
        if deal.price is None:
            return verdict(False, "REJECTED_UNKNOWN_PRICE")
        if deal.price >= rule.max_price:
            return verdict(False, "REJECTED_PRICE")
        checks.append(
            ConditionCheck(
                "MAX_PRICE",
                "Precio máximo",
                f"{format_amount(deal.price)} < {format_amount(rule.max_price)}",
            )
        )
    # The temperature window is one more AND condition, checked after the price
    # and before the merchant: a deal whose temperature is unknown cannot prove
    # the condition, so it is rejected instead of being announced, and the
    # bounds are inclusive (`425° ≥ 300°`, `150° ≤ 300°`).
    if rule.temperature_min is not None or rule.temperature_max is not None:
        if deal.temperature is None:
            return verdict(False, "REJECTED_TEMPERATURE")
        if rule.temperature_min is not None and deal.temperature < rule.temperature_min:
            return verdict(False, "REJECTED_TEMPERATURE")
        if rule.temperature_max is not None and deal.temperature > rule.temperature_max:
            return verdict(False, "REJECTED_TEMPERATURE")
        if rule.temperature_min is not None:
            checks.append(
                ConditionCheck(
                    "MIN_TEMPERATURE",
                    "Temperatura mínima",
                    f"{format_number(deal.temperature)}° ≥ "
                    f"{format_number(rule.temperature_min)}°",
                )
            )
        if rule.temperature_max is not None:
            checks.append(
                ConditionCheck(
                    "MAX_TEMPERATURE",
                    "Temperatura máxima",
                    f"{format_number(deal.temperature)}° ≤ "
                    f"{format_number(rule.temperature_max)}°",
                )
            )
    if rule.max_age_minutes is not None:
        from datetime import UTC, datetime

        if deal.published_at is None:
            return verdict(False, "REJECTED_UNKNOWN_AGE")
        age = (
            datetime.now(UTC) - deal.published_at.astimezone(UTC)
        ).total_seconds() / 60
        if age > rule.max_age_minutes or age < 0:
            return verdict(False, "REJECTED_AGE")
        checks.append(
            ConditionCheck(
                "MAX_AGE",
                "Antigüedad máxima",
                f"{format_number(age)} min ≤ {format_number(rule.max_age_minutes)} min",
            )
        )
    merchant_result, merchant_checks = merchant_decision(deal.merchant, rule)
    if not merchant_result.accepted:
        return verdict(False, merchant_result.reason)
    checks.extend(merchant_checks)
    return verdict(True, "ACCEPTED")
