import json
from importlib.resources import files

from chollometro_alerts.i18n import SUPPORTED_LANGUAGES, Translator
from chollometro_alerts.pepper_config import get_pepper_site
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


def test_locale_catalogs_have_exactly_the_same_keys():
    catalogs = {
        language: set(
            json.loads(
                files("chollometro_alerts.i18n.locales")
                .joinpath(f"{language}.json")
                .read_text(encoding="utf-8")
            )
        )
        for language in SUPPORTED_LANGUAGES
    }
    assert catalogs["es"] == catalogs["en"]


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


def test_site_selection_does_not_change_user_language(tmp_path, monkeypatch):
    repository = DealRepository(tmp_path / "site.sqlite3")
    user = repository.create_user(
        telegram_user_id="1", telegram_chat_id="chat", language="en"
    )
    monkeypatch.setenv("PEPPER_SITE", "pepper_us")
    assert get_pepper_site().name == "pepper_us"
    assert repository.user_for_id(user.id).language == "en"
