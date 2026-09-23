from decimal import Decimal
from unittest.mock import Mock

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.config import InterestRule
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.models import Deal
from chollometro_alerts.product import ProductExtraction
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


class Client:
    def __init__(self, deals):
        self.deals = deals

    def recent(self, queries, pages):
        return list(self.deals)


class RecordingNotifier:
    dry_run = False

    def __init__(self):
        self.sent = []

    def send(self, deal, evidence=None):
        self.sent.append(deal)

    @property
    def sent_ids(self):
        return [deal.deal_id for deal in self.sent]


def pack_deal(deal_id="coca-24", price="10.80", title="Pack Coca-Cola 24 latas"):
    return Deal(
        deal_id,
        title,
        f"https://example.test/{deal_id}",
        Decimal(price),
        "Carrefour",
        120,
        "generic",
        None,
        product_text=title,
    )


def quantity_extractor(counts):
    """Stand-in provider: a deterministic stub would be ignored by `extract_product`."""

    def extractor(text, deal_id=None):
        return {"product_type": "refresco", "units": counts[deal_id]}

    return extractor


def insert_rule(
    repository,
    *,
    query="coca-cola",
    product_type="refresco",
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


def snapshot(repository):
    return {
        table: repository.db.execute(f"SELECT * FROM {table}").fetchall()
        for table in TABLES
    }


def test_production_matcher_rejects_nike_for_asics_rule():
    deal = Deal(
        "2010755",
        "Zapatillas Nike Pegasus 41 - Reactx - negro",
        "",
        Decimal("59.99"),
        None,
        None,
        "generic",
        None,
        product_extraction=ProductExtraction(product_type="zapatillas", brand="Nike"),
    )
    result = apply_rule(
        deal,
        InterestRule(
            "generic",
            max_price=Decimal(200),
            product_type="zapatillas",
            brand="ASICS",
        ),
    )
    assert result.reason == "REJECTED_BRAND"
    assert not result.accepted


def test_dry_run_does_not_write_repository(tmp_path):
    repo = DealRepository(tmp_path / "rules.sqlite3")
    repo.db.execute(
        "INSERT INTO alert_rules(query, product_type, brand, max_price, price_unit, enabled, created_at, updated_at) VALUES ('zapatillas','zapatillas','ASICS','200','unit',1,'x','x')"
    )
    repo.db.commit()
    before = (
        repo.deal_count(),
        repo.db.execute("SELECT COUNT(*) FROM product_extractions").fetchone()[0],
    )
    report = AlertService(
        Client(
            [
                Deal(
                    "x",
                    "ASICS zapatillas",
                    "",
                    Decimal(63),
                    None,
                    None,
                    "generic",
                    None,
                )
            ]
        ),
        repo,
        None,
        extractor=lambda text, deal_id=None: {
            "product_type": "zapatillas",
            "brand": "ASICS",
        },
    ).dry_run_active_rules()
    assert report
    assert (
        repo.deal_count(),
        repo.db.execute("SELECT COUNT(*) FROM product_extractions").fetchone()[0],
    ) == before


def test_production_and_dry_run_load_the_same_alert_rule(tmp_path):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    structured = AlertRule(
        query="coca-cola",
        product="refresco",
        constraints=AlertConstraints(
            max_price_per_unit=Decimal("0.50"), min_quantity=Decimal(24)
        ),
    )
    rule_id = insert_rule(repository, structured=structured)
    loaded = []
    original_rule_from_row = repository.rule_from_row

    def recording_rule_from_row(row):
        rule = original_rule_from_row(row)
        loaded.append(rule)
        return rule

    repository.rule_from_row = recording_rule_from_row
    service = AlertService(
        Client([pack_deal()]), repository, RecordingNotifier(), extractor=None
    )

    service.dry_run_active_rules()
    dry_run_rules = list(loaded)
    loaded.clear()
    service.run_active_rules()
    production_rules = list(loaded)

    assert dry_run_rules == production_rules == [repository.load_alert_rule(rule_id)]
    assert dry_run_rules == [structured]


def test_dry_run_decisions_match_the_production_run(tmp_path):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    insert_rule(
        repository,
        structured=AlertRule(
            query="coca-cola",
            product="refresco",
            constraints=AlertConstraints(
                max_price_per_unit=Decimal("0.50"), min_quantity=Decimal(24)
            ),
        ),
    )
    deals = [
        pack_deal("match", "10.80"),
        pack_deal("too-expensive", "12.00"),
        pack_deal("too-small", "5.40"),
        pack_deal("unknown-quantity", "9.99"),
    ]
    counts = {
        "match": 24,
        "too-expensive": 24,
        "too-small": 12,
        "unknown-quantity": None,
    }
    notifier = RecordingNotifier()
    service = AlertService(
        Client(deals), repository, notifier, extractor=quantity_extractor(counts)
    )

    dry_run = service.dry_run_active_rules()
    decisions = {deal.deal_id: result.accepted for _, _, deal, _, result in dry_run}
    reasons = {deal.deal_id: result.reason for _, _, deal, _, result in dry_run}
    assert decisions == {
        "match": True,
        "too-expensive": False,
        "too-small": False,
        "unknown-quantity": False,
    }
    assert reasons == {
        "match": "ACCEPTED",
        "too-expensive": "REJECTED_PRICE_PER_UNIT",
        "too-small": "REJECTED_QUANTITY",
        "unknown-quantity": "REJECTED_UNKNOWN_QUANTITY",
    }
    assert notifier.sent == []

    assert service.run_active_rules() == 1
    assert notifier.sent_ids == ["match"]
    assert service.dry_run_active_rules()[0][4].accepted is decisions["match"]


def test_dry_run_uses_structured_constraints_absent_from_legacy_columns(tmp_path):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    structured = AlertRule(
        query="coca-cola",
        product="refresco",
        constraints=AlertConstraints(
            max_price_per_unit=Decimal("0.50"), min_quantity=Decimal(24)
        ),
    )
    rule_id = insert_rule(
        repository, structured=structured, max_price="0", price_unit="absolute"
    )
    # Rebuilding the rule from the legacy columns alone would apply max_price=0.
    legacy_only = repository.rule_from_row(
        (rule_id, "coca-cola", "refresco", None, "0", "absolute", 1, None)
    )
    assert legacy_only.constraints.max_price == Decimal(0)

    report = AlertService(
        Client([pack_deal()]),
        repository,
        RecordingNotifier(),
        extractor=quantity_extractor({"coca-24": 24}),
    ).dry_run_active_rules()

    assert len(report) == 1
    reported_id, query, deal, interest_rule, result = report[0]
    assert (reported_id, query) == (rule_id, "coca-cola")
    assert interest_rule.max_price_per_unit == Decimal("0.50")
    assert interest_rule.min_quantity == Decimal(24)
    assert deal.price_per_unit == Decimal("0.45")
    assert (result.accepted, result.reason) == (True, "ACCEPTED")


def test_dry_run_never_persists_state_or_notifies(tmp_path):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    insert_rule(
        repository,
        structured=AlertRule(
            query="coca-cola",
            product="refresco",
            constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        ),
    )
    baseline = pack_deal("baseline", "3.00")
    repository.upsert(baseline)
    repository.mark_notified(baseline.deal_id)
    repository.claim_rule_observation(1, baseline.deal_id, baseline=True)
    before = snapshot(repository)
    notifier = Mock(dry_run=False)

    report = AlertService(
        Client([pack_deal("new", "10.80")]),
        repository,
        notifier,
        extractor=quantity_extractor({"new": 24}),
    ).dry_run_active_rules()

    assert [(deal.deal_id, result.accepted) for _, _, deal, _, result in report] == [
        ("new", True)
    ]
    notifier.send.assert_not_called()
    assert snapshot(repository) == before
    assert repository.get_rule_observation(1, "new") is None


def test_dry_run_reuses_cached_facts_without_asking_the_provider(tmp_path):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    insert_rule(
        repository,
        structured=AlertRule(
            query="coca-cola",
            product="refresco",
            constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        ),
    )
    deal = pack_deal()
    repository.upsert(deal)
    repository.save_extraction(
        deal.deal_id,
        ProductExtraction(
            product_type="refresco", units=24, extraction_source="llm"
        ).model_dump(mode="json"),
    )

    def exploding_extractor(text, deal_id=None):
        raise AssertionError("dry-run must not call the LLM provider")

    report = AlertService(
        Client([deal]),
        repository,
        RecordingNotifier(),
        extractor=exploding_extractor,
    ).dry_run_active_rules()

    assert [result.accepted for _, _, _, _, result in report] == [True]
    assert report[0][2].price_per_unit == Decimal("0.45")


def test_legacy_rule_rows_still_evaluate_through_rule_from_row(tmp_path):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    rule_id = insert_rule(
        repository,
        query="leche",
        product_type="leche",
        max_price="0.80",
        price_unit="liter",
    )
    assert repository.load_alert_rule(rule_id) is None
    deals = [pack_deal("cheap", "4.27"), pack_deal("expensive", "5.70")]
    report = AlertService(
        Client(deals),
        repository,
        RecordingNotifier(),
        extractor=lambda text, deal_id=None: {
            "product_type": "leche",
            "units": 6,
            "unit_volume_l": "1",
            "total_volume_l": "6",
        },
    ).dry_run_active_rules()

    decisions = {deal.deal_id: result.reason for _, _, deal, _, result in report}
    assert decisions == {"cheap": "ACCEPTED", "expensive": "REJECTED_PRICE_PER_LITER"}
    assert report[0][3].max_price_per_liter == Decimal("0.80")


def test_run_rules_cli_reports_the_canonical_dry_run(monkeypatch, tmp_path, capsys):
    from chollometro_alerts import cli

    repository = DealRepository(tmp_path / "rules.sqlite3")
    insert_rule(
        repository,
        structured=AlertRule(
            query="coca-cola",
            constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        ),
    )
    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli, "ChollometroClient", lambda: Client([pack_deal()]))
    monkeypatch.setattr(cli, "DealRepository", lambda path: repository)
    monkeypatch.setattr(
        cli,
        "AlertService",
        lambda *args: AlertService(
            Client([pack_deal()]),
            repository,
            args[2],
            extractor=quantity_extractor({"coca-24": 24}),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "chollometro-alerts",
            "--db",
            str(tmp_path / "rules.sqlite3"),
            "run-rules",
            "--dry-run",
        ],
    )

    cli.main()

    output = capsys.readouterr().out
    assert "price_unit=unit price_per_unit=0.45" in output
    assert "matched=true result=WOULD_NOTIFY" in output
    assert "MATCHED=1" in output
    assert repository.deal_count() == 0
