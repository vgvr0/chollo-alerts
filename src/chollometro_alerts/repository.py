import json
import sqlite3
from datetime import UTC, datetime

from .intent import AlertIntent
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
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS product_extractions (deal_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,
            product_type TEXT, brand TEXT, max_price TEXT NOT NULL,
            price_unit TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(query, product_type, brand, max_price, price_unit)
            )"""
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL)"
        )
        self.db.commit()

    def list_alert_rules(self, enabled_only=False):
        sql = "SELECT id, query, product_type, brand, max_price, price_unit, enabled FROM alert_rules"
        if enabled_only:
            sql += " WHERE enabled=1"
        rows = self.db.execute(sql).fetchall()
        return rows

    def apply_alert_intent(self, intent: AlertIntent):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        if intent.action == "list":
            return self.list_alert_rules()
        query = intent.query or intent.product_type or intent.brand
        row = self.db.execute(
            "SELECT id FROM alert_rules WHERE query=? AND COALESCE(brand,'')=COALESCE(?, '')",
            (query, intent.brand),
        ).fetchone()
        if intent.action == "create":
            duplicate = self.db.execute(
                "SELECT 1 FROM alert_rules WHERE query=? AND product_type IS ? AND brand IS ? AND max_price=? AND price_unit=?",
                (
                    query,
                    intent.product_type,
                    intent.brand,
                    str(intent.max_price),
                    intent.price_unit,
                ),
            ).fetchone()
            if not duplicate:
                self.db.execute(
                    "INSERT INTO alert_rules(query,product_type,brand,max_price,price_unit,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        query,
                        intent.product_type,
                        intent.brand,
                        str(intent.max_price),
                        intent.price_unit,
                        1,
                        now,
                        now,
                    ),
                )
        elif row:
            if intent.action == "update":
                self.db.execute(
                    "UPDATE alert_rules SET max_price=?,price_unit=?,updated_at=?,enabled=1 WHERE id=?",
                    (str(intent.max_price), intent.price_unit, now, row[0]),
                )
            elif intent.action in {"delete", "disable"}:
                self.db.execute(
                    "UPDATE alert_rules SET enabled=0,updated_at=? WHERE id=?",
                    (now, row[0]),
                )
            elif intent.action == "enable":
                self.db.execute(
                    "UPDATE alert_rules SET enabled=1,updated_at=? WHERE id=?",
                    (now, row[0]),
                )
        self.db.commit()
        return self.list_alert_rules()

    def claim_telegram_update(self, update_id):
        from datetime import UTC, datetime

        cur = self.db.execute(
            "INSERT OR IGNORE INTO telegram_updates VALUES (?,?)",
            (update_id, datetime.now(UTC).isoformat()),
        )
        self.db.commit()
        return cur.rowcount == 1

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

    def exists(self, deal_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM deals WHERE deal_id=?", (deal_id,)
            ).fetchone()
            is not None
        )

    def known_deal_ids(self):
        return {r[0] for r in self.db.execute("SELECT deal_id FROM deals")}

    def get_extraction(self, deal_id):
        row = self.db.execute(
            "SELECT payload FROM product_extractions WHERE deal_id=?", (deal_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_extraction(self, deal_id, payload):
        self.db.execute(
            "INSERT OR REPLACE INTO product_extractions VALUES (?,?)",
            (deal_id, json.dumps(payload, default=str)),
        )
        self.db.commit()
