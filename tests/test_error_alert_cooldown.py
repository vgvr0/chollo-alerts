"""`ERROR_ALERT_COOLDOWN_MINUTES` is really read from the environment.

The variable was documented in `.env.example` from the beginning while the code
kept a fixed 60 minutes. The default does not change; what changes is that
setting it now has an effect, and an unusable value fails closed at startup
instead of silently keeping the old one.
"""

from unittest.mock import Mock

import pytest

from chollometro_alerts.config import (
    DEFAULT_ERROR_ALERT_COOLDOWN_MINUTES,
    ConfigurationError,
    error_alert_cooldown_minutes,
)
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.service import AlertService


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("ERROR_ALERT_COOLDOWN_MINUTES", raising=False)


def test_the_cooldown_defaults_to_sixty_minutes():
    assert DEFAULT_ERROR_ALERT_COOLDOWN_MINUTES == 60
    assert error_alert_cooldown_minutes() == 60


def test_an_empty_value_keeps_the_default(monkeypatch):
    monkeypatch.setenv("ERROR_ALERT_COOLDOWN_MINUTES", "")
    assert error_alert_cooldown_minutes() == 60


def test_the_cooldown_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("ERROR_ALERT_COOLDOWN_MINUTES", "15")
    assert error_alert_cooldown_minutes() == 15


@pytest.mark.parametrize("value", ["0", "-5", "soon"])
def test_an_unusable_cooldown_is_rejected(monkeypatch, value):
    monkeypatch.setenv("ERROR_ALERT_COOLDOWN_MINUTES", value)
    with pytest.raises(ConfigurationError, match="ERROR_ALERT_COOLDOWN_MINUTES"):
        error_alert_cooldown_minutes()


def test_the_service_uses_the_configured_cooldown(monkeypatch, tmp_path):
    monkeypatch.setenv("ERROR_ALERT_COOLDOWN_MINUTES", "120")
    repository = DealRepository(tmp_path / "errors.db")
    repository.error_alert_allowed = Mock(return_value=True)
    service = AlertService(Mock(), repository, Mock(dry_run=False))

    assert service.notify_error("HTTP_503", "ChollometroClient", "boom") is True
    assert repository.error_alert_allowed.call_args.args[-1] == 120

    # An explicit cooldown (the tests use this) still wins over the setting.
    assert service.notify_error("HTTP_503", "ChollometroClient", "boom", 5) is True
    assert repository.error_alert_allowed.call_args.args[-1] == 5


def test_the_operational_alerts_of_a_scan_use_the_configured_cooldown(
    monkeypatch, tmp_path
):
    """The provider-failure alert path passes no cooldown: it uses the setting."""
    monkeypatch.setenv("ERROR_ALERT_COOLDOWN_MINUTES", "90")
    repository = DealRepository(tmp_path / "errors.db")
    repository.error_alert_allowed = Mock(return_value=True)
    service = AlertService(Mock(), repository, Mock(dry_run=False))

    service.notify_error("TIMEOUT", "ChollometroClient", "timeout")

    assert repository.error_alert_allowed.call_args.args[-1] == 90
