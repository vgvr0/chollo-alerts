"""Alert Replay Engine v0.1: offline validation of a persisted rule."""

from decimal import Decimal

import pytest

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.models import Deal
from chollometro_alerts.product import ProductExtraction
from chollometro_alerts.replay import (
    ReplayDealResult,
    ReplayDecision,
    ReplayEngine,
    ReplayResult,
    RuleNotFoundError,
    query_terms,
)
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.service import AlertService

TABLES = (
    "deals",
    "product_extractions",
    "alert_rules",
    "rule_deal_observations",
    "deal_rule_matches",
    "scan_runs",
    "telegram_updates",
    "error_alerts",
)


@pytest.fixture(autouse=True)
def offline_env(monkeypatch):
    """Replay must work with the LLM provider disabled and dotenv ignored."""
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


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


def insert_rule(
    repository,
    *,
    query,
    product_type=None,
    brand=None,
    max_price="0",
    price_unit="absolute",
    structured=None,
    enabled=1,
):
    """Insert a row exactly as the legacy and structured writers would."""
    repository.db.execute(
        """INSERT INTO alert_rules
        (query,product_type,brand,max_price,price_unit,enabled,created_at,updated_at,
        structured_rule) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            query,
            product_type,
            brand,
            max_price,
            price_unit,
            enabled,
            "x",
            "x",
            structured.model_dump_json() if structured is not None else None,
        ),
    )
    repository.db.commit()
    return repository.db.execute("SELECT last_insert_rowid()").fetchone()[0]


def store_deal(
    repository,
    deal_id,
    title,
    price,
    *,
    extraction=None,
    merchant="Amazon",
    temperature=120,
    category="generic",
):
    deal = Deal(
        deal_id,
        title,
        f"https://example.test/{deal_id}",
        Decimal(price) if price is not None else None,
        merchant,
        temperature,
        category,
        None,
        product_text=title,
    )
    assert repository.upsert(deal)
    if extraction is not None:
        repository.save_extraction(deal_id, extraction.model_dump(mode="json"))
    return deal


def snapshot(repository):
    return {
        table: repository.db.execute(f"SELECT * FROM {table}").fetchall()
        for table in TABLES
    }


def shoe_facts(brand, product_type="zapatillas"):
    return ProductExtraction(product_type=product_type, brand=brand)


def asics_rule(max_price="80"):
    return AlertRule(
        query="zapatillas",
        product="zapatillas",
        brand="ASICS",
        constraints=AlertConstraints(max_price=Decimal(max_price)),
    )


def replay_one(repository, rule_id, decision):
    report = ReplayEngine(repository).replay(rule_id)
    assert len(report.results) == 1
    assert report.results[0].decision is decision
    return report.results[0]


# --- Classification --------------------------------------------------------


def test_replay_matches_a_stored_deal_below_the_max_price(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="zapatillas", structured=asics_rule())
    store_deal(
        repository,
        "2011041",
        "Zapatillas ASICS Gel Nimbus 26",
        "74.99",
        extraction=shoe_facts("ASICS"),
    )

    report = ReplayEngine(repository).replay(rule_id)

    assert isinstance(report, ReplayResult)
    assert isinstance(report.results[0], ReplayDealResult)
    assert ReplayResult.model_validate(report.model_dump()) == report
    assert (report.rule_id, report.query, report.brand) == (
        rule_id,
        "zapatillas",
        "ASICS",
    )
    assert (report.deals_available, report.deals_evaluated) == (1, 1)
    assert (report.matched, report.rejected, report.not_evaluable) == (1, 0, 0)
    entry = report.results[0]
    assert entry.decision is ReplayDecision.MATCH
    assert entry.reason is None
    assert (entry.deal_id, entry.price) == ("2011041", Decimal("74.99"))
    assert [m.deal_id for m in report.matches()] == ["2011041"]


def test_replay_rejects_a_deal_over_the_max_price(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="zapatillas", structured=asics_rule())
    store_deal(
        repository,
        "2011042",
        "Zapatillas ASICS Gel Nimbus 27",
        "119.00",
        extraction=shoe_facts("ASICS"),
    )

    report = ReplayEngine(repository).replay(rule_id)

    assert (report.matched, report.rejected) == (0, 1)
    entry = report.results[0]
    assert (entry.decision, entry.reason) == (ReplayDecision.REJECT, "REJECTED_PRICE")
    assert [r.reason for r in report.rejections()] == ["REJECTED_PRICE"]


def test_replay_rejects_a_deal_from_another_brand(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="zapatillas", structured=asics_rule())
    store_deal(
        repository,
        "2011043",
        "Zapatillas Nike Pegasus 41",
        "59.99",
        extraction=shoe_facts("Nike"),
    )

    entry = replay_one(repository, rule_id, ReplayDecision.REJECT)
    assert entry.reason == "REJECTED_BRAND"


def test_replay_matches_price_per_unit_with_stored_quantity(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(
        repository,
        query="coca-cola",
        structured=AlertRule(
            query="coca-cola",
            product="refresco",
            constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        ),
    )
    store_deal(
        repository,
        "coca-24",
        "Pack Coca-Cola 24 latas 33 cl",
        "10.80",
        extraction=ProductExtraction(product_type="refresco", units=24),
    )

    report = ReplayEngine(repository).replay(rule_id)
    assert (report.matched, report.results[0].decision) == (1, ReplayDecision.MATCH)


def test_replay_keeps_price_per_unit_exclusive_semantics(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(
        repository,
        query="coca-cola",
        structured=AlertRule(
            query="coca-cola",
            product="refresco",
            constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        ),
    )
    store_deal(
        repository,
        "coca-24",
        "Pack Coca-Cola 24 latas 33 cl",
        "12.00",
        extraction=ProductExtraction(product_type="refresco", units=24),
    )

    entry = replay_one(repository, rule_id, ReplayDecision.REJECT)
    assert entry.reason == "REJECTED_PRICE_PER_UNIT"


def test_replay_reports_missing_quantity_as_not_evaluable(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(
        repository,
        query="coca-cola",
        structured=AlertRule(
            query="coca-cola",
            product="refresco",
            constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        ),
    )
    store_deal(
        repository,
        "coca-pack",
        "Coca-Cola Zero pack familiar",
        "9.99",
        extraction=ProductExtraction(product_type="refresco"),
    )

    report = ReplayEngine(repository).replay(rule_id)
    assert (report.matched, report.rejected, report.not_evaluable) == (0, 0, 1)
    entry = report.results[0]
    # Replay relabels the verdict; production semantics stay untouched.
    assert entry.decision is ReplayDecision.NOT_EVALUABLE
    assert entry.reason == "REJECTED_UNKNOWN_QUANTITY"
    assert report.not_evaluable_results() == [entry]


def test_replay_reports_missing_volume_as_not_evaluable(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(
        repository,
        query="leche",
        product_type="leche",
        max_price="0.80",
        price_unit="liter",
    )
    store_deal(
        repository,
        "milk-1",
        "Leche Puleva sin datos de volumen",
        "1.20",
        extraction=ProductExtraction(product_type="leche", units=6),
    )

    entry = replay_one(repository, rule_id, ReplayDecision.NOT_EVALUABLE)
    assert entry.reason == "REJECTED_UNKNOWN_VOLUME"


def test_replay_reports_unknown_facts_instead_of_a_wrong_rejection(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="zapatillas", structured=asics_rule())
    # No persisted extraction and no brand in the title: nothing to compare.
    store_deal(repository, "2011045", "Zapatillas de running talla 42", "39.99")

    entry = replay_one(repository, rule_id, ReplayDecision.NOT_EVALUABLE)
    assert entry.reason == "REJECTED_PRODUCT"


def test_replay_matches_price_per_liter(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(
        repository,
        query="leche",
        product_type="leche",
        max_price="0.80",
        price_unit="liter",
    )
    store_deal(
        repository,
        "milk-cheap",
        "Leche Lauki 6x1L",
        "4.27",
        extraction=ProductExtraction(
            product_type="leche", units=6, unit_volume_l=Decimal(1)
        ),
    )
    store_deal(
        repository,
        "milk-pricey",
        "Leche Pascual 6x1L",
        "5.70",
        extraction=ProductExtraction(
            product_type="leche", units=6, unit_volume_l=Decimal(1)
        ),
    )

    report = ReplayEngine(repository).replay(rule_id)
    decisions = {r.deal_id: (r.decision, r.reason) for r in report.results}
    assert decisions == {
        "milk-cheap": (ReplayDecision.MATCH, None),
        "milk-pricey": (ReplayDecision.REJECT, "REJECTED_PRICE_PER_LITER"),
    }


def test_replay_evaluates_every_constraint_of_the_rule(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(
        repository,
        query="coca-cola",
        structured=AlertRule(
            query="coca-cola",
            product="refresco",
            constraints=AlertConstraints(
                max_price_per_unit=Decimal("0.50"),
                min_quantity=Decimal(24),
                min_temperature=100,
            ),
        ),
    )
    facts = ProductExtraction(product_type="refresco", units=24)
    store_deal(repository, "ok", "Pack Coca-Cola 24 latas", "10.80", extraction=facts)
    store_deal(
        repository,
        "small",
        "Pack Coca-Cola 12 latas",
        "5.40",
        extraction=ProductExtraction(product_type="refresco", units=12),
    )
    store_deal(
        repository,
        "cold",
        "Pack Coca-Cola 24 latas frio",
        "10.80",
        extraction=facts,
        temperature=99,
    )

    report = ReplayEngine(repository).replay(rule_id)
    decisions = {r.deal_id: (r.decision, r.reason) for r in report.results}
    assert decisions == {
        "ok": (ReplayDecision.MATCH, None),
        "small": (ReplayDecision.REJECT, "REJECTED_QUANTITY"),
        "cold": (ReplayDecision.REJECT, "REJECTED_TEMPERATURE"),
    }


# --- Rule loading ----------------------------------------------------------


def test_structured_constraints_absent_from_legacy_columns_are_used(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    structured = AlertRule(
        query="coca-cola",
        product="refresco",
        constraints=AlertConstraints(
            max_price_per_unit=Decimal("0.50"), min_quantity=Decimal(24)
        ),
    )
    rule_id = insert_rule(
        repository, query="coca-cola", structured=structured, max_price="0"
    )
    store_deal(
        repository,
        "coca-24",
        "Pack Coca-Cola 24 latas",
        "10.80",
        extraction=ProductExtraction(product_type="refresco", units=24),
    )

    assert ReplayEngine(repository).load_rule(rule_id) == structured
    report = ReplayEngine(repository).replay(rule_id)
    assert report.constraints == structured.constraints
    assert report.results[0].decision is ReplayDecision.MATCH


def test_legacy_rules_are_loaded_through_rule_from_row(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(
        repository,
        query="leche",
        product_type="leche",
        max_price="0.80",
        price_unit="liter",
    )
    assert repository.load_alert_rule(rule_id) is None
    loaded = repository.rule_by_id(rule_id)
    assert isinstance(loaded, AlertRule)
    assert loaded.constraints.max_price_per_liter == Decimal("0.80")
    assert loaded.product == "leche"
    store_deal(
        repository,
        "milk-cheap",
        "Leche Lauki 6x1L",
        "4.27",
        extraction=ProductExtraction(
            product_type="leche", units=6, unit_volume_l=Decimal(1)
        ),
    )

    report = ReplayEngine(repository).replay(rule_id)
    assert report.constraints.max_price_per_liter == Decimal("0.80")
    assert report.results[0].decision is ReplayDecision.MATCH


def test_replay_rejects_an_unknown_rule_id(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    with pytest.raises(RuleNotFoundError):
        ReplayEngine(repository).replay(12)
    assert (repository.rule_by_id(12), repository.deal_count()) == (None, 0)


# --- Historical deal selection ---------------------------------------------


def test_replay_only_considers_deals_related_to_the_rule_query(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="zapatillas", structured=asics_rule())
    store_deal(
        repository,
        "shoes",
        "Zapatillas ASICS Gel Nimbus 26",
        "74.99",
        extraction=shoe_facts("ASICS"),
    )
    store_deal(repository, "coffee", "Cafetera express 16 bar", "41.49")
    store_deal(repository, "bike", "Bicicleta de montana 29 pulgadas", "399.00")

    report = ReplayEngine(repository).replay(rule_id)
    assert report.deals_available == report.deals_evaluated == 1
    assert [r.deal_id for r in report.results] == ["shoes"]


def test_replay_limit_caps_the_evaluated_deals(tmp_path):
    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="zapatillas", structured=asics_rule())
    for index in range(5):
        store_deal(
            repository,
            f"shoe-{index}",
            f"Zapatillas ASICS modelo {index}",
            "50.00",
            extraction=shoe_facts("ASICS"),
        )

    report = ReplayEngine(repository).replay(rule_id, limit=2)
    assert (report.deals_available, report.deals_evaluated) == (5, 2)
    assert (report.matched, len(report.results)) == (2, 2)
    assert len(ReplayEngine(repository).replay(rule_id, limit=0).results) == 5


def test_replay_without_local_deals_says_so_instead_of_sounding_broken(tmp_path):
    from chollometro_alerts import cli

    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="nintendo switch", structured=asics_rule())
    report = ReplayEngine(repository).replay(rule_id)
    assert (report.deals_available, report.deals_evaluated) == (0, 0)
    assert (report.matched, report.rejected, report.not_evaluable) == (0, 0, 0)
    assert report.has_historical_deals is False
    output = cli.format_replay(report)
    assert "No historical deals stored" in output
    assert "0 matches does NOT mean the alert is wrong" in output


def test_query_terms_drops_noise_words():
    assert query_terms("  Zapatillas ASICS  ") == ["zapatillas", "asics"]
    assert query_terms("pack de leche para 6") == ["pack", "leche"]
    assert query_terms("") == []


# --- Purity: no LLM, no scraping, no Telegram, no writes -------------------


def test_replay_never_offers_a_provider_to_extraction(tmp_path, monkeypatch):
    from chollometro_alerts import evaluation, product

    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(
        repository,
        query="coca-cola",
        structured=AlertRule(
            query="coca-cola",
            constraints=AlertConstraints(max_price=Decimal(11)),
        ),
    )
    store_deal(repository, "coca-24", "Pack Coca-Cola 24 latas 33 cl", "10.80")
    seen = []

    def guarded(text, llm=None, deal_id=None):
        seen.append(llm)
        assert llm is None, "replay must never hand a provider to extraction"
        return product.extract_product(text, llm=None, deal_id=deal_id)

    def forbidden_extractor(*args, **kwargs):
        raise AssertionError("replay must not build an LLM provider")

    monkeypatch.setattr(evaluation, "extract_product", guarded)
    monkeypatch.setattr("chollometro_alerts.llm.create_extractor", forbidden_extractor)

    report = ReplayEngine(repository).replay(rule_id)
    # Deterministic local facts are enough: the title carries price and volume.
    assert seen == [None]
    assert report.results[0].decision is ReplayDecision.MATCH


def test_replay_reuses_cached_facts_without_any_extraction(tmp_path, monkeypatch):
    from chollometro_alerts import evaluation

    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="zapatillas", structured=asics_rule())
    store_deal(
        repository,
        "shoe",
        "Zapatillas ASICS Gel Nimbus 26",
        "74.99",
        extraction=shoe_facts("ASICS"),
    )

    def exploding(*args, **kwargs):
        raise AssertionError("a stored deal must never trigger extraction work")

    monkeypatch.setattr(evaluation, "extract_product", exploding)

    report = ReplayEngine(repository).replay(rule_id)
    assert report.results[0].decision is ReplayDecision.MATCH


def test_alert_test_cli_never_scrapes_never_calls_the_llm_or_telegram(
    monkeypatch, tmp_path, capsys
):
    from chollometro_alerts import cli

    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="zapatillas", structured=asics_rule())
    store_deal(
        repository,
        "shoe",
        "Zapatillas ASICS Gel Nimbus 26",
        "74.99",
        extraction=shoe_facts("ASICS"),
    )

    def forbidden(name):
        def factory(*args, **kwargs):
            raise AssertionError(f"alert test must not use {name}")

        return factory

    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli, "DealRepository", lambda path: repository)
    monkeypatch.setattr(cli, "ChollometroClient", forbidden("the scraper"))
    monkeypatch.setattr(cli, "TelegramNotifier", forbidden("Telegram"))
    monkeypatch.setattr(cli, "DeepSeekProductExtractor", forbidden("the LLM provider"))
    monkeypatch.setattr(cli, "DeepSeekAlertRuleParser", forbidden("the LLM parser"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "chollometro-alerts",
            "--db",
            str(repository.path),
            "alert",
            "test",
            str(rule_id),
        ],
    )

    cli.main()

    output = capsys.readouterr().out
    assert f"RULE #{rule_id}" in output
    assert "Historical deals available: 1" in output
    assert "Deals evaluated: 1" in output
    assert "MATCH:        1" in output
    assert "Result: MATCH" in output
    assert "no Telegram, no writes" in output


def test_alert_test_cli_reports_an_unknown_rule(monkeypatch, tmp_path, capsys):
    from chollometro_alerts import cli

    repository = DealRepository(tmp_path / "replay.sqlite3")
    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli, "DealRepository", lambda path: repository)
    monkeypatch.setattr("sys.argv", ["chollometro-alerts", "alert", "test", "12"])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 2
    assert "12" in capsys.readouterr().err


def test_alert_test_does_not_touch_the_database(tmp_path, monkeypatch, capsys):
    from chollometro_alerts import cli

    repository = DealRepository(tmp_path / "replay.sqlite3")
    rule_id = insert_rule(repository, query="zapatillas", structured=asics_rule())
    store_deal(
        repository,
        "shoe",
        "Zapatillas ASICS Gel Nimbus 26",
        "74.99",
        extraction=shoe_facts("ASICS"),
    )
    store_deal(repository, "rejected", "Zapatillas Nike Pegasus 41", "59.99")
    repository.claim_rule_observation(rule_id, "shoe", baseline=True)
    before = snapshot(repository)

    ReplayEngine(repository).replay(rule_id)
    assert snapshot(repository) == before

    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli, "DealRepository", lambda path: repository)
    monkeypatch.setattr(
        "sys.argv",
        [
            "chollometro-alerts",
            "--db",
            str(repository.path),
            "alert",
            "test",
            str(rule_id),
        ],
    )
    cli.main()
    capsys.readouterr()
    assert snapshot(repository) == before


# --- Parity with the production path ---------------------------------------


def test_replay_reaches_the_same_decisions_as_production_and_dry_run(tmp_path):
    repository = DealRepository(tmp_path / "parity.sqlite3")
    structured = AlertRule(
        query="coca-cola",
        product="refresco",
        constraints=AlertConstraints(
            max_price_per_unit=Decimal("0.50"), min_quantity=Decimal(24)
        ),
    )
    rule_id = insert_rule(repository, query="coca-cola", structured=structured)
    facts = {
        "match": 24,
        "too-expensive": 24,
        "too-small": 12,
        "unknown-quantity": None,
    }
    prices = {
        "match": "10.80",
        "too-expensive": "13.20",
        "too-small": "5.40",
        "unknown-quantity": "9.99",
    }
    for deal_id, units in facts.items():
        store_deal(
            repository,
            deal_id,
            f"Pack Coca-Cola 24 latas {deal_id}",
            prices[deal_id],
            extraction=ProductExtraction(product_type="refresco", units=units),
        )
    stored = repository.historical_deals(query_terms("coca-cola"))
    service = AlertService(Client(stored), repository, RecordingNotifier())

    dry_run = {
        deal.deal_id: result for _, _, deal, _, result in service.dry_run_active_rules()
    }
    assert set(dry_run) == set(facts)
    assert service.run_active_rules() == 1

    report = ReplayEngine(repository).replay(rule_id)
    replayed = {entry.deal_id: entry for entry in report.results}
    assert set(replayed) == set(dry_run)
    for deal_id, production in dry_run.items():
        entry = replayed[deal_id]
        assert (entry.reason or "ACCEPTED") == production.reason
        assert (entry.decision is ReplayDecision.MATCH) is production.accepted
    assert {deal_id: entry.decision for deal_id, entry in replayed.items()} == {
        "match": ReplayDecision.MATCH,
        "too-expensive": ReplayDecision.REJECT,
        "too-small": ReplayDecision.REJECT,
        "unknown-quantity": ReplayDecision.NOT_EVALUABLE,
    }
