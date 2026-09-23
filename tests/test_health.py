from datetime import UTC, datetime, timedelta

import pytest

from chollometro_alerts import cli
from chollometro_alerts.health import DEGRADED, HEALTHY, UNHEALTHY, evaluate
from chollometro_alerts.repository import DealRepository


def utc_now():
    return datetime.now(UTC).replace(microsecond=0)


def successful_scan(repository, when):
    repository.runtime_daemon_started()
    run_id = repository.runtime_scan_started("run-success")
    repository.runtime_scan_finished(run_id, "SUCCESS")
    repository.db.execute(
        "UPDATE runtime_status SET last_scan_started_at=?, last_scan_finished_at=?, last_scan_completed_at=? WHERE id=1",
        tuple(when.isoformat() for _ in range(3)),
    )
    repository.db.commit()


def test_fresh_database_is_starting_during_grace_period(tmp_path):
    repository = DealRepository(tmp_path / "health.sqlite3")
    now = utc_now()
    repository.runtime_status()

    report = evaluate(repository, interval_minutes=10, now=now + timedelta(minutes=1))

    assert report.status == HEALTHY
    assert report.scanner == "STARTING"
    assert report.exit_code == 0


def test_successful_scan_is_healthy(tmp_path):
    repository = DealRepository(tmp_path / "health.sqlite3")
    now = utc_now()
    successful_scan(repository, now)

    report = evaluate(repository, interval_minutes=10, now=now + timedelta(minutes=2))

    assert report.status == HEALTHY
    assert report.database == "OK"
    assert report.scanner == "OK"
    assert report.consecutive_failures == 0


def test_recent_failed_scan_is_degraded_and_success_resets_failures(tmp_path):
    repository = DealRepository(tmp_path / "health.sqlite3")
    now = utc_now()
    successful_scan(repository, now)
    for number in range(2):
        run_id = repository.runtime_scan_started(f"run-failed-{number}")
        repository.runtime_scan_finished(run_id, "FAILED", "TIMEOUT")

    report = evaluate(repository, interval_minutes=10, now=now + timedelta(minutes=3))
    assert report.status == DEGRADED
    assert report.consecutive_failures == 2
    assert report.last_error_type == "TIMEOUT"

    run_id = repository.runtime_scan_started("run-recovery")
    repository.runtime_scan_finished(run_id, "SUCCESS")
    assert repository.runtime_status()["consecutive_failures"] == 0


def test_stale_scanner_is_unhealthy(tmp_path):
    repository = DealRepository(tmp_path / "health.sqlite3")
    now = utc_now()
    successful_scan(repository, now - timedelta(minutes=31))

    report = evaluate(repository, interval_minutes=10, now=now)

    assert report.status == UNHEALTHY
    assert report.exit_code != 0


def test_database_failure_is_unhealthy(tmp_path):
    repository = DealRepository(tmp_path / "missing" / "health.sqlite3")

    report = evaluate(repository, interval_minutes=10, now=utc_now())

    assert report.status == UNHEALTHY
    assert report.database == "ERROR"
    assert report.exit_code != 0


def test_health_cli_exit_codes(tmp_path, monkeypatch):
    path = tmp_path / "health.sqlite3"
    repository = DealRepository(path)
    successful_scan(repository, utc_now())
    monkeypatch.setattr("sys.argv", ["chollometro-alerts", "--db", str(path), "health"])

    with pytest.raises(SystemExit) as healthy:
        cli.main()
    assert healthy.value.code == 0

    repository.db.execute(
        "UPDATE runtime_status SET last_scan_finished_at=?, last_scan_completed_at=? WHERE id=1",
        tuple((utc_now() - timedelta(minutes=31)).isoformat() for _ in range(2)),
    )
    repository.db.commit()
    with pytest.raises(SystemExit) as unhealthy:
        cli.main()
    assert unhealthy.value.code == 1
