import logging
import threading

from .adaptive_polling import AdaptivePollingController
from .config import AdaptivePollingSettings
from .errors import SCAN_SUCCESS
from .retention import RetentionService

logger = logging.getLogger(__name__)


def positive_interval(value):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("SCAN_INTERVAL_MINUTES debe ser un entero positivo") from exc
    if result <= 0:
        raise ValueError("SCAN_INTERVAL_MINUTES debe ser un entero positivo")
    return result


def run_daemon(controller, service, interval_minutes=10, pages=1, stop_event=None):
    interval_minutes = positive_interval(interval_minutes)
    stop_event = stop_event or threading.Event()

    def listen():
        try:
            controller.listen_forever(stop_event=stop_event)
        finally:
            controller.repository.close_current_thread()

    listener = threading.Thread(target=listen, name="telegram-listener", daemon=True)
    listener.start()
    try:
        run_scanner(
            service,
            interval_minutes=interval_minutes,
            pages=pages,
            stop_event=stop_event,
        )
    finally:
        logger.info("daemon.stopping")
        stop_event.set()
        listener.join(timeout=2)
        service.repository.close_current_thread()


def run_scanner(service, interval_minutes=10, pages=1, stop_event=None):
    """Run only the scanner loop, for deployments with a separate listener."""
    interval_minutes = positive_interval(interval_minutes)
    stop_event = stop_event or threading.Event()
    adaptive = AdaptivePollingController(
        service.repository,
        AdaptivePollingSettings.from_env(),
        interval_minutes * 60,
    )
    service.adaptive_polling = adaptive
    repository = service.repository
    retention = RetentionService(repository)
    repository.runtime_daemon_started()
    logger.info(
        "scanner.started interval_minutes=%s polling_mode=%s polling_interval_seconds=%s",
        interval_minutes,
        adaptive.mode,
        adaptive.interval_seconds,
    )
    try:
        while not stop_event.is_set():
            run_id = None
            try:
                rules = service.repository.list_alert_rules(enabled_only=True)
                run_id = repository.runtime_scan_started()
                service.runtime_run_id = run_id
                logger.info(
                    "scan.started run_id=%s active_rules=%s", run_id, len(rules)
                )
                logger.info(
                    "scan_started run_id=%s active_rules=%s", run_id, len(rules)
                )
                service.run_active_rules(pages=pages)
                adaptive.observe(service)
                status = getattr(service, "last_scan_status", SCAN_SUCCESS)
                repository.runtime_scan_finished(
                    run_id, status, getattr(service, "last_scan_error_type", None)
                )
                if status == SCAN_SUCCESS:
                    logger.info("scan.completed run_id=%s status=%s", run_id, status)
                else:
                    logger.warning("scan.failed run_id=%s status=%s", run_id, status)
                logger.info("scan_finished status=%s", status)
            except Exception as exc:
                if run_id is not None:
                    repository.runtime_scan_finished(
                        run_id, "FAILED", type(exc).__name__
                    )
                    logger.exception("scan.failed run_id=%s", run_id)
                else:
                    logger.exception("scan.failed")
            try:
                retention.run_if_due()
            except Exception:
                logger.exception("retention.auto_failed")
            repository.runtime_heartbeat()
            stop_event.wait(adaptive.interval_seconds)
    finally:
        logger.info("scanner.stopping")
        repository.close_current_thread()
