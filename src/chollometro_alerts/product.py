"""Deterministic product facts and validated provider enrichment."""

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .volume import extract_volume


class ProductExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_type: str | None = None
    brand: str | None = None
    variant: str | None = None
    units: int | None = Field(default=None, ge=1)
    unit_volume_l: Decimal | None = Field(default=None, ge=0)
    total_volume_l: Decimal | None = Field(default=None, ge=0)
    unit_weight_kg: Decimal | None = Field(default=None, ge=0)
    total_weight_kg: Decimal | None = Field(default=None, ge=0)
    confidence: Decimal = Field(default=Decimal(0), ge=0, le=1)
    extraction_source: str = "deterministic"

    @field_validator("extraction_source")
    @classmethod
    def valid_source(cls, value: str) -> str:
        if value not in {"deterministic", "llm", "hybrid"}:
            raise ValueError("invalid extraction source")
        return value


def normalize_product_extraction(extraction: ProductExtraction) -> ProductExtraction:
    """Fill deterministic derived volume fields after fact extraction."""
    data = extraction.model_dump()
    if data["units"] is not None and data["unit_volume_l"] is not None:
        data["total_volume_l"] = Decimal(data["units"]) * data["unit_volume_l"]
    if data["units"] is not None and data["unit_weight_kg"] is not None:
        data["total_weight_kg"] = Decimal(data["units"]) * data["unit_weight_kg"]
    return ProductExtraction.model_validate(data)


_WORD_SEPARATORS = re.compile(r"[^\w]+", re.UNICODE)


def _fold(text: str) -> str:
    """Case- and accent-insensitive form; words are never rewritten."""
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii").casefold()


def product_tokens(text: str) -> list[str]:
    """Word-aligned tokens of a product term: 'Zapatillas ASICS' -> 2 tokens."""
    return [token for token in _WORD_SEPARATORS.split(_fold(text)) if token]


def _same_word(left: str, right: str) -> bool:
    """True for the same word, tolerating only a simple Spanish plural."""
    return (
        left == right
        or left == f"{right}s"
        or left == f"{right}es"
        or right == f"{left}s"
        or right == f"{left}es"
    )


def product_type_matches(expected: str | None, extracted: str | None) -> bool:
    """Compare an alert product with the extracted product type.

    This is deliberately *not* semantic matching. It only tolerates case,
    accents, punctuation and a simple plural, and it requires the words of the
    expected product to appear, in the same order, at the head of the extracted
    type: 'zapatillas' matches 'Zapatillas running asfalto' and 'mini pc'
    matches 'Mini PC NAS'. It can never match a synonym ('running shoes'), and
    a trailing qualifier is not enough ('chocolate con leche' is not 'leche').
    A missing fact on either side is not a match; callers keep deciding whether
    that is a rejection or an unknown fact.
    """
    if not expected or not extracted:
        return False
    head = product_tokens(expected)
    extracted_tokens = product_tokens(extracted)
    if not head or len(extracted_tokens) < len(head):
        return False
    return all(
        _same_word(expected_token, extracted_token)
        for expected_token, extracted_token in zip(head, extracted_tokens, strict=False)
    )


def _deterministic(text: str) -> ProductExtraction:
    volume = extract_volume(text)
    # These are intentionally conservative. A wrong brand/type is worse than null.
    words = text.strip().split()
    product_type = next(
        (
            w.casefold()
            for w in words
            if w.casefold()
            in {"leche", "cerveza", "agua", "refresco", "zumo", "vino", "detergente"}
        ),
        None,
    )
    result = ProductExtraction(
        product_type=product_type,
        units=volume[0] if volume else None,
        unit_volume_l=volume[1] if volume else None,
        total_volume_l=volume[2] if volume else None,
        confidence=Decimal("0.98") if volume else Decimal("0.25"),
        extraction_source="deterministic",
    )
    return normalize_product_extraction(result)


def deterministic_product_facts(product_text: str) -> ProductExtraction:
    """The facts the local parser derives on its own for one text.

    Exposed so a caller can tell which facts of a hybrid extraction the model
    had to supply, without re-implementing the deterministic parser.
    """
    return _deterministic(product_text)


def extract_product(
    product_text: str,
    llm: Callable[[str], ProductExtraction | Mapping | str] | None = None,
    deal_id: str | None = None,
) -> ProductExtraction:
    """Extract product facts deterministically, completing missing facts via LLM.

    A high-confidence deterministic total volume is never sent to the LLM.
    LLM output is parsed and validated before being returned; absent values stay
    null and no price calculation is delegated to the model.
    """
    deterministic = _deterministic(product_text)
    if llm is None or (
        deterministic.total_volume_l is not None
        and deterministic.confidence >= Decimal("0.95")
    ):
        return normalize_product_extraction(deterministic)
    try:
        raw = (
            llm(product_text, deal_id=deal_id)
            if deal_id is not None
            else llm(product_text)
        )
        if isinstance(raw, ProductExtraction):
            llm_result = raw
        else:
            if isinstance(raw, str):
                raw = json.loads(raw)
            llm_result = ProductExtraction.model_validate(
                {**raw, "extraction_source": "llm"}
            )
        if llm_result.extraction_source == "deterministic":
            return normalize_product_extraction(deterministic)
    except Exception:  # noqa: BLE001 - invalid providers use deterministic fallback
        return normalize_product_extraction(deterministic)
    merged = deterministic.model_dump()
    for field in (
        "product_type",
        "brand",
        "variant",
        "units",
        "unit_volume_l",
        "total_volume_l",
        "unit_weight_kg",
        "total_weight_kg",
    ):
        if merged[field] is None and getattr(llm_result, field) is not None:
            merged[field] = getattr(llm_result, field)
    merged["confidence"] = min(deterministic.confidence, llm_result.confidence)
    merged["extraction_source"] = (
        "hybrid"
        if any(
            getattr(deterministic, f) is not None
            for f in ("units", "unit_volume_l", "total_volume_l")
        )
        else "llm"
    )
    return normalize_product_extraction(ProductExtraction.model_validate(merged))
