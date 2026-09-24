from datetime import UTC, datetime, timedelta

import pytest

from chollometro_alerts.config import ConfigurationError, RetentionSettings
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.retention import RetentionService

NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)


def repo(tmp_path):
    return DealRepository(tmp_path / "retention.sqlite3")


def settings(**overrides):
    values = RetentionSettings(
        enabled=True,
        snapshots_hours=24,
        llm_cache_days=30,
        error_history_days=30,
        scan_history_days=30,
        batch_size=2,
        interval_hours=24,
    ).__dict__
    values.update(overrides)
    return RetentionSettings(**values)


def test_default_policy_is_conservative(monkeypatch):
    for name in (
        "RETENTION_ENABLED",
        "RETENTION_FEED_THREADS_DAYS",
        "RETENTION_OBSERVATIONS_DAYS",
        "RETENTION_SNAPSHOTS_HOURS",
        "RETENTION_LLM_CACHE_DAYS",
        "RETENTION_ERROR_HISTORY_DAYS",
        "RETENTION_SCAN_HISTORY_DAYS",
        "RETENTION_BATCH_SIZE",
        "RETENTION_INTERVAL_HOURS",
    ):
        monkeypatch.delenv(name, raising=False)
    value = RetentionSettings.from_env()
    assert value.enabled is True
    assert value.feed_threads_days == 0
    assert value.observations_days == 0
    assert value.snapshots_hours == 24
    assert value.batch_size == 500


def test_nonzero_dedup_ttl_is_rejected(monkeypatch):
    monkeypatch.setenv("RETENTION_FEED_THREADS_DAYS", "1")
    with pytest.raises(ConfigurationError):
        RetentionSettings.from_env()


def test_retention_disabled_skips_automatic_runs(tmp_path):
    repository = repo(tmp_path)
    disabled = settings(enabled=False)
    assert (
        RetentionService(repository, disabled, clock=lambda: NOW).run_if_due(NOW)
        is None
    )


def test_cleanup_is_bounded_to_one_batch_per_category(tmp_path):
    repository = repo(tmp_path)
    old = NOW - timedelta(days=2)
    repository.db.executemany(
        "INSERT INTO deal_temperature_snapshots(thread_id,temperature,observed_at) VALUES (?,?,?)",
        [(str(index), 10, old.isoformat()) for index in range(5)],
    )
    repository.db.commit()
    result = RetentionService(
        repository, settings(batch_size=2), clock=lambda: NOW
    ).run(now=NOW)
    assert result.deleted_snapshots == 2
    assert (
        repository.db.execute(
            "SELECT COUNT(*) FROM deal_temperature_snapshots"
        ).fetchone()[0]
        == 3
    )


def test_prune_deletes_only_safe_old_history_and_preserves_critical_state(tmp_path):
    repository = repo(tmp_path)
    old = NOW - timedelta(days=60)
    recent = NOW - timedelta(hours=2)
    repository.db.execute(
        "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        ("old", "old", "u", None, None, None, "x", old.isoformat(), old.isoformat()),
    )
    repository.db.execute(
        "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        (
            "new",
            "new",
            "u",
            None,
            None,
            None,
            "x",
            recent.isoformat(),
            recent.isoformat(),
        ),
    )
    repository.db.execute(
        "INSERT INTO feed_threads VALUES (?,?,?)",
        ("old", old.isoformat(), old.isoformat()),
    )
    repository.db.execute(
        "INSERT INTO deal_temperature_snapshots(thread_id,temperature,observed_at) VALUES (?,?,?)",
        ("old", 10, old.isoformat()),
    )
    repository.db.execute(
        "INSERT INTO deal_temperature_snapshots(thread_id,temperature,observed_at) VALUES (?,?,?)",
        ("new", 20, recent.isoformat()),
    )
    repository.db.execute(
        "INSERT INTO product_extractions VALUES (?,?,?,?)", ("old", "{}", "x", "v")
    )
    repository.db.execute(
        "INSERT INTO product_extractions VALUES (?,?,?,?)", ("new", "{}", "x", "v")
    )
    repository.db.execute(
        "INSERT INTO error_alerts VALUES (?,?,?,?,?,?,?,1)",
        ("old", "E", "C", "m", old.isoformat(), old.isoformat(), None),
    )
    repository.db.execute(
        "INSERT INTO scan_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "old",
            "q",
            None,
            old.isoformat(),
            old.isoformat(),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "SUCCESS",
            None,
        ),
    )
    repository.db.commit()
    result = RetentionService(repository, settings(), clock=lambda: NOW).run(now=NOW)
    assert result.total_deleted == 4
    assert repository.db.execute("SELECT COUNT(*) FROM feed_threads").fetchone()[0] == 1
    assert repository.db.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 2
    assert (
        repository.db.execute(
            "SELECT COUNT(*) FROM deal_temperature_snapshots"
        ).fetchone()[0]
        == 1
    )
    assert (
        repository.db.execute("SELECT COUNT(*) FROM product_extractions").fetchone()[0]
        == 1
    )


def test_dry_run_is_read_only_and_idempotent(tmp_path):
    repository = repo(tmp_path)
    old = NOW - timedelta(days=60)
    repository.db.execute(
        "INSERT INTO error_alerts VALUES (?,?,?,?,?,?,?,1)",
        ("old", "E", "C", "m", old.isoformat(), old.isoformat(), None),
    )
    repository.db.commit()
    service = RetentionService(repository, settings(), clock=lambda: NOW)
    before_status = repository.runtime_status()
    preview = service.run(dry_run=True, now=NOW)
    assert preview.deleted_error_history == 1
    assert repository.db.execute("SELECT COUNT(*) FROM error_alerts").fetchone()[0] == 1
    assert repository.runtime_status() == before_status
    assert service.run(now=NOW).deleted_error_history == 1
    assert service.run(now=NOW).total_deleted == 0


def test_pending_notification_is_not_deleted(tmp_path):
    repository = repo(tmp_path)
    old = NOW - timedelta(days=60)
    repository.db.execute(
        "INSERT INTO alert_rules(query,product_type,brand,max_price,price_unit,enabled,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "q",
            None,
            None,
            "1",
            "absolute",
            1,
            "ACTIVE",
            old.isoformat(),
            old.isoformat(),
        ),
    )
    rule_id = repository.db.execute("SELECT id FROM alert_rules").fetchone()[0]
    repository.db.execute(
        "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        (
            "pending",
            "pending",
            "u",
            None,
            None,
            None,
            "x",
            old.isoformat(),
            old.isoformat(),
        ),
    )
    repository.db.execute(
        "INSERT INTO rule_deal_observations(rule_id,deal_id,first_seen_at,last_seen_at,matched,pending_reason) VALUES (?,?,?,?,?,?)",
        (rule_id, "pending", old.isoformat(), old.isoformat(), 1, "TELEGRAM_FAILURE"),
    )
    repository.db.commit()
    RetentionService(repository, settings(), clock=lambda: NOW).run(now=NOW)
    assert repository.pending_rule_notifications() == [(rule_id, "pending")]
    assert repository.exists("pending")


def test_auto_maintenance_survives_restart(tmp_path):
    repository = repo(tmp_path)
    service = RetentionService(repository, settings(), clock=lambda: NOW)
    assert service.run_if_due(NOW) is not None
    repository.close()
    restarted = repo(tmp_path)
    assert (
        RetentionService(restarted, settings(), clock=lambda: NOW).run_if_due(NOW)
        is None
    )
