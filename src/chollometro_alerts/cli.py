import argparse
import logging
import os
import threading
from time import perf_counter

import requests
from dotenv import load_dotenv

from .client import ChollometroClient
from .config import PROJECT_ROOT, ConfigurationError, TelegramSettings, load_rules
from .llm.alert_parser import DeepSeekAlertRuleParser
from .llm.deepseek import DeepSeekProductExtractor
from .repository import DealRepository
from .runtime import positive_interval, run_daemon
from .service import AlertService
from .telegram import DryRunNotifier, TelegramNotifier
from .telegram_rules import TelegramRuleController

PRICE_REJECTIONS = {
    "REJECTED_PRICE",
    "REJECTED_PRICE_PER_LITER",
    "REJECTED_PRICE_PER_KILOGRAM",
    "REJECTED_PRICE_PER_UNIT",
}


def _price_unit_label(rule):
    """Report the price dimension actually evaluated for a dry-run rule."""
    if rule.max_price_per_unit is not None:
        return "unit"
    if rule.max_price_per_liter is not None:
        return "liter"
    if rule.max_price_per_kilogram is not None:
        return "kilogram"
    return "absolute"


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
    sub.add_parser("telegram-listen", help="Escuchar órdenes de Telegram continuamente")
    run_parser = sub.add_parser("run", help="Ejecutar listener y scanner continuamente")
    run_parser.add_argument("--interval-minutes", type=int, default=None)
    rules_parser = sub.add_parser("run-rules", help="Evaluar reglas activas")
    rules_parser.add_argument("--dry-run", action="store_true", required=True)
    alert_parser = sub.add_parser("alert", help="Gestionar alertas en lenguaje natural")
    alert_sub = alert_parser.add_subparsers(dest="alert_command", required=True)
    for name in ("parse", "add"):
        command = alert_sub.add_parser(name)
        command.add_argument("text")
    alert_sub.add_parser("list")
    a = p.parse_args()
    if a.command == "alert":
        repository = DealRepository(a.db)
        if a.alert_command == "list":
            for row in repository.structured_alert_rules():
                print(
                    f"#{row[0]} — {row[1]} — {row[2] or 'N/D'} — {row[3] or 'N/D'} — {'activa' if row[5] else 'inactiva'}"
                )
            return
        parser = DeepSeekAlertRuleParser(DeepSeekProductExtractor())
        try:
            rule = parser.parse(a.text)
        except (ValueError, ConfigurationError, requests.RequestException) as exc:
            p.error(f"No se pudo interpretar la alerta: {type(exc).__name__}")
        print(rule.model_dump_json(indent=2))
        if a.alert_command == "add":
            print(f"RULE_ID={repository.save_alert_rule(rule, a.text)}")
        return
    if a.command == "test-llm":
        test_llm(a.text, p)
        return
    if a.command == "run-rules":
        service = AlertService(ChollometroClient(), DealRepository(a.db), None)
        summary = {}
        for rule_id, query, deal, rule, result in service.dry_run_active_rules(a.pages):
            price_unit = _price_unit_label(rule)
            extraction = deal.product_extraction
            product_match = (
                not rule.product_type
                or (getattr(extraction, "product_type", "") or "").casefold()
                == rule.product_type.casefold()
            )
            brand_match = (
                not rule.brand
                or (getattr(extraction, "brand", "") or "").casefold()
                == rule.brand.casefold()
            )
            price_match = result.reason not in PRICE_REJECTIONS
            print(
                f'rule={rule_id} deal={deal.deal_id} title="{deal.title}" brand={getattr(extraction, "brand", None) or "N/D"} price={deal.price if deal.price is not None else "N/D"} price_unit={price_unit} price_per_unit={deal.price_per_unit if deal.price_per_unit is not None else "N/D"} product_match={str(product_match).lower()} brand_match={str(brand_match).lower()} price_match={str(price_match).lower()} matched={str(result.accepted).lower()} result={"WOULD_NOTIFY" if result.accepted else result.reason}'
            )
            bucket = summary.setdefault(
                rule_id,
                {
                    "QUERY": query,
                    "CANDIDATES": 0,
                    "MATCHED": 0,
                    "WOULD_NOTIFY": 0,
                    "REJECTED_PRODUCT": 0,
                    "REJECTED_BRAND": 0,
                    "REJECTED_PRICE": 0,
                },
            )
            bucket["CANDIDATES"] += 1
            bucket["MATCHED"] += int(result.accepted)
            bucket["WOULD_NOTIFY"] += int(result.accepted)
            if result.reason == "REJECTED_PRODUCT":
                bucket["REJECTED_PRODUCT"] += 1
            if result.reason == "REJECTED_BRAND":
                bucket["REJECTED_BRAND"] += 1
            if result.reason in PRICE_REJECTIONS:
                bucket["REJECTED_PRICE"] += 1
        print("SUMMARY")
        for rule_id, values in summary.items():
            print(f"RULE_ID={rule_id}")
            for key, value in values.items():
                print(f"{key}={value}")
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
    if a.command in {"telegram-listen", "run"}:
        try:
            telegram = TelegramSettings.from_env()
        except ConfigurationError as exc:
            p.error(f"Error de configuración: {exc}")
        repository = DealRepository(a.db)
        service = None
        if a.command == "run":
            service = AlertService(
                ChollometroClient(),
                repository,
                TelegramNotifier(telegram.bot_token, telegram.authorized_chat_id),
            )
        controller = TelegramRuleController(
            bot_token=telegram.bot_token,
            authorized_chat_id=telegram.authorized_chat_id,
            repository=repository,
            translator=DeepSeekProductExtractor(),
            service=service,
        )
        stop = threading.Event()
        try:
            if a.command == "telegram-listen":
                controller.listen_forever(stop_event=stop)
            else:
                interval = positive_interval(
                    a.interval_minutes or os.getenv("SCAN_INTERVAL_MINUTES", "10")
                )
                run_daemon(controller, service, interval, a.pages, stop)
        except KeyboardInterrupt:
            stop.set()
            logging.getLogger(__name__).info("shutdown_requested")
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
        # Legacy single-query CLI check; persisted Telegram rules are scanned by
        # run-rules/run and do not share this compatibility command.
        service.run(["leche"], a.pages, load_rules())
    except Exception as exc:
        service.notify_error(type(exc).__name__, "AlertService", str(exc))
        raise
    finally:
        if hasattr(service, "last_summary"):
            print(service.last_summary.format_metrics())
