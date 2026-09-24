"""Deterministic temperature momentum calculations.

Velocity is estimated with ordinary least squares over all snapshots in the
window, rather than from the last two observations.  This is small, stable,
and naturally handles irregular polling intervals and unchanged temperatures.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite

DEFAULT_MOMENTUM_WINDOWS = (5, 15, 30, 60)


def threshold_transition(
    was_above: bool,
    velocity: float | None,
    *,
    trigger: float = 3.0,
    reset: float = 2.0,
) -> tuple[bool, bool]:
    """Return ``(is_above, should_alert)`` using configurable hysteresis."""
    if velocity is None:
        return was_above, False
    if was_above:
        return (False, False) if velocity < reset else (True, False)
    return (True, True) if velocity >= trigger else (False, False)


@dataclass(frozen=True)
class TemperatureSnapshot:
    thread_id: str
    temperature: float
    observed_at: datetime


@dataclass(frozen=True)
class TemperatureMomentum:
    current_temperature: float
    velocity_5m: float | None
    velocity_15m: float | None
    velocity_30m: float | None
    velocity_60m: float | None
    age_minutes: float | None
    age_normalized_heat: float | None
    acceleration: float | None = None

    def velocity_for(self, minutes: int) -> float | None:
        return {
            5: self.velocity_5m,
            15: self.velocity_15m,
            30: self.velocity_30m,
            60: self.velocity_60m,
        }.get(minutes)


def recent_deal(deal, now: datetime, max_age_hours: float = 3) -> bool:
    """Use provider publication time; missing timestamps are not eligible."""
    published_at = getattr(deal, "published_at", None)
    if published_at is None:
        return False
    now = _utc(now)
    age = now - _utc(published_at)
    return timedelta(0) <= age <= timedelta(hours=max_age_hours)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _velocity(snapshots: list[TemperatureSnapshot], now: datetime, minutes: int):
    cutoff = _utc(now) - timedelta(minutes=minutes)
    points = [s for s in snapshots if cutoff <= _utc(s.observed_at) <= _utc(now)]
    # Two observations at the same timestamp cannot define a rate.
    if len(points) < 2:
        return None
    xs = [(_utc(s.observed_at) - cutoff).total_seconds() / 60 for s in points]
    ys = [float(s.temperature) for s in points]
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return None
    value = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    return value if isfinite(value) else None


def calculate_momentum(
    snapshots: list[TemperatureSnapshot],
    now: datetime,
    *,
    published_at: datetime | None = None,
    minimum_age_minutes: float = 1.0,
) -> TemperatureMomentum | None:
    """Calculate all supported rates; insufficient windows are ``None``."""
    if not snapshots:
        return None
    ordered = sorted(snapshots, key=lambda item: _utc(item.observed_at))
    current = ordered[-1]
    age = None
    if published_at is not None:
        age = max(0.0, (_utc(now) - _utc(published_at)).total_seconds() / 60)
    normalized = (
        float(current.temperature) / max(age, minimum_age_minutes)
        if age is not None
        else None
    )
    velocities = [
        _velocity(ordered, now, window) for window in DEFAULT_MOMENTUM_WINDOWS
    ]
    return TemperatureMomentum(
        current_temperature=float(current.temperature),
        velocity_5m=velocities[0],
        velocity_15m=velocities[1],
        velocity_30m=velocities[2],
        velocity_60m=velocities[3],
        age_minutes=age,
        age_normalized_heat=normalized,
    )
