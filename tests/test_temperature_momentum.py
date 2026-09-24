from datetime import UTC, datetime, timedelta

from chollometro_alerts.temperature_momentum import (
    TemperatureSnapshot,
    calculate_momentum,
    recent_deal,
    threshold_transition,
)


def snaps(values, start=datetime(2026, 1, 1, tzinfo=UTC)):
    return [
        TemperatureSnapshot("123", value, start + timedelta(minutes=minute))
        for minute, value in values
    ]


def test_velocity_windows_and_linear_regression():
    now = datetime(2026, 1, 1, 0, 15, tzinfo=UTC)
    result = calculate_momentum(
        snaps([(0, 20), (15, 80)]), now, published_at=now - timedelta(minutes=15)
    )
    assert result.velocity_15m == 4
    assert result.velocity_5m is None
    assert result.velocity_30m is not None
    assert result.velocity_60m is not None


def test_representative_ramp_is_four_degrees_per_minute():
    now = datetime(2026, 1, 1, 0, 15, tzinfo=UTC)
    result = calculate_momentum(
        snaps([(0, 20), (5, 30), (10, 50), (15, 80)]),
        now,
        published_at=now - timedelta(minutes=15),
    )
    assert result.velocity_15m == 4
    assert result.velocity_15m >= 3.0


def test_stable_and_descending_temperature():
    now = datetime(2026, 1, 1, 0, 15, tzinfo=UTC)
    assert calculate_momentum(snaps([(0, 50), (15, 50)]), now).velocity_15m == 0
    assert calculate_momentum(snaps([(0, 80), (15, 70)]), now).velocity_15m == -2 / 3


def test_recent_window_is_inclusive_and_missing_history_is_none():
    now = datetime(2026, 1, 1, 3, tzinfo=UTC)

    class Deal:
        published_at = now - timedelta(hours=3)

    assert recent_deal(Deal(), now)
    Deal.published_at = now - timedelta(hours=3, minutes=1)
    assert not recent_deal(Deal(), now)
    assert calculate_momentum(snaps([(0, 20)]), now).velocity_15m is None


def test_hysteresis_alerts_only_on_crossings():
    state, alert = threshold_transition(False, 3.5)
    assert (state, alert) == (True, True)
    state, alert = threshold_transition(state, 3.8)
    assert (state, alert) == (True, False)
    state, alert = threshold_transition(state, 1.5)
    assert (state, alert) == (False, False)
    assert threshold_transition(state, 3.5) == (True, True)
