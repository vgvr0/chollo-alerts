from dataclasses import dataclass

import pytest

from chollometro_alerts.adaptive_polling import (
    ACCELERATED,
    NORMAL,
    AdaptivePollingController,
)
from chollometro_alerts.config import AdaptivePollingSettings, ConfigurationError
from chollometro_alerts.errors import SCAN_PARTIAL, SCAN_SUCCESS
from chollometro_alerts.repository import DealRepository


@dataclass
class Cycle:
    ratio: float | None
    overlap: int | None = 5
    risk: bool = False
    feed_status: str = "OK"
    scan_status: str = SCAN_SUCCESS

    @property
    def last_feed_window_ratio(self):
        return self.ratio

    @property
    def last_feed_overlap(self):
        return self.overlap

    @property
    def last_feed_risk_detected(self):
        return self.risk

    @property
    def last_feed_status(self):
        return self.feed_status

    @property
    def last_scan_status(self):
        return self.scan_status


def settings(**overrides):
    values = {
        "enabled": True,
        "interval_seconds": 120,
        "trigger_ratio": 0.70,
        "reset_ratio": 0.30,
        "cooldown_cycles": 3,
    }
    values.update(overrides)
    return AdaptivePollingSettings(**values)


def make_controller(tmp_path, adaptive_settings=None, normal=600):
    repository = DealRepository(tmp_path / "adaptive.sqlite3")
    controller = AdaptivePollingController(
        repository, adaptive_settings or settings(), normal
    )
    return controller, repository


def test_default_is_disabled_and_legacy_interval_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    monkeypatch.delenv("ADAPTIVE_POLLING_ENABLED", raising=False)
    assert AdaptivePollingSettings.from_env().enabled is False

    controller, repository = make_controller(
        tmp_path, AdaptivePollingSettings(), normal=600
    )
    assert controller.observe(Cycle(1.0, overlap=0, risk=True)).mode == NORMAL
    assert controller.interval_seconds == 600
    assert repository.feed_state("adaptive_polling_mode") == NORMAL


def test_trigger_accelerates_and_low_activity_recovers_after_cooldown(tmp_path):
    controller, _repository = make_controller(tmp_path)

    assert controller.observe(Cycle(0.699)).mode == NORMAL
    assert controller.observe(Cycle(0.70)).mode == ACCELERATED
    assert controller.interval_seconds == 120
    assert controller.observe(Cycle(0.2)).mode == ACCELERATED
    assert controller.observe(Cycle(0.2)).mode == ACCELERATED
    assert controller.observe(Cycle(0.2)).mode == NORMAL
    assert controller.interval_seconds == 600


def test_risk_accelerates_even_with_low_ratio(tmp_path):
    controller, _repository = make_controller(tmp_path)
    snapshot = controller.observe(Cycle(0.0, overlap=0, risk=True))
    assert snapshot.mode == ACCELERATED
    assert snapshot.risk is True


def test_errors_partial_scans_and_fallback_never_count_as_no_activity(tmp_path):
    controller, _repository = make_controller(tmp_path)
    controller.observe(Cycle(0.9))

    for cycle in (
        Cycle(None, feed_status="FALLBACK", scan_status="FAILED"),
        Cycle(0.1, scan_status=SCAN_PARTIAL),
        Cycle(None, feed_status="FALLBACK", scan_status="FAILED"),
    ):
        assert controller.observe(cycle).mode == ACCELERATED
    assert controller.snapshot().low_cycles == 0


def test_restart_restores_mode_and_hysteresis_from_sqlite(tmp_path):
    first, repository = make_controller(tmp_path)
    first.observe(Cycle(0.9))
    first.observe(Cycle(0.2))

    restarted = AdaptivePollingController(repository, settings(), 600)
    assert restarted.mode == ACCELERATED
    assert restarted.snapshot().low_cycles == 1
    assert restarted.observe(Cycle(0.2)).mode == ACCELERATED
    assert restarted.observe(Cycle(0.2)).mode == NORMAL


@pytest.mark.parametrize(
    "name,value",
    [
        ("ADAPTIVE_POLLING_ENABLED", "maybe"),
        ("ADAPTIVE_POLLING_INTERVAL_SECONDS", "29"),
        ("ADAPTIVE_POLLING_INTERVAL_SECONDS", "3601"),
        ("ADAPTIVE_POLLING_TRIGGER_RATIO", "1.1"),
        ("ADAPTIVE_POLLING_TRIGGER_RATIO", "nan"),
        ("ADAPTIVE_POLLING_RESET_RATIO", "0.8"),
        ("ADAPTIVE_POLLING_COOLDOWN_CYCLES", "0"),
        ("ADAPTIVE_POLLING_COOLDOWN_CYCLES", "101"),
    ],
)
def test_invalid_configuration_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        AdaptivePollingSettings.from_env()


def test_accelerated_interval_must_be_below_normal_interval():
    with pytest.raises(ConfigurationError):
        settings(interval_seconds=600).validate_against(600)
