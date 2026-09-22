import logging
import uuid
from dataclasses import dataclass

from .filters import apply_rule

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    found: int = 0
    classified: int = 0
    interesting: int = 0
    rejected: int = 0
    new: int = 0
    already_known: int = 0
    telegram_sent: int = 0
    errors: int = 0


class AlertService:
    def __init__(self, client, repository, notifier):
        self.client = client
        self.repository = repository
        self.notifier = notifier

    def run(self, queries, pages=1, rules=None):
        sent = 0
        self.last_summary = RunSummary()
        deals = self.client.recent(queries, pages)
        if not deals and getattr(self.repository, "deal_count", lambda: 0)() > 0:
            self.last_summary.errors += 1
            self.notify_error(
                "EMPTY_RESULTS", "Parser", "0 ofertas después de un run previo", 60
            )
        for deal in deals:
            self.last_summary.found += 1
            self.last_summary.classified += 1
            result = apply_rule(
                deal,
                (rules or {}).get(
                    deal.category,
                    __import__(
                        "chollometro_alerts.config", fromlist=["InterestRule"]
                    ).InterestRule(deal.category),
                ),
            )
            if not result.accepted:
                logger.info(
                    "deal=%s category=%s result=%s",
                    deal.deal_id,
                    deal.category,
                    result.reason,
                )
                self.last_summary.rejected += 1
                continue
            self.last_summary.interesting += 1
            inserted = self.repository.upsert(deal)
            self.last_summary.new += int(bool(inserted))
            self.last_summary.already_known += int(not bool(inserted))
            if not self.repository.was_notified(deal.deal_id):
                self.notifier.send(deal)
                if not getattr(self.notifier, "dry_run", False):
                    self.repository.mark_notified(deal.deal_id)
                sent += 1
                self.last_summary.telegram_sent += 1
        return sent

    def baseline(self, queries, pages=1, dry_run=False):
        deals = self.client.recent(queries, pages)
        if dry_run:
            return len(deals)
        for deal in deals:
            self.repository.upsert(deal)
            self.repository.mark_notified(deal.deal_id)
        return len(deals)

    def notify_error(
        self, error_type, component, message, cooldown_minutes=60, run_id=None
    ):
        if getattr(self.notifier, "dry_run", False):
            return False
        if not self.repository.error_alert_allowed(
            error_type, component, message, cooldown_minutes
        ):
            return False
        try:
            self.notifier.send_system_alert(
                error_type, component, message, run_id or uuid.uuid4().hex
            )
            return True
        except Exception:  # noqa: BLE001 - error reporting must never crash the run
            return False
