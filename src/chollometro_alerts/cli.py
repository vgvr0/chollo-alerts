import argparse
import os

from .client import ChollometroClient
from .config import load_rules
from .repository import DealRepository
from .service import AlertService
from .telegram import DryRunNotifier, TelegramNotifier


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="deals.sqlite3")
    p.add_argument("--pages", type=int, default=1)
    sub = p.add_subparsers(dest="command", required=False)
    baseline = sub.add_parser("baseline")
    baseline.add_argument("--dry-run", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    service = AlertService(ChollometroClient(), DealRepository(a.db), None)
    if a.command == "baseline":
        print(service.baseline(["leche", "cerveza"], a.pages, a.dry_run))
        return
    notifier = (
        DryRunNotifier()
        if getattr(a, "dry_run", False)
        else TelegramNotifier(
            os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        )
    )
    service.notifier = notifier
    try:
        print(service.run(["leche", "cerveza"], a.pages, load_rules()))
    except Exception as exc:
        service.notify_error(type(exc).__name__, "AlertService", str(exc))
        raise
