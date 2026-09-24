import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .evaluation import MatchEvidence
from .models import Deal, format_amount
from .schedule import as_aware_utc, default_timezone

TRANSIENT_TELEGRAM_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _retry_after(response):
    try:
        value = float(response.headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def post_with_retry(
    url, *, json, timeout, retries=2, backoff=0.5, max_backoff=30, sleep=time.sleep
):
    """POST to Telegram with bounded retries for transient failures only."""
    for attempt in range(retries + 1):
        try:
            response = requests.post(url, json=json, timeout=timeout)
            status = getattr(response, "status_code", None)
            if status not in TRANSIENT_TELEGRAM_STATUS:
                response.raise_for_status()
                return response
            if attempt == retries:
                response.raise_for_status()
                return response
            delay = _retry_after(response)
            sleep(
                min(delay if delay is not None else backoff * (2**attempt), max_backoff)
            )
        except (requests.Timeout, requests.ConnectionError):
            if attempt == retries:
                raise
            sleep(min(backoff * (2**attempt), max_backoff))
    raise AssertionError("telegram retry loop must return or raise")


class TelegramNotifier:
    def __init__(
        self,
        token,
        chat_id,
        timeout=20,
        retries=2,
        backoff=0.5,
        sleep=time.sleep,
        repository=None,
    ):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self.repository = repository

    def send(self, deal: Deal, evidence: MatchEvidence | None = None, rule_id=None):
        chat_id = (
            self.repository.notification_chat_id(rule_id)
            if self.repository and rule_id is not None
            else None
        )
        chat_id = chat_id or self.chat_id
        post_with_retry(
            self.url,
            json={
                "chat_id": chat_id,
                "text": format_message(deal, evidence),
                "disable_web_page_preview": False,
            },
            timeout=self.timeout,
            retries=self.retries,
            backoff=self.backoff,
            sleep=self._sleep,
        )

    def send_system_alert(self, error_type, component, message, run_id):
        from datetime import UTC, datetime

        text = f"🚨 CHOLLOMETRO ALERTS ERROR\n\nTipo: {error_type}\nComponente: {component}\nMensaje: {message}\nHora: {datetime.now(UTC).isoformat()}\nRun: {run_id}"
        post_with_retry(
            self.url, json={"chat_id": self.chat_id, "text": text}, timeout=self.timeout
        )


def _temperature(value) -> str:
    return f"{value}°" if value is not None else "N/D"


def _published_label(published_at: datetime | None) -> str | None:
    """Render the provider publication instant in the app's local timezone."""
    if published_at is None:
        return None
    try:
        local = as_aware_utc(published_at).astimezone(ZoneInfo(default_timezone()))
    except (TypeError, ValueError):
        return None
    return f"🕒 Publicado: {local:%H:%M}"


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
    published_label = _published_label(deal.published_at)
    if published_label is not None:
        lines.append(published_label)
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
        if evidence.momentum is not None:
            momentum = evidence.momentum
            velocity = momentum.get("velocity")
            age = momentum.get("age_minutes")
            lines += [
                "",
                "📈 Momentum:",
                (
                    f"• Crecimiento {momentum.get('window_minutes')} min: {velocity:+.2f} °/min"
                    if velocity is not None
                    else "• Crecimiento: N/D"
                ),
                (
                    f"• Edad del chollo: {age:.0f} min"
                    if age is not None
                    else "• Edad del chollo: N/D"
                ),
                f"• Motivo: temperatura creciendo por encima de {momentum.get('minimum_velocity')} °/min",
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
