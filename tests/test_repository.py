from chollometro_alerts.models import Deal
from chollometro_alerts.repository import DealRepository


def test_idempotent(tmp_path):
    r = DealRepository(tmp_path / "x.db")
    d = Deal("1", "Leche", "u", None, None, None, "milk", None)
    assert r.upsert(d)
    assert not r.upsert(d)
    assert not r.was_notified("1")
    r.mark_notified("1")
    assert r.was_notified("1")
