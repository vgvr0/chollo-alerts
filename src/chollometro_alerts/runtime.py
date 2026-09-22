import logging
import threading

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
        while not stop_event.is_set():
            try:
                rules = service.repository.list_alert_rules(enabled_only=True)
                logger.info("scan_started active_rules=%s", len(rules))
                service.run_active_rules(pages=pages)
                logger.info("scan_finished")
            except Exception:
                logger.exception("scan_failed")
            stop_event.wait(interval_minutes * 60)
    finally:
        stop_event.set()
        listener.join(timeout=2)
        service.repository.close_current_thread()
