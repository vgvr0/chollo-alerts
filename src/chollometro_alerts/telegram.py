import requests

from .models import Deal


class TelegramNotifier:
    def __init__(self, token, chat_id, timeout=20):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, deal: Deal):
        r = requests.post(
            self.url,
            json={
                "chat_id": self.chat_id,
                "text": format_message(deal),
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


def format_message(deal: Deal) -> str:
    return f"[{deal.category}] {deal.title}\nPrecio: {deal.price if deal.price is not None else 'N/D'}\nTienda: {deal.merchant or 'N/D'}\nTemperatura: {deal.temperature if deal.temperature is not None else 'N/D'}°\n{deal.url}"


class DryRunNotifier:
    dry_run = True

    def send(self, deal: Deal):
        print(format_message(deal))
