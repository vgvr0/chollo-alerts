"""`alert list` must show every stored rule through the canonical boundary.

The CLI used to read `repository.structured_alert_rules()`, which filters
`structured_rule IS NOT NULL`, so legacy rows #1, #3 and #4 of the local
database were invisible while Telegram showed them. Now both surfaces list
`repository.list_alert_rules()` and resolve each row with
`repository.rule_from_listing()` (structured rule, or legacy reconstruction).
"""

from decimal import Decimal

from chollometro_alerts import cli
from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.telegram_rules import format_alert_list

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
            "created-at",
            "updated-at",
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


def run_alert_list(monkeypatch, capsys, path):
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "sys.argv",
        ["chollometro-alerts", "--db", str(path), "alert", "list"],
    )
    cli.main()
    return capsys.readouterr().out.strip().splitlines()


# --- The real database shape: legacy rows plus the corrected rule #2 --------


def test_alert_list_shows_a_legacy_rule(monkeypatch, tmp_path, capsys):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    rule_id = insert_rule(
        repository,
        query="zapatillas",
        product_type="zapatillas",
        brand="ASICS",
        max_price="200",
        price_unit="unit",
    )
    assert repository.load_alert_rule(rule_id) is None

    lines = run_alert_list(monkeypatch, capsys, repository.path)

    text = "\n".join(lines)
    assert f"✅ #{rule_id} · Zapatillas ASICS" in text
    assert "💶 Menos de 200,00 €/ud" in text


def test_alert_list_shows_a_structured_rule(monkeypatch, tmp_path, capsys):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    # The legacy columns deliberately disagree with the structured rule: the
    # listing has to render the canonical AlertRule, not the raw columns.
    rule_id = insert_rule(
        repository,
        query="coca-cola",
        product_type=None,
        brand=None,
        max_price="0",
        price_unit="absolute",
        structured=AlertRule(
            query="coca-cola",
            product="refresco",
            brand="Coca-Cola",
            constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        ),
    )

    lines = run_alert_list(monkeypatch, capsys, repository.path)

    text = "\n".join(lines)
    assert f"✅ #{rule_id} · Refresco Coca-Cola" in text
    assert "💶 Menos de 0,50 €/ud" in text
    assert repository.rule_from_listing(
        repository.list_alert_rules()[0]
    ).constraints == (AlertConstraints(max_price_per_unit=Decimal("0.50")))


def test_alert_list_shows_legacy_and_structured_rules_exactly_once(
    monkeypatch, tmp_path, capsys
):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    legacy_id = insert_rule(
        repository,
        query="leche",
        product_type="leche",
        max_price="0.79",
        price_unit="liter",
    )
    structured_id = insert_rule(
        repository,
        query="coca-cola",
        structured=AlertRule(
            query="coca-cola",
            constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        ),
        enabled=0,
    )

    lines = run_alert_list(monkeypatch, capsys, repository.path)

    text = "\n".join(lines)
    assert f"✅ #{legacy_id} · Leche" in text
    assert f"⏸️ #{structured_id} · Coca-Cola" in text
    assert text.count("· Leche") == 1
    assert text.count("· Coca-Cola") == 2
    assert "⏸️ Inactiva: #2 · Coca-Cola" in text


def test_alert_list_sees_the_same_rules_as_telegram(monkeypatch, tmp_path, capsys):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    insert_rule(repository, query="leche", product_type="leche", price_unit="liter")
    insert_rule(
        repository,
        query="zapatillas",
        product_type="zapatillas",
        max_price="200",
        structured=AlertRule(
            query="zapatillas",
            product="zapatillas",
            brand="ASICS",
            constraints=AlertConstraints(max_price=Decimal(200)),
        ),
    )
    insert_rule(repository, query="GHD", enabled=0)

    lines = run_alert_list(monkeypatch, capsys, repository.path)

    # `list_alert_rules()` is exactly what the Telegram listing reads.
    telegram_view = [row[0] for row in repository.list_alert_rules()]
    listed_ids = [
        int(line.split("#", 1)[1].split(" ", 1)[0])
        for line in lines
        if "#" in line and line.startswith(("✅ #", "⏸️ #"))
    ]
    assert listed_ids == telegram_view


def test_cli_and_telegram_render_the_same_inactive_summary(
    monkeypatch, tmp_path, capsys
):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    insert_rule(repository, query="leche", product_type="leche")
    insert_rule(repository, query="zapatillas", product_type="zapatillas", enabled=0)
    insert_rule(repository, query="televisores", product_type="televisores", enabled=0)

    lines = run_alert_list(monkeypatch, capsys, repository.path)
    cli_text = "\n".join(lines)
    telegram_text = format_alert_list(
        repository.list_alert_rules(), repository.rule_from_listing
    )

    assert cli_text == telegram_text
    assert "⏸️ Inactivas: #2 · Zapatillas, #3 · Televisores" in cli_text


# --- Legacy price semantics stay untouched ----------------------------------


def test_alert_list_resolves_legacy_price_units_through_the_canonical_rule(
    monkeypatch, tmp_path, capsys
):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    insert_rule(
        repository,
        query="leche",
        product_type="leche",
        max_price="0.79",
        price_unit="liter",
    )
    insert_rule(
        repository,
        query="zapatillas",
        product_type="zapatillas",
        max_price="200",
        price_unit="unit",
    )
    insert_rule(
        repository,
        query="mini pc",
        product_type="mini pc",
        max_price="400",
        price_unit="absolute",
    )

    lines = run_alert_list(monkeypatch, capsys, repository.path)

    text = "\n".join(lines)
    assert text.count("💶") == 3
    constraints = [
        repository.rule_from_listing(row).constraints
        for row in repository.list_alert_rules()
    ]
    assert constraints[0].max_price_per_liter == Decimal("0.79")
    assert constraints[0].max_price is None
    assert constraints[1].max_price_per_unit == Decimal(200)
    assert constraints[1].max_price is None
    assert constraints[2].max_price == Decimal(400)
    assert constraints[2].max_price_per_unit is None


# --- Read only --------------------------------------------------------------


def test_alert_list_never_writes(monkeypatch, tmp_path, capsys):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    legacy_id = insert_rule(
        repository,
        query="zapatillas",
        product_type="zapatillas",
        brand="ASICS",
        max_price="200",
        price_unit="unit",
    )
    insert_rule(
        repository,
        query="coca-cola",
        structured=AlertRule(
            query="coca-cola",
            constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        ),
    )
    before = snapshot(repository)

    lines = run_alert_list(monkeypatch, capsys, repository.path)

    text = "\n".join(lines)
    assert text.count("#") == 2
    assert snapshot(repository) == before
    # In particular, the legacy row is still legacy.
    assert repository.load_alert_rule(legacy_id) is None
    assert repository.rule_by_id(legacy_id).constraints.max_price_per_unit == Decimal(
        200
    )


def test_alert_list_works_without_any_rule(monkeypatch, tmp_path, capsys):
    repository = DealRepository(tmp_path / "rules.sqlite3")
    assert "No tienes alertas configuradas." in "\n".join(
        run_alert_list(monkeypatch, capsys, repository.path)
    )
