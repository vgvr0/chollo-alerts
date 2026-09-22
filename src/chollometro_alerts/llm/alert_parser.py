"""Natural-language alert parsing boundary."""

from typing import Protocol

from ..alert_rule import AlertRule


class AlertRuleParser(Protocol):
    def parse(self, text: str) -> AlertRule: ...


class DeepSeekAlertRuleParser:
    """Adapter around the existing DeepSeek client; domain code sees no provider."""

    def __init__(self, client):
        self.client = client

    def parse(self, text: str) -> AlertRule:
        if not text or not text.strip():
            raise ValueError("La alerta no puede estar vacía")
        return self.client.interpret_alert_rule(text)
