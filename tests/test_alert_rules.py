from decimal import Decimal
from unittest.mock import Mock

from chollometro_alerts.intent import AlertIntent, validate_intent
from chollometro_alerts.repository import DealRepository


def test_intent_validation_and_ambiguity():
    intent = validate_intent(
        AlertIntent(
            action="create",
            query="leche",
            product_type="leche",
            max_price=Decimal("0.80"),
            price_unit="liter",
        )
    )
    assert intent.max_price == Decimal("0.80")
    try:
        validate_intent(AlertIntent(action="create", query="cerveza"))
    except ValueError:
        pass
    else:
        raise AssertionError("ambiguous intent must be rejected")


def test_rules_are_persistent_deduplicated_and_updates_are_idempotent(tmp_path):
    path = tmp_path / "rules.sqlite3"
    repo = DealRepository(path)
    intent = AlertIntent(
        action="create",
        query="leche",
        product_type="leche",
        max_price=Decimal("0.80"),
        price_unit="liter",
    )
    repo.apply_alert_intent(intent)
    repo.apply_alert_intent(intent)
    assert len(repo.list_alert_rules()) == 1
    assert repo.claim_telegram_update(10)
    assert not repo.claim_telegram_update(10)
    repo.db.close()
    assert len(DealRepository(path).list_alert_rules()) == 1


def test_unauthorized_update_is_ignored_without_translation_or_database_change(
    tmp_path,
):
    from chollometro_alerts.telegram_rules import TelegramRuleController

    class Translator:
        def interpret_alert(self, _text):
            raise AssertionError("translator must not be called")

    class Sender(TelegramRuleController):
        def send_message(self, _text):
            raise AssertionError("reply must not be sent")

    repo = DealRepository(tmp_path / "rules.sqlite3")
    controller = Sender(
        bot_token="token",
        authorized_chat_id="123",
        repository=repo,
        translator=Translator(),
    )
    assert (
        controller.process_update(
            {"update_id": 1, "message": {"chat": {"id": 999}, "text": "create"}}
        )
        is None
    )
    assert repo.list_alert_rules() == []


def test_telegram_credentials_have_distinct_api_and_authorization_roles(
    tmp_path, monkeypatch
):
    from chollometro_alerts.telegram_rules import TelegramRuleController

    repo = DealRepository(tmp_path / "rules.sqlite3")
    translator = Mock()
    controller = TelegramRuleController(
        bot_token="bot-token",
        authorized_chat_id="chat-123",
        repository=repo,
        translator=translator,
    )
    get = Mock()
    get.return_value.json.return_value = {"result": []}
    get.return_value.raise_for_status = Mock()
    post = Mock()
    post.raise_for_status = Mock()
    monkeypatch.setattr("chollometro_alerts.telegram_rules.requests.get", get)
    monkeypatch.setattr("chollometro_alerts.telegram_rules.requests.post", post)

    controller.poll_once()
    controller.send_message("ok")

    get.assert_called_once()
    assert get.call_args.args[0] == "https://api.telegram.org/botbot-token/getUpdates"
    assert "chat-123" not in get.call_args.args[0]
    post.assert_called_once_with(
        "https://api.telegram.org/botbot-token/sendMessage",
        json={"chat_id": "chat-123", "text": "ok"},
        timeout=20,
    )


def test_authorization_uses_chat_id_not_bot_token(tmp_path):
    from chollometro_alerts.telegram_rules import TelegramRuleController

    repo = DealRepository(tmp_path / "rules.sqlite3")
    translator = Mock()
    controller = TelegramRuleController(
        bot_token="bot-token",
        authorized_chat_id="chat-123",
        repository=repo,
        translator=translator,
    )
    assert (
        controller.process_update(
            {"update_id": 1, "message": {"chat": {"id": "bot-token"}, "text": "x"}}
        )
        is None
    )
    translator.interpret_alert.assert_not_called()
