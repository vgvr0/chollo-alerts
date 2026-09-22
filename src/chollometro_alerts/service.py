import logging
import uuid
from dataclasses import dataclass

from .alert_rule import AlertRule
from .config import InterestRule
from .filters import InterestEngine
from .llm import ProductExtractor, create_extractor
from .pricing import PricingEngine
from .product import ProductExtraction, extract_product, normalize_product_extraction

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    found: int = 0
    classified: int = 0
    interesting: int = 0
    rejected: int = 0
    new: int = 0
    already_known: int = 0
    telegram_sent: int = 0
    errors: int = 0
    deterministic_count: int = 0
    llm_count: int = 0
    hybrid_count: int = 0
    llm_calls: int = 0
    llm_cache_hits: int = 0
    llm_failures: int = 0
    llm_tokens: int = 0

    def format_metrics(self) -> str:
        fields = (
            "found",
            "new",
            "deterministic_count",
            "llm_count",
            "hybrid_count",
            "llm_calls",
            "llm_cache_hits",
            "llm_failures",
            "llm_tokens",
            "telegram_sent",
        )
        return "\n".join(f"{name.upper()}={getattr(self, name)}" for name in fields)


class AlertService:
    def __init__(
        self, client, repository, notifier, extractor: ProductExtractor | None = None
    ):
        self.client = client
        self.repository = repository
        self.notifier = notifier
        self.extractor = extractor
        self.pricing = PricingEngine()
        self.interest = InterestEngine()
        self.extraction_cache_hits = 0

    def run(self, queries, pages=1, rules=None):
        # Static/check mode is kept for compatibility; the daemon never enters
        # this path and always supplies a persisted rule id via run_active_rules.
        rules = rules or {query: InterestRule(query) for query in queries}
        return sum(
            self.run_rule(
                None, query, rules.get(query) or next(iter(rules.values())), pages
            )
            for query in queries
        )

    def run_rule(self, rule_id, query, rule, pages=1, dry_run=False):
        self.last_summary = RunSummary()
        if rule is None:
            logger.error("missing rule context query=%s rule_id=%s", query, rule_id)
            return [] if dry_run else 0
        sent = 0
        self.last_summary = RunSummary()
        extractor = self.extractor if self.extractor is not None else create_extractor()
        initial_metrics = dict(getattr(extractor, "metrics", {}))
        deals = self.client.recent([query], pages)
        stats = getattr(self.client, "last_search", {})
        if (
            not dry_run
            and rule_id is not None
            and hasattr(self.repository, "record_scan_run")
        ):
            self.repository.record_scan_run(
                query,
                rule_id=rule_id,
                fetched_items=stats.get("fetched_items"),
                parsed_items=stats.get("parsed_items"),
                http_status=stats.get("http_status"),
                relevant_items=len(deals),
                matching_items=None,
                new_items=None,
            )
        results = []
        for deal in deals:
            self.last_summary.found += 1
            claimed = (
                self.repository.claim_rule_observation(rule_id, deal.deal_id)
                if rule_id is not None and not dry_run
                else True
            )
            if not claimed:
                # Claim identity for this rule before extraction or notification.
                # A retry therefore cannot spend LLM calls or send a second alert.
                self.last_summary.already_known += 1
                observation = self.repository.get_rule_observation(
                    rule_id, deal.deal_id
                )
                # A crash after claiming but before Telegram acknowledgement is
                # retried safely; successful observations remain idempotent.
                if observation and observation[5] and observation[7] is None:
                    self.notifier.send(deal)
                    if not getattr(self.notifier, "dry_run", False):
                        self.repository.mark_rule_observation_notified(
                            rule_id, deal.deal_id
                        )
                        self.last_summary.telegram_sent += 1
                continue
            # Check identity before any possible provider call, including legacy deals.
            known = self.repository.exists(deal.deal_id)
            self.last_summary.already_known += int(known)
            cached = self.repository.get_extraction(deal.deal_id)
            if cached is not None:
                extraction = normalize_product_extraction(
                    ProductExtraction.model_validate(cached)
                )
                self.extraction_cache_hits += 1
                self.last_summary.llm_cache_hits += 1
            else:
                extraction = extract_product(
                    deal.product_text or deal.title,
                    llm=None if known else extractor,
                    deal_id=deal.deal_id,
                )
                if not dry_run:
                    self.repository.save_extraction(
                        deal.deal_id, extraction.model_dump(mode="json")
                    )
            for field in ("llm_calls", "llm_failures", "llm_tokens"):
                key = field.upper()
                value = getattr(extractor, "metrics", {}).get(key, 0)
                setattr(self.last_summary, field, value - initial_metrics.get(key, 0))
            source_count = f"{extraction.extraction_source}_count"
            setattr(
                self.last_summary,
                source_count,
                getattr(self.last_summary, source_count) + 1,
            )
            deal = self.pricing.evaluate(deal, extraction)
            self.last_summary.classified += 1
            result = self.interest.evaluate(deal, rule)
            results.append((rule_id, query, deal, rule, result))
            if not result.accepted:
                logger.info(
                    "deal=%s category=%s result=%s",
                    deal.deal_id,
                    deal.category,
                    result.reason,
                )
                self.last_summary.rejected += 1
                if rule_id is not None and not dry_run:
                    self.repository.record_rule_observation_result(
                        rule_id, deal.deal_id, False, result.reason
                    )
                continue
            self.last_summary.interesting += 1
            if dry_run:
                continue
            if rule_id is not None and hasattr(self.repository, "record_rule_match"):
                self.repository.record_rule_match(deal.deal_id, rule_id)
                self.repository.record_rule_observation_result(
                    rule_id, deal.deal_id, True, None
                )
            inserted = self.repository.upsert(deal)
            self.last_summary.new += int(bool(inserted))
            if rule_id is None and not self.repository.was_notified(deal.deal_id):
                self.notifier.send(deal)
                if not getattr(self.notifier, "dry_run", False):
                    self.repository.mark_notified(deal.deal_id)
                    self.last_summary.telegram_sent += 1
                    if rule_id is not None and hasattr(
                        self.repository, "mark_rule_match_notified"
                    ):
                        self.repository.mark_rule_match_notified(deal.deal_id, rule_id)
                sent += 1
            elif rule_id is not None:
                self.notifier.send(deal)
                if not getattr(self.notifier, "dry_run", False):
                    self.repository.mark_rule_observation_notified(
                        rule_id, deal.deal_id
                    )
                    self.last_summary.telegram_sent += 1
                sent += 1
        return results if dry_run else sent

    def baseline(self, queries, pages=1, dry_run=False):
        deals = self.client.recent(queries, pages)
        if dry_run:
            return len(deals)
        for deal in deals:
            self.repository.upsert(deal)
            self.repository.mark_notified(deal.deal_id)
        return len(deals)

    def baseline_rule(self, rule_id, query, pages=1):
        """Record the current result set for one rule without evaluating or notifying."""
        try:
            deals = self.client.recent([query], pages)
            for deal in deals:
                self.repository.upsert(deal)
                self.repository.claim_rule_observation(
                    rule_id, deal.deal_id, baseline=True
                )
            self.repository.set_rule_state(rule_id, "ACTIVE", enabled=True)
            return len(deals)
        except Exception:
            self.repository.set_rule_state(
                rule_id, "INITIALIZING_FAILED", enabled=False
            )
            raise

    def run_active_rules(self, pages=1):
        """Scan only enabled persisted rules; comparisons remain deterministic."""
        total = 0
        for rule_id, alert_rule in self._active_alert_rules():
            total += self.run_rule(
                rule_id=rule_id,
                query=alert_rule.query,
                rule=self._interest_rule(alert_rule),
                pages=pages,
            )
        return total

    @staticmethod
    def _interest_rule(alert_rule: AlertRule):
        c = alert_rule.constraints
        return InterestRule(
            category=alert_rule.category or "generic",
            product_type=alert_rule.product,
            brand=alert_rule.brand,
            max_price=c.max_price,
            max_price_per_liter=c.max_price_per_liter,
            max_price_per_unit=c.max_price_per_unit,
            min_quantity=c.min_quantity,
            min_volume_l=c.min_volume_l,
            min_temperature=c.min_temperature,
        )

    def _active_alert_rules(self):
        """Yield (rule_id, AlertRule) for every enabled persisted rule."""
        for row in self.repository.list_alert_rules(enabled_only=True):
            yield row[0], self.repository.rule_from_listing(row)

    def dry_run_active_rules(self, pages=1):
        """Evaluate persisted active rules through the canonical pipeline.

        Rule loading, conversion, pricing, constraints and evaluation are the
        same code paths as `run_active_rules`; only the side effects
        (observations, deal persistence, Telegram) are skipped.
        """
        report = []
        for rule_id, alert_rule in self._active_alert_rules():
            report.extend(
                self.run_rule(
                    rule_id,
                    alert_rule.query,
                    self._interest_rule(alert_rule),
                    pages,
                    dry_run=True,
                )
            )
        return report

    def notify_error(
        self, error_type, component, message, cooldown_minutes=60, run_id=None
    ):
        if getattr(self.notifier, "dry_run", False):
            return False
        if not self.repository.error_alert_allowed(
            error_type, component, message, cooldown_minutes
        ):
            return False
        try:
            self.notifier.send_system_alert(
                error_type, component, message, run_id or uuid.uuid4().hex
            )
            return True
        except Exception:  # noqa: BLE001 - error reporting must never crash the run
            return False
