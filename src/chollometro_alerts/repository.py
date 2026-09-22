import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from .alert_rule import AlertConstraints, AlertRule
from .intent import AlertIntent
from .models import Deal

DEAL_COLUMNS = "deal_id,title,url,price,merchant,temperature,category,published_at"


class DealRepository:
    def __init__(self, path="deals.sqlite3"):
        self.path = str(path)
        self._connections = {}
        self._connections_lock = threading.Lock()

    @property
    def db(self):
        """Return the connection owned by the calling thread."""
        thread_id = threading.get_ident()
        with self._connections_lock:
            db = self._connections.get(thread_id)
            if db is None:
                db = sqlite3.connect(self.path, timeout=10.0)
                db.execute("PRAGMA busy_timeout=10000")
                db.execute("PRAGMA journal_mode=WAL")
                self._connections[thread_id] = db
                self._initialize(db)
            return db

    def _initialize(self, db):
        db.execute(
            """CREATE TABLE IF NOT EXISTS deals (deal_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, price TEXT, merchant TEXT, temperature INTEGER, category TEXT NOT NULL, published_at TEXT, first_seen_at TEXT NOT NULL, notified_at TEXT)"""
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS error_alerts (fingerprint TEXT PRIMARY KEY, error_type TEXT NOT NULL, component TEXT NOT NULL, message TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, last_notified_at TEXT, occurrence_count INTEGER NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS product_extractions (deal_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,
            product_type TEXT, brand TEXT, max_price TEXT NOT NULL,
            price_unit TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(query, product_type, brand, max_price, price_unit)
            )"""
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL)"
        )
        db.execute("""CREATE TABLE IF NOT EXISTS deal_rule_matches (
            deal_id TEXT NOT NULL, rule_id INTEGER NOT NULL,
            matched_at TEXT NOT NULL, notified_at TEXT,
            UNIQUE(deal_id, rule_id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS rule_deal_observations (
            rule_id INTEGER NOT NULL, deal_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            baseline INTEGER NOT NULL DEFAULT 0, matched INTEGER,
            rejection_reason TEXT, notified_at TEXT,
            PRIMARY KEY (rule_id, deal_id)
        )""")
        # Migrate databases created before rule states existed.
        columns = {row[1] for row in db.execute("PRAGMA table_info(alert_rules)")}
        if "state" not in columns:
            db.execute(
                "ALTER TABLE alert_rules ADD COLUMN state TEXT NOT NULL DEFAULT 'ACTIVE'"
            )
        for name, definition in (
            ("original_text", "TEXT"),
            ("structured_rule", "TEXT"),
            ("schema_version", "INTEGER"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE alert_rules ADD COLUMN {name} {definition}")
        db.execute("""CREATE TABLE IF NOT EXISTS scan_runs (
            run_id TEXT NOT NULL, query TEXT NOT NULL, rule_id INTEGER,
            started_at TEXT NOT NULL, finished_at TEXT, http_status INTEGER,
            fetched_items INTEGER, parsed_items INTEGER, relevant_items INTEGER,
            matching_items INTEGER, new_items INTEGER, notifications_sent INTEGER,
            status TEXT NOT NULL, error_type TEXT
        )""")
        db.commit()

    def close(self):
        """Close the calling thread's connection (the safe SQLite ownership boundary)."""
        self.close_current_thread()

    def close_current_thread(self):
        thread_id = threading.get_ident()
        with self._connections_lock:
            db = self._connections.pop(thread_id, None)
        if db is not None:
            db.close()

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
                    "INSERT INTO alert_rules(query,product_type,brand,max_price,price_unit,enabled,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        query,
                        intent.product_type,
                        intent.brand,
                        str(intent.max_price),
                        intent.price_unit,
                        0,
                        "INITIALIZING",
                        now,
                        now,
                    ),
                )
        elif row:
            if intent.action == "update":
                self.db.execute(
                    "UPDATE alert_rules SET max_price=?,price_unit=?,updated_at=?,enabled=1,state='ACTIVE' WHERE id=?",
                    (str(intent.max_price), intent.price_unit, now, row[0]),
                )
            elif intent.action in {"delete", "disable"}:
                if intent.action == "delete":
                    self.db.execute("DELETE FROM alert_rules WHERE id=?", (row[0],))
                else:
                    self.db.execute(
                        "UPDATE alert_rules SET enabled=0,updated_at=? WHERE id=?",
                        (now, row[0]),
                    )
            elif intent.action == "enable":
                self.db.execute(
                    "UPDATE alert_rules SET enabled=1,state='ACTIVE',updated_at=? WHERE id=?",
                    (now, row[0]),
                )
        self.db.commit()
        return self.list_alert_rules()

    def save_alert_rule(self, rule: AlertRule, original_text: str, enabled=True):
        now = datetime.now(UTC).isoformat()
        constraints = rule.constraints
        cur = self.db.execute(
            """INSERT INTO alert_rules
            (query,product_type,brand,max_price,price_unit,enabled,state,created_at,updated_at,original_text,structured_rule,schema_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rule.query,
                rule.product,
                rule.brand,
                str(constraints.max_price)
                if constraints.max_price is not None
                else "0",
                "absolute",
                int(enabled),
                "ACTIVE",
                now,
                now,
                original_text,
                rule.model_dump_json(),
                rule.schema_version,
            ),
        )
        self.db.commit()
        return cur.lastrowid

    def load_alert_rule(self, rule_id):
        row = self.db.execute(
            "SELECT structured_rule FROM alert_rules WHERE id=?", (rule_id,)
        ).fetchone()
        if not row or not row[0]:
            return None
        return AlertRule.model_validate_json(row[0])

    def rule_from_row(self, row):
        """Canonical DB boundary, with deterministic legacy fallback."""
        if row[7]:
            return AlertRule.model_validate_json(row[7])
        _, query, product, brand, maximum, unit, _, _ = row
        kwargs = {}
        if unit == "liter":
            kwargs["max_price_per_liter"] = Decimal(maximum)
        elif unit == "unit":
            kwargs["max_price_per_unit"] = Decimal(maximum)
        else:
            kwargs["max_price"] = Decimal(maximum)
        return AlertRule(
            query=query,
            product=product,
            brand=brand,
            constraints=AlertConstraints(**kwargs),
        )

    def rule_from_listing(self, row):
        """Resolve a `list_alert_rules` row through the canonical rule boundary.

        Production scans and dry-run scans share this single load path so no
        caller has to rebuild a rule from legacy columns.
        """
        structured = self.db.execute(
            "SELECT structured_rule FROM alert_rules WHERE id=?", (row[0],)
        ).fetchone()
        return self.rule_from_row(
            (*row, structured[0] if structured is not None else None)
        )

    def rule_by_id(self, rule_id):
        """Load any persisted rule (structured or legacy) through `rule_from_row`."""
        row = self.db.execute(
            "SELECT id, query, product_type, brand, max_price, price_unit, enabled FROM alert_rules WHERE id=?",
            (rule_id,),
        ).fetchone()
        if row is None:
            return None
        structured = self.db.execute(
            "SELECT structured_rule FROM alert_rules WHERE id=?", (rule_id,)
        ).fetchone()
        return self.rule_from_row(
            (*row, structured[0] if structured is not None else None)
        )

    def _related_deal_rows(self, terms):
        if not terms:
            return []
        where = " AND ".join("lower(title) LIKE ?" for _ in terms)
        params = [f"%{term}%" for term in terms]
        return self.db.execute(
            f"SELECT {DEAL_COLUMNS} FROM deals WHERE {where} "
            "ORDER BY first_seen_at DESC, deal_id DESC",
            params,
        ).fetchall()

    def historical_deals(self, terms=(), category=None):
        """Read-only lookup of stored deals reasonably related to an alert query.

        Only already persisted rows are returned: the replay never scrapes and
        never stores anything. `terms` are matched (case-insensitively) against
        the stored title, which is the local text available for old deals.
        """
        rows = list(self._related_deal_rows([t.casefold() for t in terms if t]))
        seen = {row[0] for row in rows}
        if category:
            for row in self.db.execute(
                f"SELECT {DEAL_COLUMNS} FROM deals WHERE lower(category)=? "
                "ORDER BY first_seen_at DESC, deal_id DESC",
                (category.casefold(),),
            ):
                if row[0] not in seen:
                    seen.add(row[0])
                    rows.append(row)
        return [self.deal_from_row(row) for row in rows]

    @staticmethod
    def deal_from_row(row):
        """Rebuild a `Deal` from the persisted columns (no scraping involved)."""
        (
            deal_id,
            title,
            url,
            price,
            merchant,
            temperature,
            category,
            published_at,
        ) = row
        return Deal(
            deal_id,
            title,
            url,
            Decimal(price) if price not in (None, "") else None,
            merchant,
            temperature,
            category,
            datetime.fromisoformat(published_at) if published_at else None,
            product_text=title,
        )

    def attach_alert_rule(self, rule_id, rule, original_text=None):
        self.db.execute(
            "UPDATE alert_rules SET structured_rule=?,schema_version=?,original_text=COALESCE(?,original_text) WHERE id=?",
            (rule.model_dump_json(), rule.schema_version, original_text, rule_id),
        )
        self.db.commit()

    def alert_rule_metadata(self, rule_id):
        return self.db.execute(
            "SELECT id,original_text,enabled,schema_version FROM alert_rules WHERE id=?",
            (rule_id,),
        ).fetchone()

    def structured_alert_rules(self):
        return self.db.execute(
            "SELECT id,query,product_type,brand,max_price,enabled,structured_rule FROM alert_rules WHERE structured_rule IS NOT NULL ORDER BY id"
        ).fetchall()

    def get_rule(self, rule_id):
        return self.db.execute(
            "SELECT id,query,product_type,brand,max_price,price_unit,enabled,state FROM alert_rules WHERE id=?",
            (rule_id,),
        ).fetchone()

    def set_rule_state(self, rule_id, state, enabled=None):
        if enabled is None:
            self.db.execute(
                "UPDATE alert_rules SET state=?,updated_at=? WHERE id=?",
                (state, datetime.now(UTC).isoformat(), rule_id),
            )
        else:
            self.db.execute(
                "UPDATE alert_rules SET state=?,enabled=?,updated_at=? WHERE id=?",
                (state, int(enabled), datetime.now(UTC).isoformat(), rule_id),
            )
        self.db.commit()

    def claim_rule_observation(self, rule_id, deal_id, *, baseline=False):
        now = datetime.now(UTC).isoformat()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO rule_deal_observations(rule_id,deal_id,first_seen_at,last_seen_at,baseline) VALUES (?,?,?,?,?)",
            (rule_id, deal_id, now, now, int(baseline)),
        )
        if cur.rowcount == 0:
            self.db.execute(
                "UPDATE rule_deal_observations SET last_seen_at=? WHERE rule_id=? AND deal_id=?",
                (now, rule_id, deal_id),
            )
        self.db.commit()
        return cur.rowcount == 1

    def record_rule_observation_result(
        self, rule_id, deal_id, matched, rejection_reason=None
    ):
        self.db.execute(
            "UPDATE rule_deal_observations SET matched=?,rejection_reason=? WHERE rule_id=? AND deal_id=?",
            (int(matched), rejection_reason, rule_id, deal_id),
        )
        self.db.commit()

    def mark_rule_observation_notified(self, rule_id, deal_id):
        self.db.execute(
            "UPDATE rule_deal_observations SET notified_at=? WHERE rule_id=? AND deal_id=?",
            (datetime.now(UTC).isoformat(), rule_id, deal_id),
        )
        self.db.commit()

    def rule_observations(self, rule_id):
        return self.db.execute(
            "SELECT * FROM rule_deal_observations WHERE rule_id=?", (rule_id,)
        ).fetchall()

    def get_rule_observation(self, rule_id, deal_id):
        return self.db.execute(
            "SELECT * FROM rule_deal_observations WHERE rule_id=? AND deal_id=?",
            (rule_id, deal_id),
        ).fetchone()

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

    def record_scan_run(self, query, rule_id=None, **metrics):
        run_id = uuid.uuid4().hex
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            "INSERT INTO scan_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                query,
                rule_id,
                now,
                now,
                metrics.get("http_status"),
                metrics.get("fetched_items"),
                metrics.get("parsed_items"),
                metrics.get("relevant_items"),
                metrics.get("matching_items"),
                metrics.get("new_items"),
                metrics.get("notifications_sent"),
                metrics.get("status", "completed"),
                metrics.get("error_type"),
            ),
        )
        self.db.commit()
        return run_id

    def record_rule_match(self, deal_id, rule_id):
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            "INSERT OR IGNORE INTO deal_rule_matches(deal_id,rule_id,matched_at) VALUES (?,?,?)",
            (deal_id, rule_id, now),
        )
        self.db.commit()

    def mark_rule_match_notified(self, deal_id, rule_id):
        self.db.execute(
            "UPDATE deal_rule_matches SET notified_at=? WHERE deal_id=? AND rule_id=?",
            (datetime.now(UTC).isoformat(), deal_id, rule_id),
        )
        self.db.commit()

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
