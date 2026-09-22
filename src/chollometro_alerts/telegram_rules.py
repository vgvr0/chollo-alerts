import requests

from .intent import validate_intent


class TelegramRuleController:
    def __init__(
        self, *, bot_token, authorized_chat_id, repository, translator, timeout=20
    ):
        self.url = f"https://api.telegram.org/bot{bot_token}"
        self.authorized_chat_id = str(authorized_chat_id)
        self.repository = repository
        self.translator = translator
        self.timeout = timeout

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
            reply = self._format(intent, rows)
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

    def send_message(self, text):
        response = requests.post(
            f"{self.url}/sendMessage",
            json={"chat_id": self.authorized_chat_id, "text": text},
            timeout=self.timeout,
        )
        response.raise_for_status()

    @staticmethod
    def _format(intent, rows):
        if intent.action == "list":
            return (
                "No tienes alertas configuradas."
                if not rows
                else "\n".join(
                    f"#{r[0]} {r[1]} < {r[4]} €/{r[5]} ({'activa' if r[6] else 'inactiva'})"
                    for r in rows
                )
            )
        return f"Regla {intent.action} aplicada correctamente."
