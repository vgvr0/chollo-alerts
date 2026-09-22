import argparse
import logging
import os
from time import perf_counter

from dotenv import load_dotenv

from .client import ChollometroClient
from .config import PROJECT_ROOT, ConfigurationError, TelegramSettings, load_rules
from .llm.deepseek import DeepSeekProductExtractor
from .repository import DealRepository
from .service import AlertService
from .telegram import DryRunNotifier, TelegramNotifier
from .telegram_rules import TelegramRuleController


def test_llm(text, parser):
    """Explicit connectivity probe: one HTTP attempt, independent of LLM_ENABLED."""
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        parser.error("Error de configuración: falta DEEPSEEK_API_KEY para test-llm.")
    try:
        extractor = DeepSeekProductExtractor(retries=0)
    except ConfigurationError as exc:
        parser.error(f"Error de configuración: {exc}")
    started = perf_counter()
    extraction = extractor(text, deal_id="test-llm")
    elapsed = perf_counter() - started
    print(extraction.model_dump_json(indent=2))
    print(f"STATUS={'FAILURE' if extractor.last_error else 'SUCCESS'}")
    for name, value in extractor.metrics.items():
        print(f"{name}={value}")
    print(f"LATENCY_SECONDS={elapsed:.3f}")
    if extractor.last_error:
        print(f"ERROR={extractor.last_error} (extracción determinista de fallback)")
        raise SystemExit(1)


def main():
    load_dotenv(PROJECT_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="deals.sqlite3")
    p.add_argument("--pages", type=int, default=1)
    sub = p.add_subparsers(dest="command", required=False)
    baseline = sub.add_parser("baseline")
    baseline.add_argument("--dry-run", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("--dry-run", action="store_true")
    probe = sub.add_parser("test-llm", help="Probar DeepSeek con una sola petición")
    probe.add_argument("text", help="Texto del producto que se extraerá")
    sub.add_parser("telegram-poll", help="Procesar una tanda de órdenes de Telegram")
    a = p.parse_args()
    if a.command == "test-llm":
        test_llm(a.text, p)
        return
    if a.command == "telegram-poll":
        extractor = DeepSeekProductExtractor()
        try:
            telegram = TelegramSettings.from_env()
        except ConfigurationError as exc:
            p.error(f"Error de configuración: {exc}")
        controller = TelegramRuleController(
            bot_token=telegram.bot_token,
            authorized_chat_id=telegram.authorized_chat_id,
            repository=DealRepository(a.db),
            translator=extractor,
        )
        controller.poll_once()
        return
    if a.command != "baseline" and not getattr(a, "dry_run", False):
        missing = [
            name
            for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
            if not os.getenv(name, "").strip()
        ]
        if missing:
            p.error(
                "Error de configuración: faltan "
                + ", ".join(missing)
                + ". Configúralas en .env en la raíz del proyecto o usa check --dry-run."
            )
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
        service.run(["leche", "cerveza"], a.pages, load_rules())
    except Exception as exc:
        service.notify_error(type(exc).__name__, "AlertService", str(exc))
        raise
    finally:
        if hasattr(service, "last_summary"):
            print(service.last_summary.format_metrics())
