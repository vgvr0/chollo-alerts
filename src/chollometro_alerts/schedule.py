"""Per-alert notification windows: *when* Telegram may be sent.

The window never decides whether a deal matches. A deal that matches outside
the window is still a match: it is persisted as pending and delivered at the
first cycle that falls inside the window, so a chollo found at 03:00 is never
lost.

All comparisons are made on **local wall-clock time** of the alert's own
timezone (`zoneinfo`, `Europe/Madrid` by default, overridable with
`ALERT_TIMEZONE`), never by comparing an UTC timestamp with a local hour:
`ZoneInfo` applies the real offset of that instant, so the window keeps
working across DST changes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Madrid"
TIMEZONE_ENV = "ALERT_TIMEZONE"

# `HH:MM`, `H:MM` or a bare hour; the minutes default to zero.
_TIME_RE = re.compile(r"^(?P<hour>[01]?\d|2[0-3])(?::(?P<minute>[0-5]\d))?$")


def default_timezone() -> str:
    """The timezone of an alert that does not name one."""
    configured = os.getenv(TIMEZONE_ENV, "").strip()
    return configured or DEFAULT_TIMEZONE


def validate_timezone(name) -> str:
    """An IANA timezone name, or `ValueError` if the zone is unknown."""
    text = str(name or "").strip()
    if not text:
        raise ValueError("la timezone no puede estar vacía")
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"timezone desconocida: {text}") from exc
    return text


def parse_time(value) -> time:
    """Parse `08:00`, `8:00` or `08` into a naive `time`."""
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0, tzinfo=None)
    match = _TIME_RE.match(str(value).strip()) if value is not None else None
    if match is None:
        raise ValueError(f"hora inválida: {value!r} (usa el formato HH:MM)")
    return time(int(match.group("hour")), int(match.group("minute") or 0))


def as_aware_utc(moment: datetime) -> datetime:
    """Read a naive timestamp as UTC (the only interpretation used here)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class NotificationWindow:
    """One alert's daily window, expressed in one IANA timezone."""

    start: time
    end: time
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self):
        object.__setattr__(self, "start", parse_time(self.start))
        object.__setattr__(self, "end", parse_time(self.end))
        object.__setattr__(self, "timezone", validate_timezone(self.timezone))

    @property
    def crosses_midnight(self) -> bool:
        """`22:00 → 07:00` is a window that spans two calendar days."""
        return self.end < self.start

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def local_time(self, moment: datetime) -> time:
        return as_aware_utc(moment).astimezone(self.zone).time()

    def allows(self, moment: datetime) -> bool:
        """True when Telegram may be sent at `moment` (inclusive bounds)."""
        local = self.local_time(moment)
        if self.start == self.end:
            # A zero-width window is documented as the whole day: it can never
            # silence an alert by accident.
            return True
        if self.crosses_midnight:
            return local >= self.start or local <= self.end
        return self.start <= local <= self.end

    def describe(self) -> str:
        return f"{self.start:%H:%M}–{self.end:%H:%M} {self.timezone}"


def notification_allowed(window: NotificationWindow | None, moment: datetime) -> bool:
    """No window means the original behaviour: notify immediately."""
    return True if window is None else window.allows(moment)


def window_from_alert_rule(alert_rule) -> NotificationWindow | None:
    """The domain window of a persisted rule, or None when the alert has none."""
    if alert_rule is None:
        return None
    configured = getattr(alert_rule, "notification_window", None)
    if configured is None:
        return None
    return NotificationWindow(configured.start, configured.end, configured.timezone)
