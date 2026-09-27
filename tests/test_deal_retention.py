from datetime import UTC, datetime, timedelta

import pytest

from chollometro_alerts.config import ConfigurationError, RetentionSettings
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.retention import RetentionService

NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)


def settings(**overrides):
    values = RetentionSettings(batch_size=2).__dict__
    values.update(overrides)
    return RetentionSettings(**values)


def insert_deal(repo, deal_id, published_at, site="chollometro"):
    repo.db.execute(
        "INSERT INTO deals (deal_id,title,url,category,published_at,first_seen_at,site,currency) VALUES (?,?,?,?,?,?,?,?)",
        (
            deal_id,
            deal_id,
            "https://example.test",
            "other",
            published_at.isoformat(),
            published_at.isoformat(),
            site,
            "EUR",
        ),
    )


def test_retention_default_and_override(monkeypatch):
    monkeypatch.delenv("DEAL_RETENTION_DAYS", raising=False)
    assert RetentionSettings.from_env().deal_retention_days == 15
    monkeypatch.setenv("DEAL_RETENTION_DAYS", "30")
    assert RetentionSettings.from_env().deal_retention_days == 30
    monkeypatch.setenv("DEAL_RETENTION_DAYS", "0")
    with pytest.raises(ConfigurationError):
        RetentionSettings.from_env()


def test_retention_boundaries(tmp_path):
    repo = DealRepository(tmp_path / "retention.sqlite3")
    for deal_id, age in (("d14", 14), ("d15", 15), ("d16", 16)):
        insert_deal(repo, deal_id, NOW - timedelta(days=age))
    repo.db.commit()
    result = RetentionService(repo, settings(), clock=lambda: NOW).run(now=NOW)
    assert result.deleted_deals == 1
    assert {row[0] for row in repo.db.execute("SELECT deal_id FROM deals")} == {
        "d14",
        "d15",
    }


def test_retention_multisite_and_idempotent(tmp_path):
    repo = DealRepository(tmp_path / "retention.sqlite3")
    old, recent = NOW - timedelta(days=16), NOW - timedelta(days=1)
    for deal_id, stamp, site in (
        ("same", old, "chollometro"),
        ("same", old, "promodescuentos"),
        ("new-es", recent, "chollometro"),
        ("new-mx", recent, "promodescuentos"),
    ):
        insert_deal(repo, deal_id, stamp, site)
    repo.db.commit()
    service = RetentionService(repo, settings(), clock=lambda: NOW)
    assert service.run(now=NOW).deleted_deals == 2
    assert service.run(now=NOW).deleted_deals == 0


def test_retention_cascades_dependencies(tmp_path):
    repo = DealRepository(tmp_path / "retention.sqlite3")
    old, site = NOW - timedelta(days=16), "pepper_nl"
    insert_deal(repo, "old", old, site)
    repo.db.execute(
        "INSERT INTO product_extractions(deal_id,payload,site) VALUES (?,?,?)",
        ("old", "{}", site),
    )
    repo.db.execute(
        "INSERT INTO deal_rule_matches(deal_id,rule_id,matched_at,site) VALUES (?,?,?,?)",
        ("old", 1, old.isoformat(), site),
    )
    repo.db.execute(
        "INSERT INTO rule_deal_observations(rule_id,deal_id,first_seen_at,last_seen_at,site) VALUES (?,?,?,?,?)",
        (1, "old", old.isoformat(), old.isoformat(), site),
    )
    repo.db.execute(
        "INSERT INTO feed_threads(thread_id,published_at,first_seen_at,site) VALUES (?,?,?,?)",
        ("old", old.isoformat(), old.isoformat(), site),
    )
    repo.db.execute(
        "INSERT INTO deal_temperature_snapshots(thread_id,temperature,observed_at,site) VALUES (?,?,?,?)",
        ("old", 10, old.isoformat(), site),
    )
    repo.db.execute(
        "INSERT INTO temperature_momentum_state(thread_id,above_threshold,updated_at,site) VALUES (?,?,?,?)",
        ("old", 1, old.isoformat(), site),
    )
    repo.db.commit()
    assert (
        RetentionService(repo, settings(), clock=lambda: NOW).run(now=NOW).deleted_deals
        == 1
    )
    for table in (
        "product_extractions",
        "deal_rule_matches",
        "rule_deal_observations",
        "deal_temperature_snapshots",
        "temperature_momentum_state",
    ):
        assert repo.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert (
        repo.db.execute(
            "SELECT COUNT(*) FROM feed_threads WHERE site=? AND thread_id=?",
            (site, "old"),
        ).fetchone()[0]
        == 1
    )
