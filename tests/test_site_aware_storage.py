"""Persistence identity tests for Pepper sites."""

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from chollometro_alerts.models import Deal
from chollometro_alerts.repository import DealRepository


def deal(site, deal_id="123", price="10"):
    return Deal(
        deal_id,
        f"Deal {site}",
        f"https://{site}.test/{deal_id}",
        Decimal(price),
        "shop",
        100,
        "category",
        datetime.now(UTC),
        site=site,
        currency="EUR" if site == "chollometro" else "MXN",
    )


def test_same_id_different_sites_are_independent(tmp_path):
    repository = DealRepository(tmp_path / "sites.db")
    assert repository.upsert(deal("chollometro", price="10"))
    assert repository.upsert(deal("promodescuentos", price="200"))
    assert repository.deal_count() == 2
    assert repository.get_deal("123", "chollometro").price == Decimal(10)
    assert repository.get_deal("123", "promodescuentos").price == Decimal(200)
    assert repository.get_deal("123", "promodescuentos").currency == "MXN"
    assert repository.upsert(deal("chollometro", price="11")) is False
    assert repository.get_deal("123", "chollometro").price == Decimal(10)


def test_extractions_matches_and_observations_are_site_aware(tmp_path):
    repository = DealRepository(tmp_path / "related.db")
    repository.save_extraction("123", {"site": "es"}, site="chollometro")
    repository.save_extraction("123", {"site": "mx"}, site="promodescuentos")
    assert repository.get_extraction("123", site="chollometro") == {"site": "es"}
    assert repository.get_extraction("123", site="promodescuentos") == {"site": "mx"}

    assert repository.claim_rule_observation(1, "123", site="chollometro")
    assert repository.claim_rule_observation(1, "123", site="promodescuentos")
    repository.record_rule_match("123", 1, "chollometro")
    repository.record_rule_match("123", 1, "promodescuentos")
    assert (
        repository.db.execute("SELECT COUNT(*) FROM deal_rule_matches").fetchone()[0]
        == 2
    )


def test_old_database_is_backfilled_and_migration_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE deals (
            deal_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
            price TEXT, merchant TEXT, temperature INTEGER, category TEXT NOT NULL,
            published_at TEXT, first_seen_at TEXT NOT NULL, notified_at TEXT
        );
        CREATE TABLE product_extractions (
            deal_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            content_fingerprint TEXT, extractor_version TEXT
        );
        CREATE TABLE deal_rule_matches (
            deal_id TEXT NOT NULL, rule_id INTEGER NOT NULL,
            matched_at TEXT NOT NULL, notified_at TEXT,
            UNIQUE(deal_id, rule_id)
        );
        CREATE TABLE rule_deal_observations (
            rule_id INTEGER NOT NULL, deal_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            baseline INTEGER NOT NULL DEFAULT 0, matched INTEGER,
            rejection_reason TEXT, notified_at TEXT, evidence TEXT,
            pending_reason TEXT, PRIMARY KEY(rule_id, deal_id)
        );
        INSERT INTO deals VALUES ('legacy-1','Old','https://old','9','shop',1,'cat','2026-01-01','2026-01-01',NULL);
        INSERT INTO product_extractions VALUES ('legacy-1','{}',NULL,NULL);
        INSERT INTO deal_rule_matches VALUES ('legacy-1',1,'2026-01-01',NULL);
        INSERT INTO rule_deal_observations VALUES (1,'legacy-1','2026-01-01','2026-01-01',0,1,NULL,NULL,NULL,NULL);
        """
    )
    db.commit()
    db.close()

    repository = DealRepository(path)
    assert repository.db.execute("PRAGMA user_version").fetchone()[0] == 2
    for table in (
        "deals",
        "product_extractions",
        "deal_rule_matches",
        "rule_deal_observations",
    ):
        assert repository.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    assert repository.get_deal("legacy-1").site == "chollometro"
    assert (
        repository.db.execute("SELECT site FROM deal_rule_matches").fetchone()[0]
        == "chollometro"
    )
    repository.close()

    reopened = DealRepository(path)
    assert reopened.db.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 1
    assert reopened.get_deal("legacy-1").title == "Old"
