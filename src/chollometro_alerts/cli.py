import argparse
import logging
import os
import signal
import threading
from time import perf_counter

import requests
from dotenv import load_dotenv

from .client import ChollometroClient
from .config import (
    PROJECT_ROOT,
    ConfigurationError,
    GraphQLFeedSettings,
    RetentionSettings,
    TelegramSettings,
    load_rules,
)
from .errors import SCAN_FAILED, ChollometroError
from .graphql_feed import GraphQLFeedClient
from .health import check as health_check
from .legacy_rules import audit_legacy_rules, repair_legacy_rules
from .llm import create_extractor
from .llm.alert_parser import DeepSeekAlertRuleParser
from .llm.deepseek import DeepSeekProductExtractor
from .models import format_number
from .product import product_type_matches
from .replay import (
    DEFAULT_REPLAY_LIMIT,
    ReplayDecision,
    ReplayEngine,
    ReplayResult,
    RuleNotFoundError,
)
from .repository import DealRepository
from .retention import RetentionService
from .runtime import positive_interval, run_daemon, run_scanner
from .service import AlertService
from .telegram import DryRunNotifier, TelegramNotifier
from .telegram_rules import TelegramRuleController, format_alert_list

PRICE_REJECTIONS = {
    "REJECTED_PRICE",
    "REJECTED_PRICE_PER_LITER",
    "REJECTED_PRICE_PER_KILOGRAM",
    "REJECTED_PRICE_PER_UNIT",
}

REPLAY_SAMPLE_SIZE = 10


def report_scan_failure(error: ChollometroError) -> None:
    """Operational summary of a failed provider scan (never secrets)."""
    print(f"SCAN_STATUS={SCAN_FAILED}")
    print(f"ERROR_TYPE={error.error_type}")
    print(f"MESSAGE={error}")
    logging.getLogger(__name__).error(
        "scan_failed provider=chollometro error_type=%s http_status=%s message=%s",
        error.error_type,
        error.status_code,
        error,
    )


def format_alert_listing(rule_id, rule, enabled):
    """Render one stored alert for `alert list`, from the canonical rule."""
    line = (
        f"#{rule_id} — {rule.query} — {rule.product or 'N/D'} — "
        f"{rule.brand or 'N/D'} — {'activa' if enabled else 'inactiva'}"
    )
    details = []
    temperature = rule.constraints.temperature_min, rule.constraints.temperature_max
    if any(value is not None for value in temperature):
        details.append("temperatura: " + _temperature_label(*temperature))
    if rule.include_merchants:
        details.append("tiendas: " + ", ".join(rule.include_merchants))
    if rule.exclude_merchants:
        details.append("excepto: " + ", ".join(rule.exclude_merchants))
    if rule.notification_window is not None:
        details.append(
            "avisos: "
            f"{rule.notification_window.start:%H:%M}"
            f"–{rule.notification_window.end:%H:%M} "
            f"{rule.notification_window.timezone}"
        )
    return line if not details else line + " — " + " — ".join(details)


def _temperature_label(minimum, maximum):
    """How `alert list` reports the temperature window of one rule."""
    if minimum is not None and maximum is not None:
        return f"{format_number(minimum)}°-{format_number(maximum)}°"
    if minimum is not None:
        return f"≥ {format_number(minimum)}°"
    return f"≤ {format_number(maximum)}°"


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


def _price_label(price):
    return f"{price:.2f} €" if price is not None else "N/D"


def build_feed_client():
    """GraphQL discovery feed, or None when the operator disabled it.

    The client is only constructed (no request happens here), so a daemon that
    starts with the feed disabled never touches the GraphQL endpoint and keeps
    the original per-query HTML behaviour.
    """
    settings = GraphQLFeedSettings.from_env()
    return GraphQLFeedClient(feed_settings=settings) if settings.enabled else None


def _result_label(entry):
    if entry.decision is ReplayDecision.MATCH:
        return "MATCH"
    if entry.decision is ReplayDecision.NOT_EVALUABLE:
        return f"NOT_EVALUABLE ({entry.reason})"
    return entry.reason or "REJECT"


def _format_sample(entries, header, sample_size):
    lines = ["", header, ""]
    if not entries:
        lines.append("(none)")
        return lines
    for index, entry in enumerate(entries[:sample_size], start=1):
        lines.append(f"[{index}] #{entry.deal_id} {entry.title}")
        lines.append(f"    Price: {_price_label(entry.price)}")
        lines.append(f"    Result: {_result_label(entry)}")
    if len(entries) > sample_size:
        lines.append(f"    (showing {sample_size} of {len(entries)})")
    return lines


def format_replay(result: ReplayResult, sample_size=REPLAY_SAMPLE_SIZE) -> str:
    """Readable report of a replay run: no Telegram, no state, just evidence."""
    lines = [f"RULE #{result.rule_id}", "", f"Query: {result.query}"]
    if result.product:
        lines.append(f"Product: {result.product}")
    if result.brand:
        lines.append(f"Brand: {result.brand}")
    for name, value in result.constraints.model_dump().items():
        if value is not None:
            lines.append(f"{name}: {value}")
    lines += [
        "",
        f"Historical deals available: {result.deals_available}",
        f"Deals evaluated: {result.deals_evaluated}",
        "",
        f"{'MATCH:':<14}{result.matched}",
        f"{'REJECT:':<14}{result.rejected}",
        f"{'NOT_EVALUABLE:':<14}{result.not_evaluable}",
    ]
    if result.deals_available > result.deals_evaluated:
        pending = result.deals_available - result.deals_evaluated
        lines.append(f"(limit applied: {pending} related deals were not evaluated)")
    if not result.has_historical_deals:
        lines += [
            "",
            f"No historical deals stored for query '{result.query}'.",
            "There is no local data to evaluate, so this replay says nothing about",
            "the rule: 0 matches does NOT mean the alert is wrong.",
            "Re-run it once deals for this query are stored locally.",
        ]
        return "\n".join(lines)
    lines += _format_sample(result.matches(), "MATCHES", sample_size)
    lines += _format_sample(result.rejections(), "SAMPLE REJECTIONS", sample_size)
    if result.not_evaluable:
        lines += _format_sample(
            result.not_evaluable_results(), "SAMPLE NOT_EVALUABLE", sample_size
        )
    lines += [
        "",
        "Offline replay of local deals: no scraping, no LLM, no Telegram, no writes.",
    ]
    return "\n".join(lines)


def main():
    load_dotenv(PROJECT_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument(
        "--db",
        default=os.getenv("DATABASE_PATH", "deals.sqlite3"),
        help="Ruta de SQLite (por defecto DATABASE_PATH o deals.sqlite3)",
    )
    p.add_argument("--pages", type=int, default=1)
    sub = p.add_subparsers(dest="command", required=False)
    baseline = sub.add_parser("baseline")
    baseline.add_argument("--dry-run", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("--dry-run", action="store_true")
    sub.add_parser("health", help="Evaluar la salud local del daemon")
    sub.add_parser("users", help="Listar usuarios Telegram y sus alertas")
    probe = sub.add_parser("test-llm", help="Probar DeepSeek con una sola petición")
    probe.add_argument("text", help="Texto del producto que se extraerá")
    sub.add_parser("telegram-poll", help="Procesar una tanda de órdenes de Telegram")
    sub.add_parser("telegram-listen", help="Escuchar órdenes de Telegram continuamente")
    maintenance = sub.add_parser("maintenance", help="Mantenimiento seguro de SQLite")
    maintenance_sub = maintenance.add_subparsers(
        dest="maintenance_command", required=True
    )
    prune = maintenance_sub.add_parser("prune", help="Eliminar históricos no críticos")
    prune.add_argument("--dry-run", action="store_true")
    maintenance_sub.add_parser("status", help="Mostrar estado y candidatos de limpieza")
    maintenance_sub.add_parser("vacuum", help="Compactar SQLite explícitamente")
    run_parser = sub.add_parser("run", help="Ejecutar listener y scanner continuamente")
    run_parser.add_argument("--interval-minutes", type=int, default=None)
    scan_parser = sub.add_parser("scan", help="Ejecutar solo el scanner continuamente")
    scan_parser.add_argument("--interval-minutes", type=int, default=None)
    rules_parser = sub.add_parser("run-rules", help="Evaluar reglas activas")
    rules_parser.add_argument("--dry-run", action="store_true", required=True)
    alert_parser = sub.add_parser("alert", help="Gestionar alertas en lenguaje natural")
    alert_sub = alert_parser.add_subparsers(dest="alert_command", required=True)
    for name in ("parse", "add"):
        command = alert_sub.add_parser(name)
        command.add_argument("text")
    repair_parser = alert_sub.add_parser(
        "legacy-repair", help="Auditar o reparar identidades de reglas legacy"
    )
    repair_parser.add_argument(
        "--apply", action="store_true", help="Aplicar solo reparaciones SAFE"
    )
    alert_sub.add_parser("list")
    replay_parser = alert_sub.add_parser(
        "test", help="Replay una regla contra los deals históricos ya guardados"
    )
    replay_parser.add_argument("rule_id", type=int, help="Id de la AlertRule")
    replay_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_REPLAY_LIMIT,
        help="Máximo de deals históricos a evaluar (0 = sin límite)",
    )
    a = p.parse_args()
    if a.command == "maintenance":
        repository = DealRepository(a.db)
        try:
            settings = RetentionSettings.from_env()
            retention = RetentionService(repository, settings)
            if a.maintenance_command == "prune":
                result = retention.run(dry_run=a.dry_run)
                print(f"DRY_RUN={str(result.dry_run).lower()}")
                print(f"DELETED_SNAPSHOTS={result.deleted_snapshots}")
                print(f"DELETED_CACHE_ENTRIES={result.deleted_cache_entries}")
                print(f"DELETED_ERROR_HISTORY={result.deleted_error_history}")
                print(f"DELETED_SCAN_RUNS={result.deleted_scan_runs}")
                print(f"BATCHES={result.batches}")
                return
            if a.maintenance_command == "status":
                counts = repository.retention_counts(settings)
                status = repository.runtime_status()
                print(
                    f"DB_BYTES={os.path.getsize(a.db) if os.path.exists(a.db) else 0}"
                )
                print(
                    f"LAST_MAINTENANCE_AT={status.get('last_retention_finished_at') or 'NEVER'}"
                )
                for name, count in counts.items():
                    print(f"ELIGIBLE_{name.upper()}={count}")
                return
            repository.vacuum()
            print("VACUUM=SUCCESS")
            return
        finally:
            repository.close()
    if a.command == "health":
        raise SystemExit(health_check(a.db))
    if a.command == "users":
        repository = DealRepository(a.db)
        try:
            print("id telegram_user_id telegram_chat_id enabled rules_count")
            for row in repository.list_users():
                print(*row)
            return
        finally:
            repository.close()
    if a.command == "alert":
        repository = DealRepository(a.db)
        try:
            if a.alert_command == "list":
                # Same boundary as the Telegram listing: every stored rule, with the
                # canonical `AlertRule` resolved through `rule_from_listing()` (a
                # structured row, or a legacy row reconstructed by `rule_from_row`).
                # Read-only: nothing is written, not even the legacy rows.
                rows = repository.list_alert_rules()
                print(format_alert_list(rows, repository.rule_from_listing))
                return
            if a.alert_command == "test":
                # Read-only simulator: no scraper, no LLM, no Telegram, no writes.
                try:
                    report = ReplayEngine(repository).replay(a.rule_id, limit=a.limit)
                except RuleNotFoundError as exc:
                    p.error(str(exc))
                print(format_replay(report))
                return
            if a.alert_command == "legacy-repair":
                proposals = (
                    repair_legacy_rules(repository, dry_run=False)
                    if a.apply
                    else audit_legacy_rules(repository)
                )
                for proposal in proposals:
                    if proposal.classification == "STRUCTURED":
                        action = "NO_CHANGE"
                    elif proposal.actionable:
                        action = "UPDATED" if a.apply else "WOULD_UPDATE"
                    else:
                        action = "MANUAL_REVIEW"
                    print(f"RULE #{proposal.rule_id}")
                    print(f"query: {proposal.query}")
                    print(f"classification: {proposal.classification}")
                    print(f"proposed brand: {proposal.brand or 'N/D'}")
                    print(f"confidence: {'SAFE' if proposal.actionable else 'MANUAL'}")
                    print(f"action: {action}")
                    print(f"reason: {proposal.reason}")
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
        finally:
            repository.close()
    if a.command == "test-llm":
        test_llm(a.text, p)
        return
    if a.command == "run-rules":
        repository = DealRepository(a.db)
        try:
            service = AlertService(ChollometroClient(), repository, None)
            summary = {}
            for rule_id, query, deal, rule, result in service.dry_run_active_rules(
                a.pages
            ):
                price_unit = _price_unit_label(rule)
                extraction = deal.product_extraction
                product_match = not rule.product_type or product_type_matches(
                    rule.product_type, getattr(extraction, "product_type", None)
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
            # Distinguishes a completed dry run from one Chollometro could not serve.
            print(f"SCAN_STATUS={service.last_scan_status}")
            print("SUMMARY")
            for rule_id, values in summary.items():
                print(f"RULE_ID={rule_id}")
                for key, value in values.items():
                    print(f"{key}={value}")
            return
        finally:
            repository.close()
    if a.command == "telegram-poll":
        extractor = DeepSeekProductExtractor()
        try:
            telegram = TelegramSettings.from_env()
        except ConfigurationError as exc:
            p.error(f"Error de configuración: {exc}")
        repository = DealRepository(a.db)
        try:
            controller = TelegramRuleController(
                bot_token=telegram.bot_token,
                authorized_chat_id=telegram.authorized_chat_id,
                repository=repository,
                translator=extractor,
                multiuser_enabled=telegram.multiuser_enabled,
                auto_register=telegram.auto_register,
            )
            controller.poll_once()
            return
        finally:
            repository.close()
    if a.command in {"telegram-listen", "run", "scan"}:
        try:
            telegram = TelegramSettings.from_env()
        except ConfigurationError as exc:
            p.error(f"Error de configuración: {exc}")
        repository = DealRepository(a.db)
        extractor = create_extractor()
        service = None
        if a.command == "run":
            service = AlertService(
                ChollometroClient(),
                repository,
                (
                    TelegramNotifier(
                        telegram.bot_token,
                        telegram.authorized_chat_id,
                        repository=repository,
                    )
                    if telegram.multiuser_enabled
                    else TelegramNotifier(
                        telegram.bot_token, telegram.authorized_chat_id
                    )
                ),
                feed=build_feed_client(),
            )
        controller = None
        if a.command != "scan":
            controller = TelegramRuleController(
                bot_token=telegram.bot_token,
                authorized_chat_id=telegram.authorized_chat_id,
                repository=repository,
                translator=extractor,
                service=service,
                multiuser_enabled=telegram.multiuser_enabled,
                auto_register=telegram.auto_register,
            )
        stop = threading.Event()
        previous_handlers = {}

        def request_shutdown(signum, _frame):
            logger = logging.getLogger(__name__)
            logger.info("shutdown_requested signal=%s", signal.Signals(signum).name)
            stop.set()

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, request_shutdown)
            if a.command == "telegram-listen":
                assert controller is not None
                controller.listen_forever(stop_event=stop)
            elif a.command == "scan":
                assert service is not None
                interval = positive_interval(
                    a.interval_minutes or os.getenv("SCAN_INTERVAL_MINUTES", "10")
                )
                run_scanner(service, interval, a.pages, stop)
            else:
                assert controller is not None and service is not None
                interval = positive_interval(
                    a.interval_minutes or os.getenv("SCAN_INTERVAL_MINUTES", "10")
                )
                run_daemon(controller, service, interval, a.pages, stop)
        except KeyboardInterrupt:
            request_shutdown(signal.SIGINT, None)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            repository.close()
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
    repository = DealRepository(a.db)
    service = AlertService(ChollometroClient(), repository, None)
    if a.command == "baseline":
        try:
            count = service.baseline(["leche", "cerveza"], a.pages, a.dry_run)
        except ChollometroError as exc:
            # No baseline is written on failure: the previous one is untouched.
            report_scan_failure(exc)
            raise SystemExit(1) from exc
        print(count)
        repository.close()
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
        repository.close()
    if service.last_scan_status == SCAN_FAILED:
        # `check` is the legacy CLI entry point: the exit code is its failure
        # signal for cron/monitoring, the metrics line is the detail.
        raise SystemExit(1)
