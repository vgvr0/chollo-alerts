from datetime import UTC, datetime, timedelta
from decimal import Decimal

from chollometro_alerts.intent_router import classify_alert_operation
from chollometro_alerts.models import Deal
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.service import AlertService
from chollometro_alerts.telegram_rules import TelegramRuleController


class FailingTranslator:
    def __init__(self):
        self.calls = 0

    def interpret_alert(self, _text):
        self.calls += 1
        raise AssertionError("recent deals must not call the LLM")


def controller(repository, *, service=None, translator=None, multiuser=False):
    return TelegramRuleController(
        bot_token="token",
        authorized_chat_id="42",
        repository=repository,
        translator=translator or FailingTranslator(),
        service=service,
        multiuser_enabled=multiuser,
    )


def save_deals(repository, count=6):
    newest = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    for index in range(count):
        repository.upsert(
            Deal(
                deal_id=f"deal-{index}",
                title=f"Deal {index}",
                url=f"https://example.test/{index}",
                price=Decimal("69.99") + index,
                merchant="Amazon",
                temperature=420 - index,
                category="generic",
                published_at=newest - timedelta(minutes=index),
            )
        )


def test_recent_phrases_are_deterministic_and_do_not_reach_alert_nlp():
    assert classify_alert_operation("últimos chollos") == "RECENT_DEALS"
    assert classify_alert_operation("dime los últimos 5 chollos") == "RECENT_DEALS"
    assert classify_alert_operation("muéstrame los últimos 10") == "RECENT_DEALS"
    assert classify_alert_operation("listar últimos chollos") == "RECENT_DEALS"


def test_recent_deals_default_to_five(tmp_path):
    repository = DealRepository(tmp_path / "recent.sqlite3")
    save_deals(repository)
    translator = FailingTranslator()

    reply = controller(repository, translator=translator)._reply_to("últimos chollos")

    assert (
        sum(
            line.startswith(tuple(f"{n}. " for n in range(1, 10)))
            for line in reply.splitlines()
        )
        == 5
    )
    assert "Deal 0" in reply
    assert translator.calls == 0


def test_recent_deals_honor_requested_limit_and_maximum(tmp_path):
    repository = DealRepository(tmp_path / "limits.sqlite3")
    save_deals(repository, count=25)
    service = AlertService(None, repository, None)

    three = controller(repository, service=service)._reply_to("últimos 3 chollos")
    maximum = controller(repository, service=service)._reply_to("últimos 100 chollos")

    assert (
        sum(
            line.startswith(tuple(f"{n}. " for n in range(1, 10)))
            for line in three.splitlines()
        )
        == 3
    )
    assert len(service.recent_deals(100)) == 20
    assert sum(line.startswith("20. ") for line in maximum.splitlines()) == 1
    assert sum(line.startswith("21. ") for line in maximum.splitlines()) == 0


def test_recent_deals_are_newest_first(tmp_path):
    repository = DealRepository(tmp_path / "order.sqlite3")
    save_deals(repository, count=3)

    deals = AlertService(None, repository, None).recent_deals(3)

    assert [deal.deal_id for deal in deals] == ["deal-0", "deal-1", "deal-2"]


def test_recent_deals_empty_database_is_clear(tmp_path):
    repository = DealRepository(tmp_path / "empty.sqlite3")

    reply = controller(repository)._reply_to("listar últimos chollos")

    assert reply == "No hay chollos persistidos todavía."


def test_recent_deals_format_includes_available_fields(tmp_path):
    repository = DealRepository(tmp_path / "format.sqlite3")
    save_deals(repository, count=1)

    reply = controller(repository)._reply_to("muéstrame los últimos 1")

    assert "1. Deal 0 — 69,99 € — 420° — Amazon" in reply
    assert "🕒 26/09/2026 14:00" in reply
    assert "https://example.test/0" in reply


def test_recent_deals_do_not_create_or_modify_alerts(tmp_path):
    repository = DealRepository(tmp_path / "read-only.sqlite3")
    save_deals(repository, count=1)
    before = {
        "rules": repository.db.execute("SELECT * FROM alert_rules").fetchall(),
        "context": repository.db.execute("SELECT * FROM alert_context").fetchall(),
    }

    controller(repository)._reply_to("últimos chollos")

    after = {
        "rules": repository.db.execute("SELECT * FROM alert_rules").fetchall(),
        "context": repository.db.execute("SELECT * FROM alert_context").fetchall(),
    }
    assert after == before


def test_recent_deals_are_shared_without_leaking_multiuser_alert_state(tmp_path):
    repository = DealRepository(tmp_path / "multiuser.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    save_deals(repository, count=1)
    first_controller = controller(repository, multiuser=True)
    second_controller = controller(repository, multiuser=True)
    first_controller.current_user_id = first.id
    second_controller.current_user_id = second.id

    first_reply = first_controller._reply_to("últimos chollos")
    second_reply = second_controller._reply_to("últimos chollos")

    assert first_reply == second_reply
    assert "Deal 0" in first_reply
    assert repository.list_alert_rules(user_id=first.id) == []
    assert repository.list_alert_rules(user_id=second.id) == []


def test_existing_alert_operations_keep_their_routes():
    assert classify_alert_operation("Qué alertas tengo") == "LIST_ALERTS"
    assert classify_alert_operation("Avísame de leche") == "CREATE_ALERT"
    assert classify_alert_operation("Elimina la alerta de leche") == "DELETE_ALERT"
