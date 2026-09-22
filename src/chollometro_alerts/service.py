import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from .config import InterestRule
from .filters import InterestEngine
from .llm import ProductExtractor, create_extractor
from .pricing import PricingEngine
from .product import ProductExtraction, extract_product, normalize_product_extraction

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
    deterministic_count: int = 0
    llm_count: int = 0
    hybrid_count: int = 0
    llm_calls: int = 0
    llm_cache_hits: int = 0
    llm_failures: int = 0
    llm_tokens: int = 0

    def format_metrics(self) -> str:
        fields = (
            "found",
            "new",
            "deterministic_count",
            "llm_count",
            "hybrid_count",
            "llm_calls",
            "llm_cache_hits",
            "llm_failures",
            "llm_tokens",
            "telegram_sent",
        )
        return "\n".join(f"{name.upper()}={getattr(self, name)}" for name in fields)


class AlertService:
    def __init__(
        self, client, repository, notifier, extractor: ProductExtractor | None = None
    ):
        self.client = client
        self.repository = repository
        self.notifier = notifier
        self.extractor = extractor
        self.pricing = PricingEngine()
        self.interest = InterestEngine()
        self.extraction_cache_hits = 0

    def run(self, queries, pages=1, rules=None):
        sent = 0
        self.last_summary = RunSummary()
        extractor = self.extractor if self.extractor is not None else create_extractor()
        initial_metrics = dict(getattr(extractor, "metrics", {}))
        deals = self.client.recent(queries, pages)
        if not deals and getattr(self.repository, "deal_count", lambda: 0)() > 0:
            self.last_summary.errors += 1
            self.notify_error(
                "EMPTY_RESULTS", "Parser", "0 ofertas después de un run previo", 60
            )
        for deal in deals:
            self.last_summary.found += 1
            # Check identity before any possible provider call, including legacy deals.
            known = self.repository.exists(deal.deal_id)
            self.last_summary.already_known += int(known)
            cached = self.repository.get_extraction(deal.deal_id)
            if cached is not None:
                extraction = normalize_product_extraction(
                    ProductExtraction.model_validate(cached)
                )
                self.extraction_cache_hits += 1
                self.last_summary.llm_cache_hits += 1
            else:
                extraction = extract_product(
                    deal.product_text or deal.title,
                    llm=None if known else extractor,
                    deal_id=deal.deal_id,
                )
                self.repository.save_extraction(
                    deal.deal_id, extraction.model_dump(mode="json")
                )
            for field in ("llm_calls", "llm_failures", "llm_tokens"):
                key = field.upper()
                value = getattr(extractor, "metrics", {}).get(key, 0)
                setattr(self.last_summary, field, value - initial_metrics.get(key, 0))
            source_count = f"{extraction.extraction_source}_count"
            setattr(
                self.last_summary,
                source_count,
                getattr(self.last_summary, source_count) + 1,
            )
            deal = self.pricing.evaluate(deal, extraction)
            self.last_summary.classified += 1
            result = self.interest.evaluate(
                deal, (rules or {}).get(deal.category, InterestRule(deal.category))
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
            if not self.repository.was_notified(deal.deal_id):
                self.notifier.send(deal)
                if not getattr(self.notifier, "dry_run", False):
                    self.repository.mark_notified(deal.deal_id)
                    self.last_summary.telegram_sent += 1
                sent += 1
        return sent

    def baseline(self, queries, pages=1, dry_run=False):
        deals = self.client.recent(queries, pages)
        if dry_run:
            return len(deals)
        for deal in deals:
            self.repository.upsert(deal)
            self.repository.mark_notified(deal.deal_id)
        return len(deals)

    def run_active_rules(self, pages=1):
        """Scan only enabled persisted rules; comparisons remain deterministic."""
        total = 0
        for row in self.repository.list_alert_rules(enabled_only=True):
            _, query, product_type, _brand, max_price, price_unit, _ = row
            rule = InterestRule(
                category="milk" if product_type == "leche" else "beer",
                max_price=Decimal(max_price) if price_unit == "unit" else None,
                max_price_per_liter=Decimal(max_price)
                if price_unit == "liter"
                else None,
            )
            total += self.run([query], pages, {rule.category: rule})
        return total

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
