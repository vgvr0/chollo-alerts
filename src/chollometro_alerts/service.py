import logging
import uuid
from dataclasses import dataclass

from .alert_rule import AlertRule
from .config import InterestRule
from .errors import SCAN_FAILED, SCAN_PARTIAL, SCAN_SUCCESS, ChollometroError
from .evaluation import DealEvaluator, interest_rule_from_alert
from .filters import InterestEngine
from .llm import ProductExtractor, create_extractor
from .pricing import PricingEngine

logger = logging.getLogger(__name__)

# How the outcomes of several rules are collapsed into one cycle status.
_SCAN_PRECEDENCE = {SCAN_SUCCESS: 0, SCAN_PARTIAL: 1, SCAN_FAILED: 2}


def worst_scan_status(statuses):
    return max(statuses, key=lambda s: _SCAN_PRECEDENCE.get(s, 0), default=SCAN_SUCCESS)


@dataclass
class RunSummary:
    scan_status: str = SCAN_SUCCESS
    scan_error_type: str = "N/D"
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
            "scan_status",
            "scan_error_type",
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
        self.evaluator = DealEvaluator(self.repository, self.pricing, self.interest)
        self.extraction_cache_hits = 0
        # Status of the last scan: SUCCESS, PARTIAL or FAILED.
        self.last_scan_status = SCAN_SUCCESS
        self.last_scan_error_type = None
        # Provider counters of a failed/partial scan, when it did not complete.
        self.last_scan_stats = None

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
        self.last_scan_status = SCAN_SUCCESS
        self.last_scan_error_type = None
        self.last_scan_stats = None
        if rule is None:
            logger.error("missing rule context query=%s rule_id=%s", query, rule_id)
            return [] if dry_run else 0
        sent = 0
        self.last_summary = RunSummary()
        extractor = self.extractor if self.extractor is not None else create_extractor()
        initial_metrics = dict(getattr(extractor, "metrics", {}))
        try:
            deals = self.client.recent([query], pages)
        except ChollometroError as exc:
            # A provider failure is never an empty result set. Any other
            # exception is a programming error and keeps propagating.
            status, deals = self._provider_failure(rule_id, query, exc, dry_run)
            if status == SCAN_FAILED:
                self._record_scan_run(rule_id, query, dry_run, len(deals))
                return [] if dry_run else 0
        # One row per scan, with the status and the failing HTTP status when the
        # scan did not complete.
        self._record_scan_run(rule_id, query, dry_run, len(deals))
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
            # Canonical evaluation: identity, facts, pricing and interest rule.
            evaluation = self.evaluator.evaluate(
                deal, rule, extractor=extractor, persist_extraction=not dry_run
            )
            deal, extraction, result = (
                evaluation.deal,
                evaluation.extraction,
                evaluation.result,
            )
            self.last_summary.already_known += int(evaluation.known)
            if evaluation.from_cache:
                self.extraction_cache_hits += 1
                self.last_summary.llm_cache_hits += 1
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
            self.last_summary.classified += 1
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

    def _provider_failure(self, rule_id, query, error, dry_run):
        """Record a failed or incomplete Chollometro scan and return its outcome.

        A total failure keeps nothing: no page was read, so no deal is
        evaluated, persisted or notified. A partial failure keeps the deals of
        the pages that did answer (their observations are claimed per deal, so
        the missing pages are simply discovered in a later cycle) but the scan
        is recorded as PARTIAL, never as a complete success.
        """
        outcome = self._partial_outcome()
        status = SCAN_PARTIAL if outcome is not None else SCAN_FAILED
        deals = list(outcome.deals) if outcome is not None else []
        self.last_scan_status = status
        self.last_summary.scan_status = status
        self.last_scan_error_type = error.error_type
        self.last_summary.scan_error_type = error.error_type
        # The failing status is what belongs in `scan_runs`, not the 200 of the
        # last page that happened to answer.
        self.last_scan_stats = {
            "http_status": error.status_code,
            "fetched_items": getattr(outcome, "fetched_items", None),
            "parsed_items": getattr(outcome, "parsed_items", None),
        }
        # A partial scan is degraded, not lost; a total failure is an error.
        log = logger.warning if status == SCAN_PARTIAL else logger.error
        log(
            "scan_%s provider=chollometro operation=search query=%s "
            "pages_fetched=%s pages_requested=%s http_status=%s error_type=%s "
            "attempt=%s relevant_items=%s",
            status.lower(),
            query,
            getattr(outcome, "pages_fetched", 0),
            getattr(outcome, "pages_requested", 1),
            error.status_code,
            error.error_type,
            error.attempt,
            len(deals),
        )
        if not dry_run:
            # Operational alert per logical failure: the client retries inside
            # this single call, and the existing cooldown suppresses repeats.
            self.notify_error(error.error_type, "ChollometroClient", str(error))
        return status, deals

    def _record_scan_run(self, rule_id, query, dry_run, relevant_items):
        """Persist the outcome of one scan (SUCCESS, PARTIAL or FAILED)."""
        if dry_run or rule_id is None:
            return
        if not hasattr(self.repository, "record_scan_run"):
            return
        stats = self.last_scan_stats or getattr(self.client, "last_search", {})
        self.repository.record_scan_run(
            query,
            rule_id=rule_id,
            fetched_items=stats.get("fetched_items"),
            parsed_items=stats.get("parsed_items"),
            http_status=stats.get("http_status"),
            relevant_items=relevant_items,
            matching_items=None,
            new_items=None,
            status=self.last_scan_status,
            error_type=self.last_scan_error_type,
        )

    def _partial_outcome(self):
        outcome = getattr(self.client, "last_scan", None)
        if outcome is None:
            return None
        return outcome if getattr(outcome, "status", None) == SCAN_PARTIAL else None

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
        statuses = []
        for rule_id, alert_rule in self._active_alert_rules():
            total += self.run_rule(
                rule_id=rule_id,
                query=alert_rule.query,
                rule=self._interest_rule(alert_rule),
                pages=pages,
            )
            statuses.append(self.last_scan_status)
        # One status per cycle: a single failed rule is never reported as a
        # completely successful cycle.
        self.last_scan_status = worst_scan_status(statuses)
        return total

    @staticmethod
    def _interest_rule(alert_rule: AlertRule):
        return interest_rule_from_alert(alert_rule)

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
        statuses = []
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
            statuses.append(self.last_scan_status)
        self.last_scan_status = worst_scan_status(statuses)
        return report

    def notify_error(
        self, error_type, component, message, cooldown_minutes=60, run_id=None
    ):
        if getattr(self.notifier, "dry_run", False):
            return False
        try:
            allowed = self.repository.error_alert_allowed(
                error_type, component, message, cooldown_minutes
            )
        except Exception:  # noqa: BLE001 - alerting must not break the scan
            # Without a working cooldown store the safe default is silence.
            logger.warning(
                "error_alert_cooldown_unavailable error_type=%s component=%s",
                error_type,
                component,
            )
            return False
        if not allowed:
            return False
        try:
            self.notifier.send_system_alert(
                error_type, component, message, run_id or uuid.uuid4().hex
            )
            return True
        except Exception:  # noqa: BLE001 - error reporting must never crash the run
            return False
