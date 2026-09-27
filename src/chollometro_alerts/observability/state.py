"""Thread-safe in-memory state shared by health and Prometheus endpoints."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class LastRun:
    status: str
    finished_at: str
    deals_seen: int
    alerts_matched: int
    notifications_sent: int


class ObservabilityState:
    """Process-lifetime state; no extra persistence is introduced."""

    def __init__(self, started_at: str | None = None, registry=None):
        self.started_at = started_at or _now()
        self._started_monotonic = time.monotonic()
        self._lock = threading.RLock()
        self.last_run: LastRun | None = None
        self.last_scan_timestamp = 0.0
        self.last_success_timestamp = 0.0
        self.active_alerts = 0
        self.registry = registry or CollectorRegistry()
        self.runs = Counter(
            "chollometro_runs",
            "Completed or failed crawler runs.",
            ("status",),
            registry=self.registry,
        )
        self.deals = Counter(
            "chollometro_deals_processed",
            "Deals seen by crawler runs.",
            registry=self.registry,
        )
        self.matches = Counter(
            "chollometro_alert_matches",
            "Alert matches found by crawler runs.",
            registry=self.registry,
        )
        self.notifications = Counter(
            "chollometro_notifications_sent",
            "Notifications successfully sent.",
            registry=self.registry,
        )
        self.notification_failures = Counter(
            "chollometro_notification_failures",
            "Notification delivery failures.",
            registry=self.registry,
        )
        self.last_success = Gauge(
            "chollometro_last_success_timestamp",
            "Unix timestamp of the last successful crawler run.",
            registry=self.registry,
        )
        self.last_scan = Gauge(
            "chollometro_last_scan_timestamp",
            "Unix timestamp when the last crawler run started.",
            registry=self.registry,
        )
        self.run_duration = Histogram(
            "chollometro_run_duration_seconds",
            "Crawler run duration in seconds.",
            registry=self.registry,
        )
        self.runs_by_site = Counter(
            "chollometro_runs_by_site",
            "Crawler runs by discovered site and provider.",
            ("status", "site", "provider"),
            registry=self.registry,
        )
        self.deals_by_site = Counter(
            "chollometro_deals_processed_by_site",
            "Deals seen by site and provider.",
            ("site", "provider"),
            registry=self.registry,
        )
        self.matches_by_site = Counter(
            "chollometro_alert_matches_by_site",
            "Alert matches by site and provider.",
            ("site", "provider"),
            registry=self.registry,
        )
        self.notifications_by_site = Counter(
            "chollometro_notifications_sent_by_site",
            "Notifications by site and provider.",
            ("site", "provider"),
            registry=self.registry,
        )
        self.failures_by_site = Counter(
            "chollometro_notification_failures_by_site",
            "Notification failures by site and provider.",
            ("site", "provider"),
            registry=self.registry,
        )
        self.run_duration_by_site = Histogram(
            "chollometro_run_duration_seconds_by_site",
            "Crawler run duration by site and provider.",
            ("site", "provider"),
            registry=self.registry,
        )
        self.http_errors_by_site = Counter(
            "chollometro_http_errors_by_site",
            "HTTP errors observed by site, provider and status.",
            ("site", "provider", "http_status"),
            registry=self.registry,
        )
        self.alerts = Gauge(
            "chollometro_active_alerts",
            "Number of currently enabled alert rules.",
            registry=self.registry,
        )
        self.llm_requests = Counter(
            "chollometro_llm_requests",
            "LLM provider request attempts, including retries.",
            registry=self.registry,
        )
        self.llm_failures = Counter(
            "chollometro_llm_failures",
            "LLM extraction failures after retries.",
            registry=self.registry,
        )
        self.llm_input_tokens = Counter(
            "chollometro_llm_input_tokens",
            "Input tokens reported by the LLM provider.",
            registry=self.registry,
        )
        self.llm_output_tokens = Counter(
            "chollometro_llm_output_tokens",
            "Output tokens reported by the LLM provider.",
            registry=self.registry,
        )
        self.llm_duration = Histogram(
            "chollometro_llm_request_duration_seconds",
            "LLM request duration, including retry attempts.",
            registry=self.registry,
        )
        # Keep the documented status series visible even before the first run.
        for status in ("completed", "failed", "interrupted"):
            self.runs.labels(status=status)

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started_monotonic)

    def mark_scan_started(self):
        timestamp = time.time()
        with self._lock:
            self.last_scan_timestamp = timestamp
            self.last_scan.set(timestamp)

    def record_run(
        self,
        summary,
        status: str,
        duration_seconds: float,
        active_alerts: int,
        site: str = "unknown",
        provider: str = "unknown",
        http_status: int | None = None,
    ):
        """Record one cycle from the canonical ``RunSummary``."""
        status_label = {
            "SUCCESS": "completed",
            "INTERRUPTED": "interrupted",
        }.get(status, "failed")
        finished_at = _now()
        with self._lock:
            self.last_run = LastRun(
                status=status_label,
                finished_at=finished_at,
                deals_seen=int(summary.found),
                alerts_matched=int(summary.interesting),
                notifications_sent=int(summary.telegram_sent),
            )
            self.active_alerts = max(0, int(active_alerts))
            self.runs.labels(status=status_label).inc()
            self.deals.inc(summary.found)
            self.matches.inc(summary.interesting)
            self.notifications.inc(summary.telegram_sent)
            self.notification_failures.inc(summary.errors)
            self.run_duration.observe(max(0.0, duration_seconds))
            self.alerts.set(self.active_alerts)
            self.llm_requests.inc(getattr(summary, "llm_calls", 0))
            self.llm_failures.inc(getattr(summary, "llm_failures", 0))
            self.llm_input_tokens.inc(getattr(summary, "llm_input_tokens", 0))
            self.llm_output_tokens.inc(getattr(summary, "llm_output_tokens", 0))
            self.llm_duration.observe(
                max(0.0, float(getattr(summary, "llm_duration_seconds", 0.0)))
            )
            labels = {"site": site, "provider": provider}
            self.runs_by_site.labels(status=status_label, **labels).inc()
            self.deals_by_site.labels(**labels).inc(summary.found)
            self.matches_by_site.labels(**labels).inc(summary.interesting)
            self.notifications_by_site.labels(**labels).inc(summary.telegram_sent)
            self.failures_by_site.labels(**labels).inc(summary.errors)
            self.run_duration_by_site.labels(**labels).observe(
                max(0.0, duration_seconds)
            )
            if http_status is not None and 400 <= int(http_status) <= 599:
                self.http_errors_by_site.labels(
                    site=site, provider=provider, http_status=str(http_status)
                ).inc()
            if status == "SUCCESS":
                self.last_success_timestamp = time.time()
                self.last_success.set(self.last_success_timestamp)

    def health_payload(self, database="ok"):
        with self._lock:
            payload = {
                "status": "ok",
                "version": _version(),
                "uptime_seconds": int(self.uptime_seconds),
                "database": database,
                "last_scan_at": _iso_timestamp(self.last_scan_timestamp),
                "last_success_at": _iso_timestamp(self.last_success_timestamp),
                "last_run": asdict(self.last_run) if self.last_run else None,
            }
            return payload


def _iso_timestamp(timestamp: float) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("chollo-alerts")
    except PackageNotFoundError:  # source checkouts may be uninstalled
        return "unknown"


def runtime_site(service) -> str:
    """Return a stable site label without requiring a site allow-list."""
    config = getattr(getattr(service, "client", None), "site_config", None)
    if config is not None and getattr(config, "name", None):
        return str(config.name)
    base_url = getattr(getattr(service, "client", None), "base_url", "")
    host = urlparse(base_url).hostname if base_url else None
    return (host or "unknown").removeprefix("www.").lower()
