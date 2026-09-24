from decimal import Decimal

from chollometro_alerts.evaluation import interest_rule_from_alert
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.legacy_rules import audit_legacy_rules, repair_legacy_rules
from chollometro_alerts.models import Deal
from chollometro_alerts.pricing import PricingEngine
from chollometro_alerts.product import ProductExtraction
from chollometro_alerts.repository import DealRepository


def legacy(
    repository, query, *, price="50", price_unit="absolute", enabled=1, state="ACTIVE"
):
    now = "2026-09-24T00:00:00+00:00"
    cur = repository.db.execute(
        """INSERT INTO alert_rules
        (query,product_type,brand,max_price,price_unit,enabled,state,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (query, None, None, price, price_unit, enabled, state, now, now),
    )
    repository.db.commit()
    return cur.lastrowid


def priced(deal_id, title, price, brand):
    deal = Deal(
        deal_id,
        title,
        "https://example.test/" + deal_id,
        Decimal(price),
        "Amazon",
        100,
        "generic",
        None,
        product_text=title,
    )
    return PricingEngine().evaluate(
        deal, ProductExtraction(brand=brand, extraction_source="deterministic")
    )


def test_lagavulin_legacy_is_safe_to_repair_and_preserves_identity(tmp_path):
    repo = DealRepository(tmp_path / "legacy.sqlite3")
    rule_id = legacy(repo, "Lagavulin pr")
    proposals = audit_legacy_rules(repo)
    proposal = next(item for item in proposals if item.rule_id == rule_id)
    assert (proposal.classification, proposal.brand) == (
        "LEGACY_SAFE_TO_ENRICH",
        "Lagavulin",
    )

    repair_legacy_rules(repo, dry_run=False)
    repaired = repo.rule_by_id(rule_id)
    row = repo.db.execute(
        "SELECT id,enabled,state,max_price,structured_rule FROM alert_rules WHERE id=?",
        (rule_id,),
    ).fetchone()
    assert repaired.brand == "Lagavulin"
    assert row[:4] == (rule_id, 1, "ACTIVE", "50")
    assert row[4] is not None
    assert repo.db.execute(
        "SELECT created_at,updated_at FROM alert_rules WHERE id=?", (rule_id,)
    ).fetchone() == ("2026-09-24T00:00:00+00:00", "2026-09-24T00:00:00+00:00")

    assert not apply_rule(
        priced("skechers", "Zapatillas Skechers", "29.90", "Skechers"),
        interest_rule_from_alert(repaired),
    ).accepted
    assert apply_rule(
        priced("lagavulin", "Lagavulin 16", "45", "Lagavulin"),
        interest_rule_from_alert(repaired),
    ).accepted
    assert not apply_rule(
        priced("lagavulin-high", "Lagavulin 16", "58", "Lagavulin"),
        interest_rule_from_alert(repaired),
    ).accepted


def test_repair_is_idempotent_and_generic_legacy_stays_ambiguous(tmp_path):
    repo = DealRepository(tmp_path / "legacy.sqlite3")
    lagavulin_id = legacy(repo, "Lagavulin pr")
    generic_id = legacy(repo, "ofertas", price="20")

    assert repair_legacy_rules(repo, dry_run=False)
    assert repair_legacy_rules(repo, dry_run=False) == []
    generic = next(
        item for item in audit_legacy_rules(repo) if item.rule_id == generic_id
    )
    assert generic.classification == "GENERIC"
    assert repo.rule_by_id(generic_id).brand is None
    assert repo.rule_by_id(lagavulin_id).brand == "Lagavulin"


def test_milk_legacy_dry_run_and_repair_enrich_product_type(tmp_path):
    repo = DealRepository(tmp_path / "legacy.sqlite3")
    rule_id = legacy(repo, "leche", price="0.79", price_unit="liter")
    proposal = next(
        item for item in audit_legacy_rules(repo) if item.rule_id == rule_id
    )
    assert (proposal.classification, proposal.product_type, proposal.brand) == (
        "LEGACY_SAFE_TO_ENRICH",
        "leche",
        None,
    )
    assert repair_legacy_rules(repo, dry_run=True)[0].rule_id == rule_id
    repair_legacy_rules(repo, dry_run=False)
    repaired = repo.rule_by_id(rule_id)
    assert repaired.product == "leche"
    assert repaired.brand is None
    assert repaired.constraints.max_price_per_liter == Decimal("0.79")


def test_unrepaired_legacy_milk_row_is_loaded_with_product_relevance(tmp_path):
    repo = DealRepository(tmp_path / "legacy.sqlite3")
    rule_id = legacy(repo, "leche", price="0.79", price_unit="liter")

    loaded = repo.rule_by_id(rule_id)

    assert loaded.product == "leche"
    assert loaded.constraints.max_price_per_liter == Decimal("0.79")
