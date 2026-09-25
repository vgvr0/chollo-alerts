from decimal import Decimal

import pytest

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.alert_text import extract_temperature_mentions, merge_intent
from chollometro_alerts.config import InterestRule
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.intent import AlertIntent, intent_to_rule, validate_intent
from chollometro_alerts.intent_router import classify_alert_operation
from chollometro_alerts.models import Deal
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.telegram_rules import TelegramRuleController


@pytest.mark.parametrize(
    ("text", "minimum", "maximum"),
    [
        ("temperatura mayor a 300", 300, None),
        ("temperatura mayor de 300", 300, None),
        ("temperatura superior a 300", 300, None),
        ("más de 300 de temperatura", 300, None),
        ("temperatura > 300", 300, None),
        ("temperatura menor a 100", None, 100),
        ("temperatura menor de 100", None, 100),
        ("temperatura inferior a 100", None, 100),
        ("menos de 100 de temperatura", None, 100),
        ("temperatura < 100", None, 100),
    ],
)
def test_temperature_language_is_parsed_without_reclassifying_numbers(
    text, minimum, maximum
):
    result = extract_temperature_mentions(text)
    assert (result.minimum, result.maximum) == (minimum, maximum)


@pytest.mark.parametrize(
    "text",
    [
        "ASICS por menos de 100 €",
        "leche por debajo de 0,80 €/L",
        "24 cervezas",
        "6x330ml",
    ],
)
def test_numbers_without_temperature_evidence_are_not_temperature(text):
    assert extract_temperature_mentions(text).empty


def test_global_temperature_alert_needs_no_product_or_brand():
    intent = validate_intent(
        merge_intent(
            AlertIntent(action="create"),
            "Quiero alertas si la temperatura es mayor a 300",
        )
    )
    rule = intent_to_rule(intent)
    assert rule.query is None
    assert rule.product is None
    assert rule.brand is None
    assert rule.constraints.temperature_min == 300


class _Translator:
    def interpret_alert(self, _text):
        return AlertIntent(action="create")


class _Controller(TelegramRuleController):
    def send_message(self, _text):
        pass


def test_telegram_creates_and_explains_a_global_temperature_alert(tmp_path):
    repository = DealRepository(tmp_path / "alerts.db")
    controller = _Controller(
        bot_token="token",
        authorized_chat_id="7",
        repository=repository,
        translator=_Translator(),
    )
    reply = controller.process_update(
        {
            "update_id": 1,
            "message": {
                "chat": {"id": "7"},
                "text": "Quiero alertas si la temperatura es mayor a 300",
            },
        }
    )
    assert "cualquier chollo" in reply
    assert "al menos 300°" in reply
    stored = repository.rule_from_listing(repository.list_alert_rules()[0])
    assert stored.query is None
    assert stored.constraints.temperature_min == 300


def test_global_temperature_thresholds_do_not_deduplicate_each_other(tmp_path):
    repository = DealRepository(tmp_path / "alerts.db")
    for threshold in (300, 500):
        intent = validate_intent(
            merge_intent(
                AlertIntent(action="create"), f"temperatura mayor a {threshold}"
            )
        )
        repository.apply_alert_intent(intent)
        row = repository.list_alert_rules()[-1]
        repository.attach_alert_rule(
            row[0], intent_to_rule(intent), f"temperatura mayor a {threshold}"
        )
    assert len(repository.list_alert_rules()) == 2


def test_global_temperature_thresholds_can_coexist_for_one_user(tmp_path):
    repository = DealRepository(tmp_path / "alerts.db")
    for threshold in (300, 500):
        intent = validate_intent(
            merge_intent(
                AlertIntent(action="create"), f"temperatura mayor a {threshold}"
            )
        )
        repository.apply_alert_intent(intent, user_id=11)
        row = repository.list_alert_rules(user_id=11)[-1]
        repository.attach_alert_rule(
            row[0],
            intent_to_rule(intent),
            f"temperatura mayor a {threshold}",
            user_id=11,
        )
    assert len(repository.list_alert_rules(user_id=11)) == 2


def test_temperature_capability_question_is_not_alert_listing():
    assert (
        classify_alert_operation("¿Tienes alertas por temperatura?")
        == "CAPABILITY_QUESTION"
    )
    assert classify_alert_operation("qué alertas tengo") == "LIST_ALERTS"


def test_temperature_persists_across_repository_reopen(tmp_path):
    path = tmp_path / "alerts.db"
    repository = DealRepository(path)
    rule_id = repository.save_alert_rule(
        AlertRule(query=None, constraints=AlertConstraints(temperature_min=300)),
        "temperatura mayor a 300",
        user_id=11,
    )
    repository.close()
    reopened = DealRepository(path)
    loaded = reopened.load_alert_rule(rule_id, user_id=11)
    assert loaded is not None
    assert loaded.constraints.temperature_min == 300
    assert reopened.get_rule(rule_id, user_id=11)[6] == 1


def test_temperature_matching_min_max_and_missing_value_are_conservative():
    def deal(value):
        return Deal(
            "1",
            "Oferta",
            "https://example.test",
            Decimal(1),
            "Amazon",
            value,
            "generic",
            None,
        )

    assert apply_rule(deal(350), InterestRule("global", temperature_min=300)).accepted
    assert not apply_rule(
        deal(250), InterestRule("global", temperature_min=300)
    ).accepted
    assert apply_rule(deal(50), InterestRule("global", temperature_max=100)).accepted
    assert not apply_rule(
        deal(150), InterestRule("global", temperature_max=100)
    ).accepted
    assert not apply_rule(
        deal(None), InterestRule("global", temperature_min=300)
    ).accepted


def test_temperature_alerts_remain_owned_by_their_user(tmp_path):
    repository = DealRepository(tmp_path / "alerts.db")
    repository.save_alert_rule(
        AlertRule(query=None, constraints=AlertConstraints(temperature_min=300)),
        "temperatura mayor a 300",
        user_id=1,
    )
    assert len(repository.list_alert_rules(user_id=1)) == 1
    assert repository.list_alert_rules(user_id=2) == []
