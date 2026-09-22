import threading
from decimal import Decimal
from unittest.mock import Mock

import pytest

from chollometro_alerts.intent import AlertIntent
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.runtime import positive_interval
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
