import sqlite3
from datetime import UTC, datetime

from .models import Deal


class DealRepository:
    def __init__(self, path="deals.sqlite3"):
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS deals (deal_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, price TEXT, merchant TEXT, temperature INTEGER, category TEXT NOT NULL, published_at TEXT, first_seen_at TEXT NOT NULL, notified_at TEXT)"""
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS error_alerts (fingerprint TEXT PRIMARY KEY, error_type TEXT NOT NULL, component TEXT NOT NULL, message TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, last_notified_at TEXT, occurrence_count INTEGER NOT NULL)"
        )
        self.db.commit()

    def error_alert_allowed(self, error_type, component, message, cooldown_minutes=60):
        import hashlib
        from datetime import timedelta

        now = datetime.now(UTC)
        fingerprint = hashlib.sha256(f"{error_type}:{component}".encode()).hexdigest()
        row = self.db.execute(
            "SELECT last_notified_at, occurrence_count FROM error_alerts WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if (
            row
            and row[0]
            and now - datetime.fromisoformat(row[0])
            < timedelta(minutes=cooldown_minutes)
        ):
            self.db.execute(
                "UPDATE error_alerts SET last_seen_at=?, occurrence_count=occurrence_count+1 WHERE fingerprint=?",
                (now.isoformat(), fingerprint),
            )
            self.db.commit()
            return False
        if row:
            self.db.execute(
                "UPDATE error_alerts SET last_seen_at=?, last_notified_at=?, message=?, occurrence_count=occurrence_count+1 WHERE fingerprint=?",
                (now.isoformat(), now.isoformat(), message, fingerprint),
            )
        else:
            self.db.execute(
                "INSERT INTO error_alerts VALUES (?,?,?,?,?,?,?,1)",
                (
                    fingerprint,
                    error_type,
                    component,
                    message,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        self.db.commit()
        return True

    def upsert(self, deal: Deal) -> bool:
        now = datetime.now(UTC).isoformat()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO deals VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (
                deal.deal_id,
                deal.title,
                deal.url,
                str(deal.price) if deal.price is not None else None,
                deal.merchant,
                deal.temperature,
                deal.category,
                deal.published_at.isoformat() if deal.published_at else None,
                now,
            ),
        )
        self.db.commit()
        return cur.rowcount == 1

    def was_notified(self, deal_id):
        return (
            self.db.execute(
                "SELECT notified_at FROM deals WHERE deal_id=?", (deal_id,)
            ).fetchone()[0]
            is not None
        )

    def mark_notified(self, deal_id):
        self.db.execute(
            "UPDATE deals SET notified_at=? WHERE deal_id=?",
            (datetime.now(UTC).isoformat(), deal_id),
        )
        self.db.commit()

    def deal_count(self):
        return self.db.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
