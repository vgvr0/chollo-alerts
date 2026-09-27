import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .evaluation import MatchEvidence
from .i18n import DEFAULT_LANGUAGE, Translator
from .models import Deal, format_amount
from .schedule import as_aware_utc, default_timezone

TRANSIENT_TELEGRAM_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_I18N = Translator()


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
                "text": format_message(
                    deal, evidence, self._language_for_rule(rule_id)
                ),
                "disable_web_page_preview": False,
            },
            timeout=self.timeout,
            retries=self.retries,
            backoff=self.backoff,
            sleep=self._sleep,
        )

    def _language_for_rule(self, rule_id):
        if self.repository is None or rule_id is None:
            return DEFAULT_LANGUAGE
        resolver = getattr(self.repository, "language_for_rule", None)
        return resolver(rule_id) if resolver is not None else DEFAULT_LANGUAGE

    def send_system_alert(self, error_type, component, message, run_id):
        from datetime import UTC, datetime

        text = f"🚨 CHOLLO ALERTS ERROR\n\nTipo: {error_type}\nComponente: {component}\nMensaje: {message}\nHora: {datetime.now(UTC).isoformat()}\nRun: {run_id}"
        post_with_retry(
            self.url, json={"chat_id": self.chat_id, "text": text}, timeout=self.timeout
        )


def _temperature(value, language=DEFAULT_LANGUAGE) -> str:
    return (
        f"{value}°"
        if value is not None
        else _I18N.t("deals.not_available", locale=language)
    )


def _published_label(
    published_at: datetime | None, language=DEFAULT_LANGUAGE
) -> str | None:
    """Render the provider publication instant in the app's local timezone."""
    if published_at is None:
        return None
    try:
        local = as_aware_utc(published_at).astimezone(ZoneInfo(default_timezone()))
    except (TypeError, ValueError):
        return None
    return _I18N.t("deals.published", locale=language, value=f"{local:%H:%M}")


def format_message(
    deal: Deal, evidence: MatchEvidence | None = None, language=DEFAULT_LANGUAGE
) -> str:
    """The Telegram message: the deal, the alert that matched and why.

    Without evidence the message still names the deal; the alert and the
    conditions come from `MatchEvidence`, which the evaluation built from the
    values it really compared. `deal.category` is never used to explain a match.
    """
    lines = [
        _I18N.t("deals.found", locale=language),
        "",
        deal.title,
        "",
        _I18N.t(
            "deals.price",
            locale=language,
            value=format_amount(deal.price, deal.currency),
        ),
        _I18N.t("deals.store", locale=language, value=deal.merchant or "N/D"),
        _I18N.t(
            "deals.temperature",
            locale=language,
            value=_temperature(deal.temperature, language),
        ),
    ]
    published_label = _published_label(deal.published_at, language)
    if published_label is not None:
        lines.append(published_label)
    if evidence is not None:
        alert = (evidence.alert_text or evidence.query).strip()
        if alert:
            lines += ["", _I18N.t("deals.alert", locale=language), f'"{alert}"']
        if evidence.checks:
            lines += ["", _I18N.t("deals.matches", locale=language)]
            lines += [f"• {check.label}: {check.detail}" for check in evidence.checks]
        if evidence.semantic_reason:
            lines += [
                "",
                _I18N.t("deals.semantic", locale=language),
                f'"{evidence.semantic_reason}"',
            ]
        if evidence.momentum is not None:
            momentum = evidence.momentum
            velocity = momentum.get("velocity")
            age = momentum.get("age_minutes")
            lines += [
                "",
                _I18N.t("deals.momentum", locale=language),
                (
                    _I18N.t(
                        "deals.growth",
                        locale=language,
                        minutes=momentum.get("window_minutes"),
                        velocity=velocity,
                    )
                    if velocity is not None
                    else _I18N.t("deals.growth_unknown", locale=language)
                ),
                (
                    _I18N.t("deals.age", locale=language, value=f"{age:.0f}")
                    if age is not None
                    else _I18N.t("deals.age_unknown", locale=language)
                ),
                _I18N.t(
                    "deals.reason",
                    locale=language,
                    value=momentum.get("minimum_velocity"),
                ),
            ]
        lines += [
            "",
            _I18N.t("deals.evaluation", locale=language, value=evidence.method),
        ]
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
