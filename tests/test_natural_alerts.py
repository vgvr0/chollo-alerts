from decimal import Decimal
from unittest.mock import Mock

import pytest

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.llm.alert_parser import DeepSeekAlertRuleParser
from chollometro_alerts.repository import DealRepository


def make_rule():
    return AlertRule(
        query="cerveza Mahou",
        product="cerveza",
        brand="Mahou",
        constraints=AlertConstraints(
            max_price_per_liter=Decimal("1.20"), min_temperature=100
        ),
    )


def test_alert_rule_round_trip_persistence(tmp_path):
    repository = DealRepository(tmp_path / "alerts.db")
    original = make_rule()
    rule_id = repository.save_alert_rule(original, "texto original")
    assert repository.load_alert_rule(rule_id) == original
    assert repository.alert_rule_metadata(rule_id)[1:] == ("texto original", 1, 1)


def test_parser_validates_provider_output_and_rejects_unknown_fields():
    provider = Mock()
    provider.interpret_alert_rule.return_value = make_rule()
    parser = DeepSeekAlertRuleParser(provider)
    assert parser.parse("Mahou") == make_rule()
    provider.interpret_alert_rule.assert_called_once_with("Mahou")

    with pytest.raises(ValueError):
        AlertRule.model_validate({"query": "Mahou", "unknown": True})
