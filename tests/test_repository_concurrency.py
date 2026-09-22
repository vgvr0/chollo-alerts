import sqlite3
import threading

from chollometro_alerts.models import Deal
from chollometro_alerts.repository import DealRepository


def test_telegram_and_scanner_use_independent_sqlite_connections(tmp_path):
    repository = DealRepository(tmp_path / "concurrent.sqlite3")
    deal = Deal(
        "scanner-1", "Leche", "https://example.test/1", None, None, 1, "milk", None
    )
    barrier = threading.Barrier(2)
    claimed = []
    failures = []

    def telegram_worker():
        try:
            barrier.wait()
            claimed.append(repository.claim_telegram_update(42))
        except (AssertionError, sqlite3.Error) as exc:
            failures.append(exc)
        finally:
            repository.close_current_thread()

    def scanner_worker():
        try:
            barrier.wait()
            assert repository.upsert(deal)
            repository.save_extraction("scanner-1", {"category": "milk"})
        except (AssertionError, sqlite3.Error) as exc:
            failures.append(exc)
        finally:
            repository.close_current_thread()

    threads = [
        threading.Thread(target=telegram_worker),
        threading.Thread(target=scanner_worker),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert claimed == [True]
    assert repository.claim_telegram_update(42) is False
    assert repository.deal_count() == 1
    assert repository.get_extraction("scanner-1") == {"category": "milk"}
    repository.close_current_thread()
