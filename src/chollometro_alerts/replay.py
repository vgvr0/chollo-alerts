"""Alert Replay Engine v0.1.

Evaluate a persisted `AlertRule` against deals that are already stored locally.
The replay is a read-only simulator: it reuses the production evaluation path
(`DealEvaluator` -> `PricingEngine` -> `InterestEngine`), never scrapes, never
calls an LLM provider, never sends Telegram and never writes to SQLite.
"""

import re
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .alert_rule import AlertConstraints, AlertRule
from .evaluation import DealEvaluation, DealEvaluator, interest_rule_from_alert

DEFAULT_REPLAY_LIMIT = 200

# Rejection reasons that mean "the local data was not enough to check this
# rule". Production keeps its own semantic; only the replay report relabels
# them, without duplicating any decision logic.
NOT_EVALUABLE_REASONS = frozenset(
    {
        "REJECTED_UNKNOWN_QUANTITY",
        "REJECTED_UNKNOWN_VOLUME",
        "REJECTED_UNKNOWN_WEIGHT",
    }
)

# Only used to relate a query to stored deals, never to decide a match.
QUERY_STOPWORDS = frozenset(
    {
        "al",
        "con",
        "de",
        "del",
        "el",
        "la",
        "las",
        "los",
        "para",
        "por",
        "que",
        "sin",
        "un",
        "una",
        "y",
        "o",
    }
)


class RuleNotFoundError(LookupError):
    """No persisted alert rule exists for the requested id."""

    def __init__(self, rule_id):
        super().__init__(f"no existe ninguna alerta con id {rule_id}")
        self.rule_id = rule_id


class ReplayDecision(StrEnum):
    MATCH = "MATCH"
    REJECT = "REJECT"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class ReplayDealResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deal_id: str
    title: str
    url: str | None = None
    price: Decimal | None = None
    decision: ReplayDecision
    reason: str | None = None


class ReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: int
    query: str
    product: str | None = None
    brand: str | None = None
    constraints: AlertConstraints = Field(default_factory=AlertConstraints)
    deals_available: int = 0
    deals_evaluated: int = 0
    matched: int = 0
    rejected: int = 0
    not_evaluable: int = 0
    results: list[ReplayDealResult] = Field(default_factory=list)

    def matches(self) -> list[ReplayDealResult]:
        return [r for r in self.results if r.decision is ReplayDecision.MATCH]

    def rejections(self) -> list[ReplayDealResult]:
        return [r for r in self.results if r.decision is ReplayDecision.REJECT]

    def not_evaluable_results(self) -> list[ReplayDealResult]:
        return [r for r in self.results if r.decision is ReplayDecision.NOT_EVALUABLE]

    @property
    def has_historical_deals(self) -> bool:
        return self.deals_evaluated > 0


def query_terms(query: str) -> list[str]:
    """Split an alert query into the terms used to find related stored deals."""
    words = re.split(r"[^\w]+", (query or "").casefold())
    return [w for w in words if len(w) >= 2 and w not in QUERY_STOPWORDS]


def _missing_fact(evaluation: DealEvaluation) -> bool:
    """True when the engine rejected because a required fact is unknown."""
    result = evaluation.result
    if result.reason in NOT_EVALUABLE_REASONS:
        return True
    extraction = evaluation.extraction
    rule = evaluation.rule
    if result.reason == "REJECTED_PRODUCT" and rule.product_type:
        return not extraction.product_type
    if result.reason == "REJECTED_BRAND" and rule.brand:
        return not extraction.brand
    return False


def classify(evaluation: DealEvaluation) -> tuple[ReplayDecision, str | None]:
    """Present the existing deterministic verdict as a replay decision.

    No filter logic lives here: the decision always comes from `FilterResult`.
    """
    if evaluation.result.accepted:
        return ReplayDecision.MATCH, None
    if _missing_fact(evaluation):
        return ReplayDecision.NOT_EVALUABLE, evaluation.result.reason
    return ReplayDecision.REJECT, evaluation.result.reason


class ReplayEngine:
    """Replay one persisted rule against historical deals."""

    def __init__(self, repository, evaluator=None):
        self.repository = repository
        self.evaluator = (
            evaluator if evaluator is not None else DealEvaluator(repository)
        )

    def load_rule(self, rule_id: int) -> AlertRule:
        rule = self.repository.rule_by_id(rule_id)
        if rule is None:
            raise RuleNotFoundError(rule_id)
        return rule

    def replay(
        self, rule_id: int, limit: int | None = DEFAULT_REPLAY_LIMIT
    ) -> ReplayResult:
        alert_rule = self.load_rule(rule_id)
        interest_rule = interest_rule_from_alert(alert_rule)
        candidates = self.repository.historical_deals(
            query_terms(alert_rule.query), category=alert_rule.category
        )
        selected = candidates if limit is None or limit <= 0 else candidates[:limit]
        results = []
        for deal in selected:
            # `extractor=None` keeps the replay offline: only cached or
            # deterministic local facts are used for already stored deals.
            evaluation = self.evaluator.evaluate(
                deal, interest_rule, extractor=None, persist_extraction=False
            )
            decision, reason = classify(evaluation)
            results.append(
                ReplayDealResult(
                    deal_id=deal.deal_id,
                    title=deal.title,
                    url=deal.url,
                    price=deal.price,
                    decision=decision,
                    reason=reason,
                )
            )
        return ReplayResult(
            rule_id=rule_id,
            query=alert_rule.query,
            product=alert_rule.product,
            brand=alert_rule.brand,
            constraints=alert_rule.constraints,
            deals_available=len(candidates),
            deals_evaluated=len(results),
            matched=sum(r.decision is ReplayDecision.MATCH for r in results),
            rejected=sum(r.decision is ReplayDecision.REJECT for r in results),
            not_evaluable=sum(
                r.decision is ReplayDecision.NOT_EVALUABLE for r in results
            ),
            results=results,
        )
