"""Cheap, local-only runtime health evaluation."""

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .repository import DealRepository

logger = logging.getLogger(__name__)

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
UNHEALTHY = "UNHEALTHY"


def _minutes(name, default):
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser numérico") from exc
    if value <= 0:
        raise ValueError(f"{name} debe ser positivo")
    return value


@dataclass(frozen=True)
class HealthReport:
    status: str
    database: str
    scanner: str
    telegram: str
    last_scan_started_at: str | None = None
    last_scan_completed_at: str | None = None
    seconds_since_last_scan: int | None = None
    consecutive_failures: int = 0
    last_error_type: str | None = None
    reason: str | None = None

    @property
    def exit_code(self):
        return 1 if self.status == UNHEALTHY else 0

    def format(self):
        values = [
            f"STATUS={self.status}",
            f"DATABASE={self.database}",
            f"SCANNER={self.scanner}",
            f"TELEGRAM={self.telegram}",
            f"LAST_SCAN_STARTED_AT={self.last_scan_started_at or 'N/D'}",
            f"LAST_SCAN_COMPLETED_AT={self.last_scan_completed_at or 'N/D'}",
            "SECONDS_SINCE_LAST_SCAN="
            + (
                str(self.seconds_since_last_scan)
                if self.seconds_since_last_scan is not None
                else "N/D"
            ),
            f"CONSECUTIVE_FAILURES={self.consecutive_failures}",
            f"LAST_ERROR_TYPE={self.last_error_type or 'N/D'}",
        ]
        if self.reason:
            values.append(f"REASON={self.reason}")
        return "\n".join(values)


def _parse(value):
    if not value:
        return None
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def evaluate(repository: DealRepository, interval_minutes=None, now=None):
    now = now or datetime.now(UTC)
    try:
        state = repository.runtime_status()
        database = "OK"
    except Exception as exc:  # noqa: BLE001 - health must report storage failure
        logger.error(
            "health.unhealthy reason=database_unavailable error=%s", type(exc).__name__
        )
        return HealthReport(
            UNHEALTHY, "ERROR", "UNKNOWN", "UNKNOWN", reason="database_unavailable"
        )

    interval = interval_minutes or _minutes("SCAN_INTERVAL_MINUTES", 10)
    stale_after = _minutes("HEALTH_STALE_AFTER_MINUTES", max(interval * 3, 15))
    startup_grace = _minutes("HEALTH_STARTUP_GRACE_MINUTES", max(interval * 2, 5))
    started = _parse(state.get("last_scan_started_at"))
    completed = _parse(state.get("last_scan_completed_at"))
    finished = _parse(state.get("last_scan_finished_at"))
    created = _parse(state.get("created_at")) or now
    age_base = completed or finished or started or created
    age = max(0, int((now - age_base).total_seconds()))
    failures = int(state.get("consecutive_failures") or 0)
    telegram_failures = int(state.get("telegram_consecutive_failures") or 0)
    telegram = (
        "OK"
        if state.get("last_telegram_activity_at") and not telegram_failures
        else ("DEGRADED" if telegram_failures else "UNKNOWN")
    )

    if completed is None and now - created <= timedelta(minutes=startup_grace):
        return HealthReport(
            HEALTHY,
            database,
            "STARTING",
            telegram,
            state.get("last_scan_started_at"),
            None,
            age,
            failures,
            state.get("last_error_type"),
            "startup_grace",
        )
    if completed is None and (
        finished is None or now - finished > timedelta(minutes=stale_after)
    ):
        logger.error("health.unhealthy reason=scan_never_completed")
        return HealthReport(
            UNHEALTHY,
            database,
            "STALE",
            telegram,
            state.get("last_scan_started_at"),
            None,
            age,
            failures,
            state.get("last_error_type"),
            "scan_never_completed",
        )
    if finished is None or now - finished > timedelta(minutes=stale_after):
        logger.error("health.unhealthy reason=scanner_stale")
        return HealthReport(
            UNHEALTHY,
            database,
            "STALE",
            telegram,
            state.get("last_scan_started_at"),
            state.get("last_scan_completed_at"),
            age,
            failures,
            state.get("last_error_type"),
            "scanner_stale",
        )
    if failures or telegram_failures:
        logger.warning(
            "health.degraded scan_failures=%s telegram_failures=%s",
            failures,
            telegram_failures,
        )
        return HealthReport(
            DEGRADED,
            database,
            "DEGRADED" if failures else "OK",
            telegram,
            state.get("last_scan_started_at"),
            state.get("last_scan_completed_at"),
            age,
            failures,
            state.get("last_error_type"),
            "recent_errors",
        )
    return HealthReport(
        HEALTHY,
        database,
        "OK",
        telegram,
        state.get("last_scan_started_at"),
        state.get("last_scan_completed_at"),
        age,
        failures,
        None,
    )


def check(path):
    repository = DealRepository(path)
    report = evaluate(repository)
    print(report.format())
    return report.exit_code
