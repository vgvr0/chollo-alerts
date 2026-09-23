import logging
import threading

from .errors import SCAN_SUCCESS

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
    logger.info("daemon.started interval_minutes=%s", interval_minutes)
    try:
        while not stop_event.is_set():
            try:
                rules = service.repository.list_alert_rules(enabled_only=True)
                logger.info(
                    "scan_started event=scan.started active_rules=%s", len(rules)
                )
                service.run_active_rules(pages=pages)
                # A provider failure is recorded per scan and must never stop
                # the loop: the next cycle simply tries again.
                status = getattr(service, "last_scan_status", SCAN_SUCCESS)
                logger.info("scan_finished status=%s", status)
                logger.info("scan.completed status=%s", status)
            except Exception:
                logger.exception("scan_failed event=scan.failed")
            stop_event.wait(interval_minutes * 60)
    finally:
        stop_event.set()
        listener.join(timeout=2)
        service.repository.close_current_thread()
