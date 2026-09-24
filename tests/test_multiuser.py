import sqlite3
from decimal import Decimal
from typing import ClassVar

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.telegram import TelegramNotifier
from chollometro_alerts.telegram_rules import TelegramRuleController


def rule(query):
    return AlertRule(
        query=query,
        product=query,
        constraints=AlertConstraints(max_price=Decimal(100)),
    )


def update(update_id, user_id, chat_id, text):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id, "username": f"u{user_id}", "first_name": "Test"},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def test_legacy_seven_rules_are_assigned_once(monkeypatch, tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE alert_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,
        product_type TEXT, brand TEXT, max_price TEXT NOT NULL,
        price_unit TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(query, product_type, brand, max_price, price_unit)
    )""")
    for index in range(7):
        db.execute(
            "INSERT INTO alert_rules(query,max_price,price_unit,created_at,updated_at) VALUES (?,?,?,?,?)",
            (f"legacy-{index}", "100", "absolute", "created", "updated"),
        )
    db.commit()
    db.close()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "legacy-chat")
    monkeypatch.delenv("TELEGRAM_USER_ID", raising=False)

    repository = DealRepository(path)
    assert len(repository.list_users()) == 1
    user_id = repository.list_users()[0][0]
    assert (
        repository.db.execute(
            "SELECT COUNT(*) FROM alert_rules WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        == 7
    )
    repository.close()
    reopened = DealRepository(path)
    assert len(reopened.list_users()) == 1
    assert (
        reopened.db.execute(
            "SELECT COUNT(*) FROM alert_rules WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        == 7
    )


def test_multiuser_list_and_delete_are_scoped(tmp_path):
    repository = DealRepository(tmp_path / "users.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    first_rule = repository.save_alert_rule(rule("leche"), "leche", user_id=first.id)
    second_rule = repository.save_alert_rule(
        rule("relojes"), "relojes", user_id=second.id
    )
    assert [row[0] for row in repository.list_alert_rules(user_id=first.id)] == [
        first_rule
    ]
    assert [row[0] for row in repository.list_alert_rules(user_id=second.id)] == [
        second_rule
    ]
    assert repository.delete_alert_rule(second_rule, user_id=first.id) is False
    assert (
        repository.replace_alert_rule(first_rule, rule("cafe"), user_id=second.id)
        is False
    )
    assert repository.delete_alert_rule(second_rule, user_id=second.id) is True


def test_unknown_multiuser_is_rejected_and_known_user_can_list(tmp_path):
    repository = DealRepository(tmp_path / "telegram.sqlite3")
    user = repository.create_user(telegram_user_id="42", telegram_chat_id="chat-a")
    repository.save_alert_rule(rule("leche"), "leche", user_id=user.id)
    replies = []

    class Controller(TelegramRuleController):
        def send_message(self, text):
            replies.append(text)

    controller = Controller(
        bot_token="token",
        authorized_chat_id="legacy",
        repository=repository,
        translator=None,
        multiuser_enabled=True,
        auto_register=False,
    )
    assert (
        controller.process_update(update(1, "99", "chat-x", "Qué alertas tengo"))
        is None
    )
    assert "No tienes acceso" in replies[-1]
    assert controller.process_update(update(2, "42", "chat-a", "Qué alertas tengo"))
    assert "leche" in replies[-1].casefold()


def test_notifier_routes_by_rule_owner(monkeypatch, tmp_path):
    repository = DealRepository(tmp_path / "routing.sqlite3")
    user = repository.create_user(telegram_user_id="7", telegram_chat_id="chat-owner")
    rule_id = repository.save_alert_rule(rule("leche"), "leche", user_id=user.id)
    calls = []

    class Response:
        status_code = 200
        headers: ClassVar = {}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        "chollometro_alerts.telegram.requests.post",
        lambda url, **kwargs: calls.append(kwargs) or Response(),
    )
    notifier = TelegramNotifier("token", "global-chat", repository=repository)
    from chollometro_alerts.models import Deal

    notifier.send(
        Deal("d", "Leche", "https://example.test", Decimal(1), "shop", 1, "milk", None),
        rule_id=rule_id,
    )
    assert calls[0]["json"]["chat_id"] == "chat-owner"
