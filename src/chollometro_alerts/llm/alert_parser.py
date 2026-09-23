"""Natural-language alert parsing boundary."""

from typing import Protocol

from ..alert_rule import AlertRule
from ..alert_text import merge_rule


class AlertRuleParser(Protocol):
    def parse(self, text: str) -> AlertRule: ...


class DeepSeekAlertRuleParser:
    """Adapter around the existing DeepSeek client; domain code sees no provider."""

    def __init__(self, client):
        self.client = client

    def parse(self, text: str) -> AlertRule:
        if not text or not text.strip():
            raise ValueError("La alerta no puede estar vacía")
        # The shops and the notification window of the sentence are read
        # deterministically and merged into the provider's answer.
        return merge_rule(self.client.interpret_alert_rule(text), text)
