import requests

from .evaluation import MatchEvidence
from .models import Deal, format_amount


class TelegramNotifier:
    def __init__(self, token, chat_id, timeout=20):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, deal: Deal, evidence: MatchEvidence | None = None):
        r = requests.post(
            self.url,
            json={
                "chat_id": self.chat_id,
                "text": format_message(deal, evidence),
                "disable_web_page_preview": False,
            },
            timeout=self.timeout,
        )
        r.raise_for_status()

    def send_system_alert(self, error_type, component, message, run_id):
        from datetime import UTC, datetime

        text = f"🚨 CHOLLOMETRO ALERTS ERROR\n\nTipo: {error_type}\nComponente: {component}\nMensaje: {message}\nHora: {datetime.now(UTC).isoformat()}\nRun: {run_id}"
        r = requests.post(
            self.url, json={"chat_id": self.chat_id, "text": text}, timeout=self.timeout
        )
        r.raise_for_status()


def _temperature(value) -> str:
    return f"{value}°" if value is not None else "N/D"


def format_message(deal: Deal, evidence: MatchEvidence | None = None) -> str:
    """The Telegram message: the deal, the alert that matched and why.

    Without evidence the message still names the deal; the alert and the
    conditions come from `MatchEvidence`, which the evaluation built from the
    values it really compared. `deal.category` is never used to explain a match.
    """
    lines = [
        "🔔 Chollo encontrado",
        "",
        deal.title,
        "",
        f"💰 Precio: {format_amount(deal.price)}",
        f"🏪 Tienda: {deal.merchant or 'N/D'}",
        f"🔥 Temperatura: {_temperature(deal.temperature)}",
    ]
    if evidence is not None:
        alert = (evidence.alert_text or evidence.query).strip()
        if alert:
            lines += ["", "🎯 Alerta:", f'"{alert}"']
        if evidence.checks:
            lines += ["", "✅ Cumple:"]
            lines += [f"• {check.label}: {check.detail}" for check in evidence.checks]
        if evidence.semantic_reason:
            lines += [
                "",
                "🤖 Coincidencia semántica:",
                f'"{evidence.semantic_reason}"',
            ]
        lines += ["", f"🧠 Evaluación: {evidence.method}"]
    lines += ["", deal.url]
    return "\n".join(lines)


class DryRunNotifier:
    dry_run = True

    def send(self, deal: Deal, evidence: MatchEvidence | None = None):
        print(format_message(deal, evidence))
        extraction = deal.product_extraction
        values = {
            "EXTRACTION_SOURCE": getattr(extraction, "extraction_source", None),
            "CONFIDENCE": getattr(extraction, "confidence", None),
            "TOTAL_VOLUME_L": deal.total_volume_l,
            "PRICE_PER_LITER": deal.price_per_liter,
        }
        for name, value in values.items():
            print(f"{name}={value if value is not None else 'N/D'}")
