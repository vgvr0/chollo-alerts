"""Small persisted state machine for opt-in GraphQL polling acceleration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .errors import SCAN_SUCCESS

logger = logging.getLogger(__name__)

NORMAL = "NORMAL"
ACCELERATED = "ACCELERATED"

_PREFIX = "adaptive_polling_"


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AdaptivePollingSnapshot:
    mode: str
    interval_seconds: int
    last_ratio: float | None
    risk: bool
    low_cycles: int
    accelerated_cycles: int
    activations: int
    deactivations: int

    def as_dict(self):
        return {
            "polling_mode": self.mode,
            "polling_interval_seconds": self.interval_seconds,
            "last_feed_window_ratio": self.last_ratio,
            "feed_window_risk": self.risk,
            "adaptive_polling_low_cycles": self.low_cycles,
            "adaptive_polling_accelerated_cycles": self.accelerated_cycles,
            "adaptive_polling_activations": self.activations,
            "adaptive_polling_deactivations": self.deactivations,
        }


class AdaptivePollingController:
    """Own polling cadence; the daemon remains the only scheduler."""

    def __init__(self, repository, settings, normal_interval_seconds):
        if settings.enabled:
            settings.validate_against(normal_interval_seconds)
        self.repository = repository
        self.settings = settings
        self.normal_interval_seconds = normal_interval_seconds
        self._enabled = settings.enabled
        self._mode = NORMAL
        self._low_cycles = 0
        self._accelerated_cycles = 0
        self._activations = 0
        self._deactivations = 0
        self._last_ratio = None
        self._risk = False
        self._load()
        if not self._enabled:
            self._mode = NORMAL
            self._low_cycles = 0
            self._persist()

    def _load(self):
        if not self._enabled:
            return
        mode = self.repository.feed_state(f"{_PREFIX}mode")
        if mode in {NORMAL, ACCELERATED}:
            self._mode = mode
        self._low_cycles = max(
            0, _int(self.repository.feed_state(f"{_PREFIX}low_cycles"))
        )
        self._accelerated_cycles = max(
            0, _int(self.repository.feed_state(f"{_PREFIX}accelerated_cycles"))
        )
        self._activations = max(
            0, _int(self.repository.feed_state(f"{_PREFIX}activations"))
        )
        self._deactivations = max(
            0, _int(self.repository.feed_state(f"{_PREFIX}deactivations"))
        )
        raw_ratio = self.repository.feed_state(f"{_PREFIX}last_ratio")
        try:
            self._last_ratio = float(raw_ratio) if raw_ratio else None
        except (TypeError, ValueError):
            self._last_ratio = None
        self._risk = self.repository.feed_state(f"{_PREFIX}last_risk") == "1"

    @property
    def mode(self):
        return self._mode

    @property
    def interval_seconds(self):
        if self._enabled and self._mode == ACCELERATED:
            return self.settings.interval_seconds
        return self.normal_interval_seconds

    def snapshot(self):
        return AdaptivePollingSnapshot(
            self._mode,
            self.interval_seconds,
            self._last_ratio,
            self._risk,
            self._low_cycles,
            self._accelerated_cycles,
            self._activations,
            self._deactivations,
        )

    def observe(self, service):
        """Consume one completed scan without changing scan/error semantics."""
        ratio = getattr(service, "last_feed_window_ratio", None)
        risk = bool(getattr(service, "last_feed_risk_detected", False))
        self._last_ratio = ratio
        self._risk = risk
        if not self._enabled:
            return self.snapshot()

        feed_ok = getattr(service, "last_feed_status", None) == "OK"
        scan_ok = getattr(service, "last_scan_status", None) == SCAN_SUCCESS
        complete_feed = feed_ok and scan_ok
        high_activity = (
            getattr(service, "last_feed_overlap", None) is not None
            and ratio is not None
            and ratio >= self.settings.trigger_ratio
        )
        should_accelerate = high_activity or risk

        if self._mode == NORMAL:
            self._low_cycles = 0
            if should_accelerate:
                self._mode = ACCELERATED
                self._activations += 1
                logger.info(
                    "adaptive_polling.activated ratio=%s risk=%s interval_seconds=%s",
                    "N/D" if ratio is None else f"{ratio:.3f}",
                    risk,
                    self.settings.interval_seconds,
                )
        elif not complete_feed or should_accelerate or ratio is None:
            # Errors, fallback and partial recovery are not low activity.
            self._low_cycles = 0
        elif ratio <= self.settings.reset_ratio:
            self._low_cycles += 1
            if self._low_cycles >= self.settings.cooldown_cycles:
                self._mode = NORMAL
                self._low_cycles = 0
                self._deactivations += 1
                logger.info(
                    "adaptive_polling.deactivated interval_seconds=%s",
                    self.normal_interval_seconds,
                )
        else:
            self._low_cycles = 0

        if self._mode == ACCELERATED:
            self._accelerated_cycles += 1
        snapshot = self.snapshot()
        self._persist()
        logger.info(
            "adaptive_polling.cycle mode=%s ratio=%s risk=%s low_cycles=%s interval_seconds=%s",
            snapshot.mode,
            "N/D" if snapshot.last_ratio is None else f"{snapshot.last_ratio:.3f}",
            snapshot.risk,
            snapshot.low_cycles,
            snapshot.interval_seconds,
        )
        return snapshot

    def _persist(self):
        values = {
            "mode": self._mode,
            "low_cycles": str(self._low_cycles),
            "accelerated_cycles": str(self._accelerated_cycles),
            "activations": str(self._activations),
            "deactivations": str(self._deactivations),
            "last_ratio": "" if self._last_ratio is None else str(self._last_ratio),
            "interval_seconds": str(self.interval_seconds),
            "last_risk": "1" if self._risk else "0",
        }
        for key, value in values.items():
            self.repository.set_feed_state(f"{_PREFIX}{key}", value)
