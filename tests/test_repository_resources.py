import pytest

from chollometro_alerts.repository import DealRepository


def test_repository_context_closes_connection_after_exception(tmp_path):
    repository = DealRepository(tmp_path / "context.sqlite3")

    with pytest.raises(RuntimeError, match="boom"), repository:
        repository.db.execute("SELECT 1")
        raise RuntimeError("boom")

    assert repository._connections == {}
