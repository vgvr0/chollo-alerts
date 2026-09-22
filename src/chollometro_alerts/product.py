"""Deterministic product facts and validated provider enrichment."""

import json
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
    return ProductExtraction.model_validate(data)


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
