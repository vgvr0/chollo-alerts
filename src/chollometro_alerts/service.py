import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from .alert_rule import AlertRule
from .config import InterestRule, error_alert_cooldown_minutes
from .errors import SCAN_FAILED, SCAN_PARTIAL, SCAN_SUCCESS, ChollometroError
from .evaluation import DealEvaluator, MatchEvidence, interest_rule_from_alert
from .filters import InterestEngine
from .llm import ProductExtractor, create_extractor
from .pricing import PricingEngine
from .repository import (
    PENDING_NOTIFICATION_SCHEDULE,
    PENDING_TELEGRAM_FAILURE,
    as_utc,
)
from .schedule import window_from_alert_rule

logger = logging.getLogger(__name__)

# How the outcomes of several rules are collapsed into one cycle status.
_SCAN_PRECEDENCE = {SCAN_SUCCESS: 0, SCAN_PARTIAL: 1, SCAN_FAILED: 2}

# Outcome of the discovery feed inside a cycle, independent from the scan
# status of the HTML provider: the feed can fall back while the cycle still
# delivers alerts through the per-query HTML scans.
FEED_DISABLED = "DISABLED"
FEED_OK = "OK"
FEED_SKIPPED = "SKIPPED"
FEED_FALLBACK = "FALLBACK"

# One row per discovery cycle in `scan_runs`; the HTML path keeps its own rows.
FEED_QUERY_LABEL = "graphql:feed"

# Upper bound of delivery retries per cycle, so a Telegram outage cannot turn
# one cycle into an unbounded loop.
PENDING_NOTIFICATION_LIMIT = 50

# Only transient failures are retried by the feed client; this is the label the
# operational alert uses when a GraphQL failure degraded the cycle.
FEED_COMPONENT = "GraphQLFeedClient"


def published_after_alert(deal, created_at):
    """True only when the deal is provably newer than the alert creation.

    The provider timestamp and the stored `created_at` are compared in UTC. A
    deal without a timestamp is never considered "published after": the alert
    cannot prove it is new, so it stays silent.
    """
    if deal.published_at is None or created_at is None:
        return False
    return as_utc(deal.published_at) > as_utc(created_at)


def _isoformat(value):
    """ISO-8601 for logs; a missing provider timestamp is reported as N/D."""
    return value.isoformat() if value is not None else "N/D"


def _window_label(limit):
    """How to name the window in logs: `limit` omitted means the server's own."""
    return "server_default" if limit is None else limit


def _unique_feed_deals(deals):
    """One entry per thread id, first occurrence wins (the window is newest-first).

    The provider already deduplicates its window; the cycle repeats the guard so
    a repeated id can never be evaluated — or notified — twice in one pass.
    """
    unique = []
    seen = set()
    for deal in deals:
        if deal.deal_id in seen:
            continue
        seen.add(deal.deal_id)
        unique.append(deal)
    return unique


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
    before_alert: int = 0
    llm_calls: int = 0
    llm_cache_hits: int = 0
    llm_failures: int = 0
    llm_tokens: int = 0
    # Matches kept pending by an alert's notification window, and pending
    # deliveries that are still outside their window. Neither is an error.
    deferred: int = 0
    pending_waiting: int = 0

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
        self,
        client,
        repository,
        notifier,
        extractor: ProductExtractor | None = None,
        feed=None,
        clock=None,
    ):
        self.client = client
        self.repository = repository
        self.notifier = notifier
        self.extractor = extractor
        # Injectable clock: the notification windows are evaluated against it,
        # so tests never depend on when the suite runs.
        self._clock = clock or (lambda: datetime.now(UTC))
        # Optional GraphQL discovery feed. Without it the service keeps the
        # original per-query HTML scans (which are also its HTML fallback).
        self.feed = feed
        # Cooldown of the operational alerts, read once from the environment
        # (`ERROR_ALERT_COOLDOWN_MINUTES`, 60 minutes by default).
        self.error_cooldown_minutes = error_alert_cooldown_minutes()
        self.pricing = PricingEngine()
        self.interest = InterestEngine()
        self.evaluator = DealEvaluator(self.repository, self.pricing, self.interest)
        self.extraction_cache_hits = 0
        # Status of the last scan: SUCCESS, PARTIAL or FAILED.
        self.last_scan_status = SCAN_SUCCESS
        self.last_scan_error_type = None
        # Provider counters of a failed/partial scan, when it did not complete.
        self.last_scan_stats = None
        # Outcome of the last discovery cycle: the window it saw and whether it
        # had to degrade to the HTML provider.
        self.last_feed_status = FEED_DISABLED if feed is None else FEED_OK
        self.last_feed_error_type = None
        self.last_feed_received = 0
        self.last_feed_new = 0
        self.last_feed_oldest_age_seconds = None
        # Threads of the window that were already known: the loss signal the
        # cycle actually acts on (a saturated window alone means nothing).
        self.last_feed_overlap = None
        self.last_feed_window_full = False

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
        # The alert this scan evaluates: the notification names it, never the
        # deal category.
        alert_text = self._alert_text(rule_id, query)
        # The alert's notification window is read once per scan: it only gates
        # the Telegram delivery, never the match.
        window = self._rule_window(rule_id)
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
                    # The retry reuses the stored evidence of the original
                    # match, so the message keeps explaining the same alert.
                    self._notify_or_defer(
                        rule_id,
                        deal,
                        self._stored_evidence(rule_id, deal.deal_id),
                        window,
                    )
                continue
            # Canonical evaluation: identity, facts, pricing and interest rule.
            evaluation = self.evaluator.evaluate(
                deal, rule, extractor=extractor, persist_extraction=not dry_run
            )
            deal, result = evaluation.deal, evaluation.result
            self._record_evaluation(evaluation, extractor, initial_metrics)
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
            # Evidence of what really produced the match: the alert, the method
            # and every condition the engine checked.
            evidence = evaluation.evidence(
                rule_id=rule_id, alert_text=alert_text, query=query
            )
            if rule_id is not None and hasattr(self.repository, "record_rule_match"):
                self.repository.record_rule_match(deal.deal_id, rule_id)
                self.repository.record_rule_observation_result(
                    rule_id, deal.deal_id, True, None, evidence=evidence.as_dict()
                )
            inserted = self.repository.upsert(deal)
            self.last_summary.new += int(bool(inserted))
            if rule_id is None and not self.repository.was_notified(deal.deal_id):
                self.notifier.send(deal, evidence)
                if not getattr(self.notifier, "dry_run", False):
                    self.repository.mark_notified(deal.deal_id)
                    self.last_summary.telegram_sent += 1
                    if rule_id is not None and hasattr(
                        self.repository, "mark_rule_match_notified"
                    ):
                        self.repository.mark_rule_match_notified(deal.deal_id, rule_id)
                sent += 1
            elif rule_id is not None:
                sent += int(self._notify_or_defer(rule_id, deal, evidence, window))
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
        """Run one scan cycle: discovery feed when configured, HTML otherwise.

        The GraphQL cycle fetches the newest threads exactly once and evaluates
        only the new ones against every active rule. The per-query HTML loop is
        kept unchanged, both for installations without a feed client and as the
        controlled degradation when the GraphQL API cannot be reached.
        """
        if self.feed is not None:
            return self.run_feed_cycle(pages=pages)
        return self._run_rule_cycles(pages)

    def _run_rule_cycles(self, pages=1):
        """Scan only enabled persisted rules; comparisons remain deterministic."""
        # The HTML path never compares publishing dates (that is the feed's
        # window), so it does not need the creation timestamps.
        rules = [
            (rule_id, alert_rule, self._interest_rule(alert_rule))
            for rule_id, alert_rule in self._active_alert_rules()
        ]
        self.last_summary = RunSummary()
        # The HTML path keeps the same delivery guarantee as the feed: a match
        # whose Telegram delivery failed, or whose notification window was
        # closed, is delivered here even when the deal has already left the
        # scanned page.
        total = self._deliver_pending_notifications(rules)
        statuses = []
        for rule_id, alert_rule, rule in rules:
            total += self.run_rule(
                rule_id=rule_id,
                query=alert_rule.query,
                rule=rule,
                pages=pages,
            )
            statuses.append(self.last_scan_status)
        # One status per cycle: a single failed rule is never reported as a
        # completely successful cycle.
        self.last_scan_status = worst_scan_status(statuses)
        return total

    def _record_evaluation(self, evaluation, extractor, initial_metrics):
        """Counters shared by the HTML scans and the GraphQL discovery cycle."""
        self.last_summary.already_known += int(evaluation.known)
        if evaluation.from_cache:
            self.extraction_cache_hits += 1
            self.last_summary.llm_cache_hits += 1
        for field in ("llm_calls", "llm_failures", "llm_tokens"):
            key = field.upper()
            value = getattr(extractor, "metrics", {}).get(key, 0)
            setattr(self.last_summary, field, value - initial_metrics.get(key, 0))
        source_count = f"{evaluation.extraction.extraction_source}_count"
        setattr(
            self.last_summary,
            source_count,
            getattr(self.last_summary, source_count) + 1,
        )
        self.last_summary.classified += 1

    # --- GraphQL discovery cycle ------------------------------------------

    def run_feed_cycle(self, pages=1):
        """Fetch the feed once, then evaluate only the deals that are new.

        Persistence order is deliberate: a thread is registered as seen only
        after its per-rule outcome is durable, and a delivery that fails leaves
        a pending (matched, not notified) observation behind. That pending row
        is what the next cycle retries, so a Telegram failure — or a crash
        between evaluation and Telegram — can never mark a deal as processed
        without having notified it.

        The first cycle of a fresh database is not a blind snapshot: it
        initializes the feed state **and** evaluates what the alerts can prove
        to be new (see `_record_feed_baseline`).
        """
        self.last_summary = RunSummary()
        self.last_scan_status = SCAN_SUCCESS
        self.last_scan_error_type = None
        self.last_scan_stats = None
        self.last_feed_status = FEED_OK
        self.last_feed_error_type = None
        self.last_feed_received = 0
        self.last_feed_new = 0
        self.last_feed_oldest_age_seconds = None
        self.last_feed_overlap = None
        self.last_feed_window_full = False
        rules = self._active_rules_with_dates()
        if not rules:
            # Nothing can match, so the feed is not fetched at all.
            self.last_feed_status = FEED_SKIPPED
            logger.info("feed_cycle_skipped reason=no_active_rules")
            return 0
        try:
            deals = self.feed.latest()
        except ChollometroError as exc:
            return self._feed_fallback(pages, exc)
        deals = _unique_feed_deals(deals)
        self.last_summary.found = len(deals)
        self.last_feed_received = len(deals)
        batch = getattr(self.feed, "last_feed", None)
        if not self.repository.feed_is_initialized():
            return self._record_feed_baseline(deals, batch, rules)
        seen = self.repository.seen_feed_thread_ids([deal.deal_id for deal in deals])
        new_deals = [deal for deal in deals if deal.deal_id not in seen]
        self.last_feed_new = len(new_deals)
        self.last_summary.already_known += len(deals) - len(new_deals)
        self._log_feed_window(batch, new=len(new_deals), overlap=len(seen))
        extractor = self.extractor if self.extractor is not None else create_extractor()
        initial_metrics = dict(getattr(extractor, "metrics", {}))
        sent = self._deliver_pending_notifications(rules)
        for deal in new_deals:
            sent += self._process_feed_deal(deal, rules, extractor, initial_metrics)
            # Registered as seen after the outcome of every rule is durable:
            # a crash here simply evaluates the thread again next cycle, and a
            # failed delivery is retried from its pending observation.
            self.repository.record_feed_threads([deal])
        self.repository.set_feed_watermark(
            batch.newest_published_at if batch is not None else None
        )
        self._record_feed_scan_run(
            deals, new=len(new_deals), sent=sent, status=SCAN_SUCCESS
        )
        return sent

    def _record_feed_baseline(self, deals, batch, rules=None):
        """First cycle ever: initialize the feed, then evaluate what is new.

        The window is the historical reference for everything the alerts cannot
        prove to be new: a deal published at or before an alert's `created_at`
        only initializes the seen state and is never announced for that alert.
        It is **not** a blind snapshot, though: a deal published *after* an
        alert was created goes through the normal pipeline in this very first
        cycle (temporal gate -> merchant/interest filters -> extraction ->
        evidence -> persistence -> Telegram). Without that, every chollo
        published between the alert and the first daemon cycle would be lost.

        Each thread is registered as seen only after the outcome of every rule
        is durable, exactly like the steady-state cycle, so a crash mid-cycle
        only re-evaluates what was not settled yet.
        """
        if rules is None:
            rules = self._active_rules_with_dates()
        extractor = self.extractor if self.extractor is not None else create_extractor()
        initial_metrics = dict(getattr(extractor, "metrics", {}))
        sent = self._deliver_pending_notifications(rules)
        for deal in deals:
            sent += self._process_feed_deal(deal, rules, extractor, initial_metrics)
            self.repository.record_feed_threads([deal])
        self.repository.mark_feed_initialized()
        # Every thread of the window is seen for the first time.
        self.last_feed_new = len(deals)
        # No history exists yet, so "no overlap" cannot mean anything here.
        self._log_feed_window(batch, new=len(deals), overlap=None, initialized=False)
        self.repository.set_feed_watermark(
            batch.newest_published_at if batch is not None else None
        )
        logger.info(
            "feed_baseline_recorded threads=%s window_limit=%s eligible_sent=%s",
            len(deals),
            _window_label(getattr(batch, "window_limit", None)),
            sent,
        )
        self._record_feed_scan_run(
            deals, new=len(deals), sent=sent, status=SCAN_SUCCESS
        )
        return sent

    def _process_feed_deal(self, deal, rules, extractor, initial_metrics):
        """Evaluate one new feed deal against every active rule."""
        sent = 0
        for rule_id, alert_rule, rule, created_at, alert_text in rules:
            if not published_after_alert(deal, created_at):
                # The alert is younger than the deal: it must never announce it.
                self.last_summary.before_alert += 1
                continue
            observation = self.repository.get_rule_observation(rule_id, deal.deal_id)
            if self._already_settled(observation, deal, created_at):
                self.last_summary.already_known += 1
                continue
            if observation is None and not self.repository.claim_rule_observation(
                rule_id, deal.deal_id
            ):
                continue
            evaluation = self.evaluator.evaluate(
                deal, rule, extractor=extractor, persist_extraction=True
            )
            self._record_evaluation(evaluation, extractor, initial_metrics)
            if not evaluation.result.accepted:
                logger.info(
                    "deal=%s category=%s result=%s",
                    deal.deal_id,
                    evaluation.deal.category,
                    evaluation.result.reason,
                )
                self.repository.record_rule_observation_result(
                    rule_id, deal.deal_id, False, evaluation.result.reason
                )
                self.last_summary.rejected += 1
                continue
            self.last_summary.interesting += 1
            window = window_from_alert_rule(alert_rule)
            sent += self._deliver(
                rule_id,
                evaluation.deal,
                evaluation.evidence(
                    rule_id=rule_id, alert_text=alert_text, query=alert_rule.query
                ),
                window=window,
            )
        return sent

    @staticmethod
    def _already_settled(observation, deal, created_at):
        """True when a durable verdict already answers this (rule, deal) pair.

        A `matched` value means the pair is done: rejected, delivered, or
        matched with the delivery owned by the retry pass. A baseline snapshot
        only covers deals that already existed when the alert was created, so a
        deal that is provably newer than the alert is still evaluated — the
        baseline can never hide a new chollo. A claim without a verdict is an
        interrupted cycle and is evaluated again.
        """
        if observation is None:
            return False
        if observation[5] is not None:
            return True
        if observation[4]:
            return not published_after_alert(deal, created_at)
        return False

    def _rule_window(self, rule_id):
        """The notification window of one persisted alert, or None.

        An alert without a window — every legacy row, and every alert created
        before the windows existed — keeps notifying immediately.
        """
        if rule_id is None:
            return None
        loader = getattr(self.repository, "load_alert_rule", None)
        if loader is None:
            return None
        try:
            alert_rule = loader(rule_id)
        except Exception:  # noqa: BLE001 - an unreadable window is not fatal
            logger.warning("notification_window_unreadable rule_id=%s", rule_id)
            return None
        return window_from_alert_rule(alert_rule)

    def _defer(self, rule_id, deal_id, window):
        """True when the alert's window keeps this delivery for later."""
        if window is None or rule_id is None:
            return False
        if window.allows(self._clock()):
            return False
        self.repository.mark_rule_observation_pending(
            rule_id, deal_id, PENDING_NOTIFICATION_SCHEDULE
        )
        self.last_summary.deferred += 1
        logger.info(
            "notification_deferred rule_id=%s deal=%s window=%s reason=%s",
            rule_id,
            deal_id,
            window.describe(),
            PENDING_NOTIFICATION_SCHEDULE,
        )
        return True

    def _notify_or_defer(self, rule_id, deal, evidence, window):
        """Send the message now, or leave the durable match pending.

        Returns True only when Telegram really received it. A match outside the
        alert's window is neither an error nor a loss: it stays pending until a
        cycle runs inside the window.
        """
        if self._defer(rule_id, deal.deal_id, window):
            return False
        self.notifier.send(deal, evidence)
        if not getattr(self.notifier, "dry_run", False):
            self.repository.mark_rule_observation_notified(rule_id, deal.deal_id)
            self.last_summary.telegram_sent += 1
        return True

    def _deliver(self, rule_id, deal, evidence=None, window=None):
        """Persist the match, notify once and record the delivery.

        The match and the observation are written before Telegram, and
        `notified_at` only after it succeeded. A failure therefore leaves a
        durable pending state instead of a silently processed deal.

        The evidence is stored next to the match, so the retry pass replays the
        original explanation instead of a bare deal.

        The notification window is checked here, after the match is durable: a
        match outside the window is persisted as pending, not dropped, and the
        next cycle inside the window delivers it.
        """
        if evidence is None:
            # Retry pass: the alert, the method and the reasons come back from
            # the durable observation written before the failed delivery.
            evidence = self._stored_evidence(rule_id, deal.deal_id)
        self.repository.record_rule_match(deal.deal_id, rule_id)
        self.repository.record_rule_observation_result(
            rule_id,
            deal.deal_id,
            True,
            None,
            evidence=evidence.as_dict() if evidence is not None else None,
        )
        inserted = self.repository.upsert(deal)
        self.last_summary.new += int(bool(inserted))
        if self._defer(rule_id, deal.deal_id, window):
            return 0
        try:
            self.notifier.send(deal, evidence)
        except Exception as exc:  # noqa: BLE001 - one delivery must not stop the cycle
            self.repository.mark_rule_observation_pending(
                rule_id, deal.deal_id, PENDING_TELEGRAM_FAILURE
            )
            self.last_summary.errors += 1
            logger.warning(
                "telegram_send_failed deal=%s rule_id=%s error=%s",
                deal.deal_id,
                rule_id,
                type(exc).__name__,
            )
            self.notify_error(
                "TELEGRAM_ERROR", "AlertService", f"{type(exc).__name__}: {exc}"
            )
            return 0
        if not getattr(self.notifier, "dry_run", False):
            self.repository.mark_rule_observation_notified(rule_id, deal.deal_id)
            self.repository.mark_rule_match_notified(deal.deal_id, rule_id)
            self.repository.mark_notified(deal.deal_id)
        self.last_summary.telegram_sent += 1
        return 1

    def _deliver_pending_notifications(self, rules):
        """Deliver the pairs a Telegram failure or a window left pending.

        Both kinds are retried from the durable match, and the alert's own
        notification window applies to the retry just like it does to a fresh
        match: a pair that is still outside its window stays pending (with the
        reason it already had) and is picked up by a later cycle.
        """
        # The callers pass their own rule tuples (the feed adds the creation
        # date and the alert text); only the first two fields matter here.
        enabled = {entry[0] for entry in rules}
        windows = {entry[0]: window_from_alert_rule(entry[1]) for entry in rules}
        sent = 0
        for rule_id, deal_id in self.repository.pending_rule_notifications(
            PENDING_NOTIFICATION_LIMIT
        ):
            if rule_id not in enabled:
                continue
            window = windows.get(rule_id)
            if window is not None and not window.allows(self._clock()):
                self.last_summary.pending_waiting += 1
                logger.info(
                    "pending_notification_waiting rule_id=%s deal=%s window=%s",
                    rule_id,
                    deal_id,
                    window.describe(),
                )
                continue
            deal = self.repository.get_deal(deal_id)
            if deal is None:
                # Without the stored deal there is nothing to render; the row
                # stays pending instead of being marked as delivered.
                logger.warning(
                    "feed_pending_deal_missing rule_id=%s deal_id=%s",
                    rule_id,
                    deal_id,
                )
                continue
            sent += self._deliver(rule_id, deal, window=window)
        return sent

    def _active_rules_with_dates(self):
        """Enabled rules with the creation date and text that name their alerts."""
        rules = []
        for rule_id, alert_rule in self._active_alert_rules():
            rules.append(
                (
                    rule_id,
                    alert_rule,
                    self._interest_rule(alert_rule),
                    self.repository.alert_rule_created_at(rule_id),
                    self._alert_text(rule_id, alert_rule.query),
                )
            )
        return rules

    def _alert_text(self, rule_id, query):
        """The alert a notification must name: its stored text, else its query."""
        if rule_id is None:
            return query
        getter = getattr(self.repository, "alert_rule_original_text", None)
        if getter is None:
            return query
        return getter(rule_id) or query

    def _stored_evidence(self, rule_id, deal_id):
        """Evidence persisted with an earlier match, or None for older rows."""
        loader = getattr(self.repository, "rule_observation_evidence", None)
        if loader is None:
            return None
        payload = loader(rule_id, deal_id)
        return MatchEvidence.from_dict(payload) if payload else None

    def _log_feed_window(self, batch, new, overlap=None, initialized=True):
        """Record the visibility window and warn only on a real loss signal.

        A saturated window is **not** a risk by itself: the production request
        omits `limit`, so the endpoint always answers with its full default
        window (30 threads) and a "full" batch is the normal case. What matters
        is whether this window overlaps the threads already recorded: a window
        with threads and **zero** overlap, after a non-empty history, is the
        sign that the newest threads moved past the window between two cycles.
        """
        if batch is None:
            return
        age = batch.oldest_age_seconds
        previous = self.repository.feed_watermark()
        self.last_feed_oldest_age_seconds = age
        self.last_feed_overlap = overlap
        self.last_feed_window_full = bool(batch.window_full)
        logger.info(
            "feed_window received=%s new=%s window_limit=%s overlap=%s "
            "oldest_published_at=%s newest_published_at=%s oldest_age_seconds=%s "
            "xsrf_present=%s",
            batch.received,
            new,
            _window_label(batch.window_limit),
            "N/D" if overlap is None else overlap,
            _isoformat(batch.oldest_published_at),
            _isoformat(batch.newest_published_at),
            "N/D" if age is None else f"{age:.0f}",
            batch.xsrf_present,
        )
        if batch.server_window_full:
            # Informational: the widest available answer came back full.
            logger.info(
                "feed_window_saturated window=%s received=%s new=%s",
                batch.expected_window,
                batch.received,
                new,
            )
        oldest = batch.oldest_published_at
        if oldest is not None and previous is not None and oldest > previous:
            logger.warning(
                "feed_window_gap previous_newest_published_at=%s "
                "oldest_published_at=%s gap_seconds=%s",
                previous.isoformat(),
                oldest.isoformat(),
                int((oldest - previous).total_seconds()),
            )
        if (
            initialized
            and overlap == 0
            and batch.received > 0
            and self.repository.feed_thread_count() > 0
        ):
            logger.warning(
                "feed_window_risk reason=no_overlap received=%s new=%s overlap=0 "
                "window_limit=%s oldest_age_seconds=%s",
                batch.received,
                new,
                _window_label(batch.window_limit),
                "N/D" if age is None else f"{age:.0f}",
            )

    def _record_feed_scan_run(
        self, deals, *, new, sent, status, error_type=None, http_status=None
    ):
        """One `scan_runs` row per discovery cycle, with its window metrics."""
        if not hasattr(self.repository, "record_scan_run"):
            return
        self.repository.record_scan_run(
            FEED_QUERY_LABEL,
            rule_id=None,
            fetched_items=len(deals),
            parsed_items=len(deals),
            relevant_items=new,
            matching_items=sent,
            new_items=new,
            notifications_sent=sent,
            http_status=(
                http_status
                if http_status is not None
                else getattr(self.feed, "last_http_status", None)
            ),
            status=status,
            error_type=error_type,
        )

    def _feed_fallback(self, pages, error):
        """Degrade to the unchanged HTML scans, loudly but only once per cycle."""
        self.last_feed_status = FEED_FALLBACK
        self.last_feed_error_type = error.error_type
        logger.warning(
            "feed_fallback provider=chollometro_graphql error_type=%s "
            "http_status=%s message=%s",
            error.error_type,
            error.status_code,
            error,
        )
        self._record_feed_scan_run(
            (),
            new=0,
            sent=0,
            status=SCAN_FAILED,
            error_type=error.error_type,
            http_status=error.status_code,
        )
        self.notify_error(error.error_type, FEED_COMPONENT, str(error))
        return self._run_rule_cycles(pages)

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
        self, error_type, component, message, cooldown_minutes=None, run_id=None
    ):
        # The operator's `ERROR_ALERT_COOLDOWN_MINUTES` is the default; a caller
        # that passes an explicit value (including a test) still wins.
        if cooldown_minutes is None:
            cooldown_minutes = self.error_cooldown_minutes
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
