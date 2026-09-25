from decimal import Decimal

import pytest

from chollometro_alerts.config import ConfigurationError, TelegramSettings
from chollometro_alerts.intent import AlertIntent
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.telegram_rules import TelegramRuleController


class FakeTranslator:
    def __init__(self, intent=None, error=None):
        self.intent = intent
        self.error = error
        self.calls = 0

    def interpret_alert(self, _text):
        self.calls += 1
        if self.error:
            raise self.error
        return self.intent


class LocalController(TelegramRuleController):
    def send_message(self, _text):
        pass


def update(text, update_id=1):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": "42"}, "text": text},
    }


def controller(tmp_path, translator, mode="hybrid"):
    repository = DealRepository(tmp_path / "alerts.db")
    bot = LocalController(
        bot_token="token",
        authorized_chat_id="42",
        repository=repository,
        translator=translator,
        alert_nlp_mode=mode,
    )
    return bot, repository


def test_deterministic_mode_keeps_unit_price_fast_path_without_llm(tmp_path):
    translator = FakeTranslator(
        AlertIntent(
            action="create",
            query="invented",
            max_price=Decimal(1),
            price_unit="absolute",
        )
    )
    bot, repository = controller(tmp_path, translator, "deterministic")

    reply = bot.process_update(
        update("Quiero alertas de leche por debajo de 0,8€ el litro")
    )

    assert "0,80 €/L" in reply
    assert translator.calls == 0
    assert bot.last_interpretation_method == "deterministic"
    assert repository.load_alert_rule(1).constraints.max_price_per_liter == Decimal(
        "0.8"
    )


def test_hybrid_mode_uses_existing_llm_for_non_deterministic_alert(tmp_path):
    translator = FakeTranslator(
        AlertIntent(
            action="create",
            query="zapatillas",
            product_type="zapatillas",
            brand="ASICS",
            max_price=Decimal(100),
            price_unit="absolute",
            exclude_merchants=["AliExpress"],
        )
    )
    bot, repository = controller(tmp_path, translator, "hybrid")

    bot.process_update(
        update("Quiero zapatillas ASICS por menos de 100€ y no quiero AliExpress")
    )

    assert translator.calls == 1
    rule = repository.load_alert_rule(1)
    assert rule.brand == "ASICS"
    assert rule.constraints.max_price == Decimal(100)
    assert rule.exclude_merchants == ("AliExpress",)


def test_llm_first_uses_structured_intent_and_preserves_combined_constraints(tmp_path):
    translator = FakeTranslator(
        AlertIntent(
            action="create",
            query="leche",
            product_type="leche",
            max_price=Decimal("0.8"),
            price_unit="liter",
            temperature_min=300,
            include_merchants=["Amazon", "PcComponentes"],
        )
    )
    bot, repository = controller(tmp_path, translator, "llm_first")

    bot.process_update(
        update("Leche por debajo de 80 céntimos por litro y más de 300 grados", 2)
    )

    assert translator.calls == 1
    rule = repository.load_alert_rule(1)
    assert rule.constraints.max_price_per_liter == Decimal("0.8")
    assert rule.constraints.temperature_min == 300
    assert rule.include_merchants == ("Amazon", "PcComponentes")


def test_llm_first_falls_back_to_deterministic_on_provider_error(tmp_path):
    translator = FakeTranslator(error=TimeoutError("secret provider detail"))
    bot, repository = controller(tmp_path, translator, "llm_first")

    reply = bot.process_update(
        update("Quiero alertas de leche por debajo de 0,8€ el litro")
    )

    assert "0,80 €/L" in reply
    assert bot.last_interpretation_method == "fallback"
    assert repository.load_alert_rule(1).constraints.max_price_per_liter == Decimal(
        "0.8"
    )


def test_telegram_alert_nlp_mode_is_validated_early(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_ALERT_NLP_MODE", "unknown")

    with pytest.raises(ConfigurationError, match="TELEGRAM_ALERT_NLP_MODE"):
        TelegramSettings.from_env()
