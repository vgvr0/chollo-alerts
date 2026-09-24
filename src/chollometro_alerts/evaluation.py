"""Deterministic evaluation core shared by production, dry-run and replay.

Production scans, `run-rules --dry-run` and `alert test` all evaluate a deal
through this single path: persisted facts -> `PricingEngine` -> `InterestEngine`.
They only differ in where the deals come from, which side effects they allow and
how the verdict is presented.
"""

from dataclasses import dataclass

from .alert_rule import AlertRule
from .config import InterestRule
from .filters import (
    ConditionCheck,
    FilterResult,
    InterestEngine,
    merchant_decision,
)
from .models import Deal, format_number
from .pricing import PricingEngine
from .product import (
    ProductExtraction,
    deterministic_product_facts,
    extract_product,
    normalize_product_extraction,
)

# Provenance of the facts that produced a verdict. It is the `extraction_source`
# every evaluation already carries (and the cycle metrics already count); the
# notification only exposes it.
MATCH_METHODS = ("deterministic", "llm", "hybrid")

# Checks whose facts the model can supply when the local parser cannot derive
# them. Used to name the model's real contribution to an accepted match.
MODEL_FACT_CHECKS = frozenset(
    {
        "MIN_QUANTITY",
        "MIN_VOLUME",
        "MAX_PRICE_PER_LITER",
        "MAX_PRICE_PER_UNIT",
        "MAX_PRICE_PER_KILOGRAM",
    }
)


def interest_rule_from_alert(alert_rule: AlertRule) -> InterestRule:
    """Adapt a persisted `AlertRule` to the shape the evaluation engine uses."""
    constraints = alert_rule.constraints
    return InterestRule(
        category=alert_rule.category or "generic",
        product_type=alert_rule.product,
        brand=alert_rule.brand,
        include_merchants=alert_rule.include_merchants,
        exclude_merchants=alert_rule.exclude_merchants,
        max_price=constraints.max_price,
        max_price_per_liter=constraints.max_price_per_liter,
        max_price_per_unit=constraints.max_price_per_unit,
        min_quantity=constraints.min_quantity,
        min_volume_l=constraints.min_volume_l,
        temperature_min=constraints.temperature_min,
        temperature_max=constraints.temperature_max,
        query=alert_rule.query,
        category_include=constraints.category_include,
        category_exclude=constraints.category_exclude,
        max_age_minutes=constraints.max_age_minutes,
        momentum_enabled=alert_rule.momentum_enabled,
        momentum_window_minutes=alert_rule.momentum_window_minutes,
        minimum_temperature_velocity=alert_rule.minimum_temperature_velocity,
    )


@dataclass(frozen=True)
class MatchEvidence:
    """What a notification must show about one accepted match.

    The evidence travels with the match: the alert that produced it, the method
    that produced it and every condition that was really evaluated. `checks` is
    built by the engine, never by the notifier, so nothing can be claimed
    without having been compared first.
    """

    rule_id: int | None
    alert_text: str
    query: str
    method: str
    checks: tuple[ConditionCheck, ...] = ()
    semantic_reason: str | None = None
    momentum: dict | None = None

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "alert_text": self.alert_text,
            "query": self.query,
            "method": self.method,
            "checks": [check.as_dict() for check in self.checks],
            "semantic_reason": self.semantic_reason,
            "momentum": self.momentum,
        }

    @classmethod
    def from_dict(cls, payload) -> "MatchEvidence | None":
        """Rebuild persisted evidence; a malformed payload is ignored."""
        if not isinstance(payload, dict):
            return None
        checks = tuple(
            check
            for check in (
                ConditionCheck.from_dict(item) for item in payload.get("checks") or ()
            )
            if check is not None
        )
        method = payload.get("method")
        return cls(
            rule_id=payload.get("rule_id"),
            alert_text=str(payload.get("alert_text") or ""),
            query=str(payload.get("query") or ""),
            method=method if method in MATCH_METHODS else "deterministic",
            checks=checks,
            semantic_reason=payload.get("semantic_reason") or None,
            momentum=payload.get("momentum")
            if isinstance(payload.get("momentum"), dict)
            else None,
        )


@dataclass(frozen=True)
class DealEvaluation:
    """The deterministic verdict for one deal, before any side effect."""

    deal: Deal
    extraction: ProductExtraction
    rule: InterestRule
    result: FilterResult
    known: bool
    from_cache: bool

    @property
    def method(self) -> str:
        """How the facts that produced this verdict were obtained."""
        source = getattr(self.extraction, "extraction_source", "deterministic")
        return source if source in MATCH_METHODS else "deterministic"

    def evidence(
        self, *, rule_id=None, alert_text=None, query=None, momentum=None
    ) -> MatchEvidence:
        """Render-ready evidence of this evaluation, scoped to one alert."""
        return build_evidence(
            self, rule_id=rule_id, alert_text=alert_text, query=query, momentum=momentum
        )


def build_evidence(
    evaluation: DealEvaluation,
    *,
    rule_id=None,
    alert_text=None,
    query=None,
    momentum=None,
) -> MatchEvidence:
    """Package an accepted evaluation as alert-scoped, render-ready evidence.

    The alert is the one being evaluated (`alert_text`/`query`), never
    `deal.category`: the same deal matching two alerts produces two independent
    pieces of evidence, each naming its own alert.
    """
    text = (query or "").strip()
    return MatchEvidence(
        rule_id=rule_id,
        alert_text=(alert_text or text).strip(),
        query=text,
        method=evaluation.method,
        checks=evaluation.result.checks,
        semantic_reason=semantic_reason(evaluation),
        momentum=momentum,
    )


def semantic_reason(evaluation: DealEvaluation) -> str | None:
    """The model's real contribution to an accepted match, or None.

    No prompt, chain of thought or internal token is ever exposed: the facts
    listed here are the values the successful checks compared and they are
    listed only when the local deterministic parser could not produce them, so
    they can only have come from the model. A fact the checks did not use is
    never mentioned.
    """
    if evaluation.method == "deterministic":
        return None
    used = {check.code for check in evaluation.result.checks}
    extraction = evaluation.extraction
    local = deterministic_product_facts(
        evaluation.deal.product_text or evaluation.deal.title
    )
    facts = []
    if (
        "PRODUCT" in used
        and extraction.product_type
        and (local.product_type or "").casefold() != extraction.product_type.casefold()
    ):
        facts.append(f"producto «{extraction.product_type}»")
    # The local parser never infers a brand: a brand check that passed proves
    # the model supplied that fact.
    if "BRAND" in used and extraction.brand and not local.brand:
        facts.append(f"marca «{extraction.brand}»")
    if MODEL_FACT_CHECKS & used:
        if extraction.units is not None and local.units is None:
            facts.append(f"unidades: {format_number(extraction.units)}")
        if extraction.unit_volume_l is not None and local.unit_volume_l is None:
            facts.append(
                f"volumen por unidad: {format_number(extraction.unit_volume_l)} L"
            )
    if not facts:
        return None
    return "Hechos aportados por el modelo: " + ", ".join(facts) + "."


class DealEvaluator:
    """Resolve stored facts, price the deal and apply the interest rule."""

    def __init__(self, repository, pricing=None, interest=None):
        self.repository = repository
        self.pricing = pricing if pricing is not None else PricingEngine()
        self.interest = interest if interest is not None else InterestEngine()
        # Per-service-cycle reuse also covers dry-runs and concurrent rule
        # loops where durable persistence is intentionally disabled.
        self._cycle_extractions = {}

    def evaluate(
        self,
        deal: Deal,
        rule: InterestRule,
        *,
        extractor=None,
        persist_extraction=False,
    ) -> DealEvaluation:
        """Evaluate one deal without deciding anything about side effects.

        Identity is checked before any provider call, exactly like production: a
        deal already stored locally never reaches the LLM, it reuses the cached
        extraction or the deterministic one.

        The merchant filter runs before the provider call as well: it depends on
        the shop alone, so an excluded (or not allowed) shop is rejected without
        spending an LLM request. The facts of that rejection are the local,
        deterministic ones, and they are deliberately not cached: the cache is
        reserved for extractions that were really asked for.
        """
        known = self.repository.exists(deal.deal_id)
        product_text = deal.product_text or deal.title
        cache_key = (deal.deal_id, product_text)
        cached = self._cycle_extractions.get(cache_key)
        if cached is None:
            try:
                cached = self.repository.get_extraction(
                    deal.deal_id, product_text=product_text
                )
            except TypeError:
                # Small repository doubles used by integrations may still
                # implement the pre-fingerprint one-argument API.
                cached = self.repository.get_extraction(deal.deal_id)
        if cached is not None:
            extraction = (
                cached
                if isinstance(cached, ProductExtraction)
                else normalize_product_extraction(
                    ProductExtraction.model_validate(cached)
                )
            )
            self._cycle_extractions[cache_key] = extraction
            from_cache = True
        else:
            verdict, merchant_checks = merchant_decision(deal.merchant, rule)
            if not verdict.accepted:
                extraction = extract_product(deal.product_text or deal.title)
                priced = self.pricing.evaluate(deal, extraction)
                return DealEvaluation(
                    priced,
                    extraction,
                    rule,
                    FilterResult(
                        False, verdict.reason or "REJECTED_MERCHANT", merchant_checks
                    ),
                    known,
                    False,
                )
            extraction = extract_product(
                product_text,
                llm=None if known else extractor,
                deal_id=deal.deal_id,
            )
            from_cache = False
            self._cycle_extractions[cache_key] = extraction
            if persist_extraction:
                payload = extraction.model_dump(mode="json")
                try:
                    self.repository.save_extraction(
                        deal.deal_id, payload, product_text=product_text
                    )
                except TypeError:
                    # Backward-compatible repository doubles and adapters.
                    self.repository.save_extraction(deal.deal_id, payload)
        priced = self.pricing.evaluate(deal, extraction)
        result = self.interest.evaluate(priced, rule)
        return DealEvaluation(priced, extraction, rule, result, known, from_cache)
