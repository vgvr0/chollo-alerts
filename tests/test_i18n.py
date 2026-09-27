from chollometro_alerts.i18n import Translator
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.telegram_rules import TelegramRuleController


def test_translator_locales_interpolation_and_fallback():
    translator = Translator()
    assert (
        translator.t("language.changed", "en", language="English")
        == "Language changed to English."
    )
    assert (
        translator.t("language.changed", "es", language="Español")
        == "Idioma cambiado a Español."
    )
    assert (
        translator.t("language.changed", "fr", language="Español")
        == "Idioma cambiado a Español."
    )
    assert translator.t("does.not.exist", "en") == "does.not.exist"


def test_user_language_is_persisted_with_spanish_default(tmp_path):
    path = tmp_path / "users.sqlite3"
    repository = DealRepository(path)
    user = repository.create_user(telegram_user_id="1", telegram_chat_id="chat")
    assert user.language == "es"
    repository.update_user_language(user.id, "en")
    repository.close()
    assert DealRepository(path).user_for_id(user.id).language == "en"


def test_language_command_changes_reply_language_and_keeps_site_independent(tmp_path):
    repository = DealRepository(tmp_path / "telegram.sqlite3")
    controller = TelegramRuleController(
        bot_token="token",
        authorized_chat_id="chat",
        repository=repository,
        translator=None,
    )
    sent = []
    controller.send_message = sent.append
    assert (
        controller.process_update(
            {
                "update_id": 1,
                "message": {"chat": {"id": "chat"}, "text": "/language en"},
            }
        )
        == "Language changed to English."
    )
    assert repository.user_for_id(controller.current_language_user_id).language == "en"
    assert controller.process_update(
        {"update_id": 2, "message": {"chat": {"id": "chat"}, "text": "/language"}}
    ).startswith("Current language: English")
    assert repository.user_for_id(controller.current_language_user_id).language == "en"
