"""Deterministic evaluation core shared by production, dry-run and replay.

Production scans, `run-rules --dry-run` and `alert test` all evaluate a deal
through this single path: persisted facts -> `PricingEngine` -> `InterestEngine`.
They only differ in where the deals come from, which side effects they allow and
how the verdict is presented.
"""

from dataclasses import dataclass

from .alert_rule import AlertRule
from .config import InterestRule
from .filters import FilterResult, InterestEngine
from .models import Deal
from .pricing import PricingEngine
from .product import ProductExtraction, extract_product, normalize_product_extraction


def interest_rule_from_alert(alert_rule: AlertRule) -> InterestRule:
    """Adapt a persisted `AlertRule` to the shape the evaluation engine uses."""
    constraints = alert_rule.constraints
    return InterestRule(
        category=alert_rule.category or "generic",
        product_type=alert_rule.product,
        brand=alert_rule.brand,
        max_price=constraints.max_price,
        max_price_per_liter=constraints.max_price_per_liter,
        max_price_per_unit=constraints.max_price_per_unit,
        min_quantity=constraints.min_quantity,
        min_volume_l=constraints.min_volume_l,
        min_temperature=constraints.min_temperature,
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


class DealEvaluator:
    """Resolve stored facts, price the deal and apply the interest rule."""

    def __init__(self, repository, pricing=None, interest=None):
        self.repository = repository
        self.pricing = pricing if pricing is not None else PricingEngine()
        self.interest = interest if interest is not None else InterestEngine()

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
        """
        known = self.repository.exists(deal.deal_id)
        cached = self.repository.get_extraction(deal.deal_id)
        if cached is not None:
            extraction = normalize_product_extraction(
                ProductExtraction.model_validate(cached)
            )
            from_cache = True
        else:
            extraction = extract_product(
                deal.product_text or deal.title,
                llm=None if known else extractor,
                deal_id=deal.deal_id,
            )
            from_cache = False
            if persist_extraction:
                self.repository.save_extraction(
                    deal.deal_id, extraction.model_dump(mode="json")
                )
        priced = self.pricing.evaluate(deal, extraction)
        result = self.interest.evaluate(priced, rule)
        return DealEvaluation(priced, extraction, rule, result, known, from_cache)
