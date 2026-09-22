"""Deterministic product matching, pinned to the real NOVABLAST data.

Deal #2011781 stores `extraction.product_type == "Zapatillas running asfalto"`
while rule #2 stores `product == "zapatillas"`. The old exact comparison
(`extraction.product_type.casefold() == rule.product_type.casefold()`) turned
that into REJECTED_PRODUCT. These tests pin the real cause and the conservative
replacement, and they keep the production/dry-run/replay parity.
"""

from decimal import Decimal

import pytest

from chollometro_alerts.config import InterestRule
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.models import Deal
from chollometro_alerts.pricing import PricingEngine
from chollometro_alerts.product import ProductExtraction, product_type_matches
from chollometro_alerts.replay import ReplayDecision, ReplayEngine
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.service import AlertService

# --- Facts exactly as they are cached in the local database -----------------

GEL_QUANTUM = ("2011041", "Zapatillas Asics GEL-QUANTUM 360 VIII", "63.00")
NOVABLAST = (
    "2011781",
    "ASICS NOVABLAST 6 - Zapatillas running asfalto. (Última versión) Tallas 40 a 50",
    "108.75",
)
DEALS = {entry[0]: entry for entry in (GEL_QUANTUM, NOVABLAST)}


class Client:
    def __init__(self, deals):
        self.deals = deals

    def recent(self, queries, pages):
        return list(self.deals)


class RecordingNotifier:
    dry_run = False

    def __init__(self):
        self.sent = []

    def send(self, deal):
        self.sent.append(deal)

    @property
    def sent_ids(self):
        return [deal.deal_id for deal in self.sent]


def asics_rule(**constraints):
    return InterestRule(
        "generic",
        product_type="zapatillas",
        brand="ASICS",
        **constraints,
    )


def stored_extractions():
    """The cached LLM extractions of the two ASICS deals, verbatim."""
    return {
        GEL_QUANTUM[0]: ProductExtraction(
            product_type="Zapatillas", brand="Asics", extraction_source="llm"
        ),
        NOVABLAST[0]: ProductExtraction(
            product_type="Zapatillas running asfalto",
            brand="ASICS",
            extraction_source="llm",
        ),
    }


def priced(entry, extraction):
    deal_id, title, price = entry
    deal = Deal(
        deal_id,
        title,
        f"https://example.test/{deal_id}",
        Decimal(price),
        "Zalando",
        65,
        "generic",
        None,
        product_text=title,
    )
    return PricingEngine().evaluate(deal, extraction)


def store(repository, entry, extraction):
    deal = priced(entry, extraction)
    assert repository.upsert(deal)
    repository.save_extraction(entry[0], extraction.model_dump(mode="json"))
    return deal


# --- The real cause ---------------------------------------------------------


@pytest.mark.parametrize("entry", [GEL_QUANTUM, NOVABLAST])
def test_cached_llm_product_type_matches_the_rule_product(entry):
    """A descriptive LLM product type is not a different product."""
    extraction = stored_extractions()[entry[0]]
    result = apply_rule(
        priced(entry, extraction),
        asics_rule(max_price=Decimal(200)),
    )
    assert result.accepted
    assert result.reason == "ACCEPTED"


def test_absolute_price_footwear_rule_accepts_the_real_prices():
    rule = asics_rule(max_price=Decimal(200))
    for entry, extraction in (
        (GEL_QUANTUM, stored_extractions()[GEL_QUANTUM[0]]),
        (NOVABLAST, stored_extractions()[NOVABLAST[0]]),
    ):
        deal = priced(entry, extraction)
        assert deal.price < rule.max_price
        assert apply_rule(deal, rule).accepted
    assert (
        apply_rule(
            priced(NOVABLAST, stored_extractions()[NOVABLAST[0]]),
            asics_rule(max_price=Decimal(100)),
        ).reason
        == "REJECTED_PRICE"
    )


def test_price_per_unit_still_requires_a_known_quantity():
    """Caso A keeps its semantics: no quantity is ever assumed to be one."""
    extraction = stored_extractions()[NOVABLAST[0]]
    deal = priced(NOVABLAST, extraction)
    assert deal.units is None
    assert deal.price_per_unit is None
    result = apply_rule(deal, asics_rule(max_price_per_unit=Decimal(200)))
    assert not result.accepted
    assert result.reason == "REJECTED_UNKNOWN_QUANTITY"


def test_a_wrong_brand_still_rejects_after_the_product_matches():
    extraction = ProductExtraction(product_type="Zapatillas de running", brand="Nike")
    result = apply_rule(
        priced(GEL_QUANTUM, extraction), asics_rule(max_price=Decimal(200))
    )
    assert result.reason == "REJECTED_BRAND"


def test_the_singular_product_of_the_real_parser_matches_the_cached_facts():
    """`alert parse` returns product "zapatilla" for "zapatillas ASICS"."""
    rule = InterestRule(
        "generic", product_type="zapatilla", brand="ASICS", max_price=Decimal(200)
    )
    for entry in (GEL_QUANTUM, NOVABLAST):
        result = apply_rule(priced(entry, stored_extractions()[entry[0]]), rule)
        assert (result.accepted, result.reason) == (True, "ACCEPTED")


# --- The deterministic boundary of the comparison ---------------------------


@pytest.mark.parametrize(
    ("expected", "extracted"),
    [
        ("zapatillas", "Zapatillas"),
        ("zapatilla", "zapatillas"),
        ("zapatillas", "ZAPATILLAS deportivas"),
        ("zapatillas", "Zapatillas de running"),
        ("zapatillas", "Zapatillas running asfalto"),
        ("mini pc", "Mini PC NAS"),
        ("móvil", "Moviles libres"),
    ],
)
def test_product_matching_tolerates_case_accents_and_plural(expected, extracted):
    assert product_type_matches(expected, extracted)


@pytest.mark.parametrize(
    ("expected", "extracted"),
    [
        ("zapatillas", "running shoes"),
        ("zapatillas", "Botas de montaña"),
        ("mini pc", "Caja PC"),
        # A trailing qualifier is not the product: no semantic matching.
        ("leche", "Chocolate con leche"),
        ("leche", None),
        (None, "leche"),
        ("zapatillas", ""),
    ],
)
def test_product_matching_stays_conservative(expected, extracted):
    assert not product_type_matches(expected, extracted)


def test_a_trailing_qualifier_is_rejected_by_the_rule():
    deal = priced(
        ("choco", "Chocolate con leche Valor", "1.50"),
        ProductExtraction(product_type="Chocolate con leche"),
    )
    result = apply_rule(deal, InterestRule("generic", product_type="leche"))
    assert result.reason == "REJECTED_PRODUCT"


# --- Parity: production, dry-run and replay keep agreeing -------------------


def test_production_dry_run_and_replay_share_the_product_decision(tmp_path):
    repository = DealRepository(tmp_path / "parity.sqlite3")
    repository.db.execute(
        """INSERT INTO alert_rules
        (query,product_type,brand,max_price,price_unit,enabled,created_at,updated_at)
        VALUES ('zapatillas','zapatillas','ASICS','200','absolute',1,'x','x')"""
    )
    repository.db.commit()
    for deal_id, extraction in stored_extractions().items():
        store(repository, DEALS[deal_id], extraction)
    wrong_brand = store(
        repository,
        ("2010755", "Zapatillas Nike Pegasus 41 - Reactx - negro", "59.99"),
        ProductExtraction(product_type="Zapatillas", brand="Nike"),
    )
    assert wrong_brand.deal_id == "2010755"

    stored = repository.historical_deals(["zapatillas"])
    dry_run = {
        deal.deal_id: result
        for _, _, deal, _, result in AlertService(
            Client(stored), repository, RecordingNotifier()
        ).dry_run_active_rules()
    }
    replay = ReplayEngine(repository).replay(1, limit=0)
    replayed = {entry.deal_id: entry for entry in replay.results}

    assert set(replayed) == set(dry_run) == {"2011041", "2011781", "2010755"}
    for deal_id, production in dry_run.items():
        entry = replayed[deal_id]
        assert (entry.decision is ReplayDecision.MATCH) is production.accepted
        assert (entry.reason or "ACCEPTED") == production.reason
    assert replayed["2011781"].decision is ReplayDecision.MATCH
    assert replayed["2010755"].reason == "REJECTED_BRAND"

    # The production scan notifies exactly the deals the replay reports as MATCH.
    notifier = RecordingNotifier()
    sent = AlertService(Client(stored), repository, notifier).run_active_rules()
    assert sent == 2
    assert sorted(notifier.sent_ids) == sorted(
        entry.deal_id for entry in replay.matches()
    )


def test_replay_still_reports_a_missing_product_as_not_evaluable(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    repository.db.execute(
        """INSERT INTO alert_rules
        (query,product_type,brand,max_price,price_unit,enabled,created_at,updated_at)
        VALUES ('zapatillas','zapatillas','ASICS','200','absolute',1,'x','x')"""
    )
    repository.db.commit()
    extraction = ProductExtraction(brand="ASICS")
    store(repository, GEL_QUANTUM, extraction)

    report = ReplayEngine(repository).replay(1)

    entry = report.results[0]
    assert entry.decision is ReplayDecision.NOT_EVALUABLE
    assert entry.reason == "REJECTED_PRODUCT"
