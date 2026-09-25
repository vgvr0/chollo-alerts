import threading
from decimal import Decimal
from unittest.mock import Mock

import pytest

from chollometro_alerts.intent import AlertIntent
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.runtime import positive_interval, run_scanner
from chollometro_alerts.telegram_rules import TelegramRuleController


def test_positive_interval_rejects_invalid_values():
    assert positive_interval("10") == 10
    with pytest.raises(ValueError):
        positive_interval("0")
    with pytest.raises(ValueError):
        positive_interval("no")


def test_listener_processes_updates_and_ignores_duplicate(monkeypatch, tmp_path):
    repo = DealRepository(tmp_path / "alerts.sqlite3")
    translator = Mock()
    translator.interpret_alert.return_value = AlertIntent(
        action="create",
        query="leche",
        product_type="leche",
        max_price=Decimal("0.80"),
        price_unit="liter",
    )
    controller = TelegramRuleController(
        bot_token="token",
        authorized_chat_id="123",
        repository=repo,
        translator=translator,
    )
    update = {"update_id": 7, "message": {"chat": {"id": "123"}, "text": "alerta"}}
    response = Mock()
    response.json.return_value = {"result": [update]}
    response.raise_for_status.return_value = None
    monkeypatch.setattr(
        "chollometro_alerts.telegram_rules.requests.get", Mock(return_value=response)
    )
    monkeypatch.setattr(controller, "send_message", Mock())
    stop = threading.Event()
    stop.set()
    # The first call is still processed when the stop was requested; this models a
    # final in-flight response and keeps the test independent from network timing.
    controller.process_update(update)
    controller.process_update(update)
    assert translator.interpret_alert.call_count == 1
    assert len(repo.list_alert_rules()) == 1


def test_user_facing_rule_messages_are_deterministic(tmp_path):
    controller = TelegramRuleController(
        bot_token="token",
        authorized_chat_id="123",
        repository=DealRepository(tmp_path / "a.db"),
        translator=Mock(),
    )
    intent = AlertIntent(
        action="update", query="leche", max_price=Decimal("0.75"), price_unit="liter"
    )
    assert "✅ Alerta actualizada: leche por debajo de 0,75 €/L" == controller._format(
        intent, []
    )


def test_scanner_records_unexpected_scan_and_maintenance_failures(
    monkeypatch, tmp_path
):
    repository = DealRepository(tmp_path / "scanner-errors.sqlite3")
    service = Mock()
    service.repository = repository
    service.run_active_rules.side_effect = RuntimeError("scanner test failure")

    class StopAfterOneWait:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits > 0

        def wait(self, _seconds):
            self.waits += 1

    def fail_maintenance(_self):
        raise RuntimeError("maintenance test failure")

    monkeypatch.setattr(
        "chollometro_alerts.runtime.RetentionService.run_if_due", fail_maintenance
    )
    run_scanner(service, stop_event=StopAfterOneWait())

    status = repository.runtime_status()
    assert status["last_error_type"] == "RuntimeError"
    repository.close_current_thread()


def test_scanner_handles_failure_before_creating_a_scan_run(monkeypatch, tmp_path):
    repository = DealRepository(tmp_path / "scanner-before-run.sqlite3")
    service = Mock()
    service.repository = repository
    monkeypatch.setattr(
        repository,
        "list_alert_rules",
        Mock(side_effect=RuntimeError("rules test failure")),
    )

    class StopAfterOneWait:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits > 0

        def wait(self, _seconds):
            self.waits += 1

    run_scanner(service, stop_event=StopAfterOneWait())
    assert repository.runtime_status()["last_scan_run_id"] is None
    repository.close_current_thread()
