"""Safe, idempotent cleanup of disposable SQLite history."""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import RetentionSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionResult:
    dry_run: bool
    deleted_deals: int = 0
    deleted_product_extractions: int = 0
    deleted_matches: int = 0
    deleted_observations: int = 0
    deleted_feed_threads: int = 0
    deleted_temperature_snapshots: int = 0
    deleted_momentum_states: int = 0
    cutoff: datetime | None = None
    retention_days: int = 15
    deleted_snapshots: int = 0
    deleted_cache_entries: int = 0
    deleted_error_history: int = 0
    deleted_scan_runs: int = 0
    batches: int = 0
    duration_seconds: float = 0.0

    @property
    def total_deleted(self):
        return sum(
            (
                self.deleted_snapshots,
                self.deleted_cache_entries,
                self.deleted_error_history,
                self.deleted_scan_runs,
                self.deleted_deals,
                self.deleted_product_extractions,
                self.deleted_matches,
                self.deleted_observations,
                self.deleted_feed_threads,
                self.deleted_temperature_snapshots,
                self.deleted_momentum_states,
            )
        )


class RetentionService:
    """Repository-facing maintenance coordinator.

    Critical thread identity is deliberately absent from this delete list.
    Deal details and their rule state are disposable after the deal TTL, while
    ``feed_threads`` remains the durable site-aware deduplication memory.
    """

    def __init__(self, repository, settings=None, clock=None):
        self.repository = repository
        self.settings = settings or RetentionSettings.from_env()
        self.clock = clock or (lambda: datetime.now(UTC))

    def is_due(self, now=None):
        if not self.settings.enabled:
            return False
        now = now or self.clock()
        last = self.repository.retention_last_finished_at()
        return last is None or now - last >= timedelta(
            hours=self.settings.interval_hours
        )

    def run(self, *, dry_run=False, now=None):
        now = now or self.clock()
        started = time.monotonic()
        if not dry_run:
            self.repository.retention_started(now)
        logger.info("retention.started dry_run=%s", dry_run)
        try:
            result = self.repository.prune_retention(
                self.settings, now=now, dry_run=dry_run
            )
            result = RetentionResult(
                **{**result.__dict__, "duration_seconds": time.monotonic() - started}
            )
            if not dry_run:
                self.repository.retention_finished(now, "SUCCESS")
            logger.info(
                "retention.completed dry_run=%s retention_days=%s cutoff=%s deleted_deals=%s deleted=%s snapshots=%s cache=%s errors=%s scan_runs=%s duration_seconds=%.3f",
                dry_run,
                result.retention_days,
                result.cutoff.isoformat() if result.cutoff else None,
                result.deleted_deals,
                result.total_deleted,
                result.deleted_snapshots,
                result.deleted_cache_entries,
                result.deleted_error_history,
                result.deleted_scan_runs,
                result.duration_seconds,
            )
            return result
        except Exception as exc:
            if not dry_run:
                self.repository.retention_finished(now, "FAILED", type(exc).__name__)
            logger.exception(
                "retention.failed retention_days=%s cutoff=%s duration_seconds=%.3f error_type=%s",
                self.settings.deal_retention_days,
                (now - timedelta(days=self.settings.deal_retention_days)).isoformat(),
                time.monotonic() - started,
                type(exc).__name__,
            )
            raise

    def run_if_due(self, now=None):
        if not self.is_due(now):
            return None
        return self.run(now=now)
