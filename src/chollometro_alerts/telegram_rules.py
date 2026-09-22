import logging
import time

import requests

from .intent import intent_to_rule, validate_intent

logger = logging.getLogger(__name__)

# How the persisted price dimension is rendered back to the user. An absolute
# price is the total price of the deal, so it has no "/unit" suffix at all.
PRICE_UNIT_LABELS = {"liter": "L", "unit": "ud", "kilogram": "kg"}


def price_suffix(price_unit):
    label = PRICE_UNIT_LABELS.get(price_unit or "absolute")
    return f"€/{label}" if label else "€"


class TelegramRuleController:
    def __init__(
        self,
        *,
        bot_token,
        authorized_chat_id,
        repository,
        translator,
        timeout=20,
        service=None,
    ):
        self.url = f"https://api.telegram.org/bot{bot_token}"
        self.authorized_chat_id = str(authorized_chat_id)
        self.repository = repository
        self.translator = translator
        self.timeout = timeout
        self.service = service

    def process_update(self, update):
        update_id = update.get("update_id")
        message = update.get("message") or {}
        if (
            update_id is None
            or str(message.get("chat", {}).get("id")) != self.authorized_chat_id
        ):
            return None
        if not self.repository.claim_telegram_update(update_id):
            return None
        text = message.get("text", "").strip()
        try:
            intent = validate_intent(self.translator.interpret_alert(text))
            rows = self.repository.apply_alert_intent(intent)
            if intent.action in {"create", "update"}:
                target = next(
                    (
                        row
                        for row in rows
                        if row[1]
                        == (intent.query or intent.product_type or intent.brand)
                    ),
                    None,
                )
                if target:
                    self.repository.attach_alert_rule(
                        target[0], intent_to_rule(intent), text
                    )
            baseline_count = None
            if intent.action == "create" and self.service is not None:
                query = intent.query or intent.product_type or intent.brand
                rule = next((r for r in rows if r[1] == query), None)
                if (
                    rule is not None
                    and self.repository.get_rule(rule[0])[7] == "INITIALIZING"
                ):
                    baseline_count = self.service.baseline_rule(rule[0], query)
                    rows = self.repository.list_alert_rules()
            reply = self._format(intent, rows, baseline_count)
        except ValueError as exc:
            reply = f"Necesito una aclaración: {exc}"
        self.send_message(reply)
        return reply

    def poll_once(self, offset=None):
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        response = requests.get(
            f"{self.url}/getUpdates", params=params, timeout=self.timeout
        )
        response.raise_for_status()
        for update in response.json().get("result", []):
            self.process_update(update)

    def listen_forever(self, stop_event=None, poll_timeout=45, max_backoff=60):
        """Consume Telegram updates until stopped, tolerating transient failures."""
        offset = None
        backoff = 1
        while stop_event is None or not stop_event.is_set():
            try:
                params = {"timeout": poll_timeout}
                if offset is not None:
                    params["offset"] = offset
                response = requests.get(
                    f"{self.url}/getUpdates", params=params, timeout=poll_timeout + 10
                )
                response.raise_for_status()
                updates = response.json().get("result", [])
                for update in updates:
                    update_id = update.get("update_id")
                    if update_id is not None:
                        offset = max(offset or update_id, update_id + 1)
                    logger.info("telegram_update_received update_id=%s", update_id)
                    try:
                        self.process_update(update)
                    except Exception:
                        logger.exception(
                            "telegram_update_failed update_id=%s", update_id
                        )
                backoff = 1
            except (requests.RequestException, ValueError):
                logger.warning("telegram_poll_error retry_in_seconds=%s", backoff)
                if stop_event is not None:
                    stop_event.wait(backoff)
                else:
                    time.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)

    def send_message(self, text):
        response = requests.post(
            f"{self.url}/sendMessage",
            json={"chat_id": self.authorized_chat_id, "text": text},
            timeout=self.timeout,
        )
        response.raise_for_status()

    @staticmethod
    def _format(intent, rows, baseline_count=None):
        def price(row):
            return f"{float(row[4]):.2f}".replace(".", ",")

        def unit(row):
            return price_suffix(row[5])

        if intent.action == "list":
            if not rows:
                return "🔔 Tus alertas:\n\nNo tienes alertas configuradas."
            return "🔔 Tus alertas:\n\n" + "\n".join(
                f"#{r[0]} — {r[1]} — < {price(r)} {unit(r)} — "
                f"{'activa' if r[6] else 'inactiva'}"
                for r in rows
            )
        verb = {
            "create": "Alerta creada",
            "update": "Alerta actualizada",
            "delete": "Alerta eliminada",
            "enable": "Alerta activada",
            "disable": "Alerta desactivada",
        }[intent.action]
        subject = intent.query or intent.product_type or intent.brand or "alerta"
        if intent.action in {"create", "update"}:
            amount = f"{intent.max_price:.2f}".replace(".", ",")
            reply = (
                f"✅ {verb}: {subject} por debajo de "
                f"{amount} {price_suffix(intent.price_unit)}"
            )
            if baseline_count is not None and intent.action == "create":
                reply += f"\n🔎 {baseline_count} ofertas actuales guardadas como referencia.\nTe avisaré de las nuevas que cumplan la condición."
            return reply
        icon = {"delete": "🗑️", "disable": "⏸️", "enable": "▶️"}[intent.action]
        return f"{icon} {verb}: {subject}"
