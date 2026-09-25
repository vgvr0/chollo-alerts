import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .alert_rule import AlertConstraints, AlertRule
from .intent import AlertIntent
from .models import Deal, User
from .product import product_tokens
from .retention import RetentionResult

DEAL_COLUMNS = "deal_id,title,url,price,merchant,temperature,category,published_at"

# Keys of the single-row `feed_state` table used by the GraphQL discovery feed.
FEED_BOOTSTRAP_KEY = "bootstrap_at"
FEED_WATERMARK_KEY = "newest_published_at"

# Why a matched (rule, deal) pair is still waiting for its Telegram message.
# Both states share the same durable mechanism (a match with `notified_at`
# NULL) but stay distinguishable: a hard delivery failure is not the same as a
# delivery the alert's own notification schedule is deliberately holding back.
PENDING_TELEGRAM_FAILURE = "TELEGRAM_FAILURE"
# Bump this whenever extraction semantics change.  A content fingerprint alone
# cannot invalidate facts produced by an older algorithm for the same title.
EXTRACTION_CACHE_VERSION = "product-extraction-v2"


def extraction_fingerprint(product_text: str) -> str:
    """Stable fingerprint for the text sent to the product extractor."""
    return hashlib.sha256(product_text.strip().encode("utf-8")).hexdigest()


PENDING_NOTIFICATION_SCHEDULE = "NOTIFICATION_SCHEDULE"


def as_utc(value):
    """Return a timestamp as timezone-aware UTC.

    Every writer stores ISO-8601 strings built from `datetime.now(UTC)`, but a
    database created by an older version (or by hand) may hold a naive value.
    Reading a naive timestamp as UTC is the only interpretation that keeps the
    alert window monotonic with the provider timestamps.
    """
    if value is None:
        return None
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def legacy_price_columns(rule: AlertRule) -> tuple[str, str]:
    """The `max_price` / `price_unit` columns that mirror a structured rule.

    The exact inverse of `rule_from_row`'s legacy fallback, so a row rewritten
    from a structured rule reads back as the same rule when it is old enough to
    have no structured rule at all.
    """
    constraints = rule.constraints
    for value, unit in (
        (constraints.max_price, "absolute"),
        (constraints.max_price_per_unit, "unit"),
        (constraints.max_price_per_liter, "liter"),
    ):
        if value is not None:
            return str(value), unit
    return "0", "absolute"


class DealRepository:
    def __init__(self, path="deals.sqlite3"):
        self.path = str(path)
        self._connections = {}
        self._connections_lock = threading.Lock()
        self._initialized = False

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
                if not self._initialized:
                    self._initialize(db)
                    self._initialized = True
            return db

    def _initialize(self, db):
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id TEXT UNIQUE,
            telegram_chat_id TEXT,
            username TEXT,
            first_name TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        db.execute(
            """CREATE TABLE IF NOT EXISTS deals (deal_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, price TEXT, merchant TEXT, temperature INTEGER, category TEXT NOT NULL, published_at TEXT, first_seen_at TEXT NOT NULL, notified_at TEXT)"""
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS error_alerts (fingerprint TEXT PRIMARY KEY, error_type TEXT NOT NULL, component TEXT NOT NULL, message TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, last_notified_at TEXT, occurrence_count INTEGER NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS product_extractions (deal_id TEXT PRIMARY KEY, payload TEXT NOT NULL, content_fingerprint TEXT, extractor_version TEXT)"
        )
        extraction_columns = {
            row[1] for row in db.execute("PRAGMA table_info(product_extractions)")
        }
        for name in ("content_fingerprint", "extractor_version"):
            if name not in extraction_columns:
                db.execute(f"ALTER TABLE product_extractions ADD COLUMN {name} TEXT")
        db.execute(
            """CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT,
            product_type TEXT, brand TEXT, max_price TEXT NOT NULL,
            price_unit TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id)
            )"""
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL)"
        )
        # What the bot showed or created last, per chat: it is what resolves a
        # later "quita esa alerta" when the sentence points instead of naming.
        db.execute(
            """CREATE TABLE IF NOT EXISTS alert_context (
            chat_id TEXT PRIMARY KEY, rule_ids TEXT NOT NULL,
            updated_at TEXT NOT NULL
            )"""
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
            rejection_reason TEXT, notified_at TEXT, evidence TEXT,
            pending_reason TEXT,
            PRIMARY KEY (rule_id, deal_id)
        )""")
        # Migrate databases created before the match evidence was stored: the
        # durable evidence is what lets a retried notification explain the
        # original match again, after the deal left the provider window.
        observation_columns = {
            row[1] for row in db.execute("PRAGMA table_info(rule_deal_observations)")
        }
        if "evidence" not in observation_columns:
            db.execute("ALTER TABLE rule_deal_observations ADD COLUMN evidence TEXT")
        # ... and before the notification could be pending for a reason other
        # than a Telegram failure (the alert's notification schedule).
        if "pending_reason" not in observation_columns:
            db.execute(
                "ALTER TABLE rule_deal_observations ADD COLUMN pending_reason TEXT"
            )
        # Migrate databases created before rule states existed. Generic hot
        # deal rules use NULL query, so old NOT NULL tables are rebuilt while
        # copying every legacy row unchanged.
        columns = {row[1] for row in db.execute("PRAGMA table_info(alert_rules)")}
        query_not_null = next(
            row[3]
            for row in db.execute("PRAGMA table_info(alert_rules)")
            if row[1] == "query"
        )
        if query_not_null:
            if "state" not in columns:
                db.execute(
                    "ALTER TABLE alert_rules ADD COLUMN state TEXT NOT NULL DEFAULT 'ACTIVE'"
                )
            db.execute("ALTER TABLE alert_rules RENAME TO alert_rules_legacy")
            db.execute("""CREATE TABLE alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT,
                product_type TEXT, brand TEXT, max_price TEXT NOT NULL,
                price_unit TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(query, product_type, brand, max_price, price_unit))""")
            db.execute("""INSERT INTO alert_rules
                (id,query,product_type,brand,max_price,price_unit,enabled,state,created_at,updated_at)
                SELECT id,query,product_type,brand,max_price,price_unit,enabled,state,created_at,updated_at
                FROM alert_rules_legacy""")
            db.execute("DROP TABLE alert_rules_legacy")
            columns = {row[1] for row in db.execute("PRAGMA table_info(alert_rules)")}
        if "user_id" not in columns:
            db.execute(
                "ALTER TABLE alert_rules ADD COLUMN user_id INTEGER REFERENCES users(id)"
            )
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
        # One row per thread ever seen in the GraphQL feed: the existence of the
        # row is the "seen" state behind the "only new deals" guarantee.
        # `first_seen_at` is the discovery time, `published_at` the provider
        # timestamp of the thread (what the alert window compares against).
        db.execute("""CREATE TABLE IF NOT EXISTS feed_threads (
            thread_id TEXT PRIMARY KEY, published_at TEXT,
            first_seen_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS feed_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS runtime_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            created_at TEXT NOT NULL,
            daemon_started_at TEXT,
            daemon_heartbeat_at TEXT,
            last_scan_run_id TEXT,
            last_scan_started_at TEXT,
            last_scan_finished_at TEXT,
            last_scan_completed_at TEXT,
            last_scan_failed_at TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error_type TEXT,
            last_telegram_activity_at TEXT,
            last_telegram_error_at TEXT,
            telegram_consecutive_failures INTEGER NOT NULL DEFAULT 0
        )""")
        runtime_columns = {
            row[1] for row in db.execute("PRAGMA table_info(runtime_status)")
        }
        for name, definition in (
            ("last_retention_started_at", "TEXT"),
            ("last_retention_finished_at", "TEXT"),
            ("last_retention_status", "TEXT"),
            ("last_retention_error_type", "TEXT"),
        ):
            if name not in runtime_columns:
                db.execute(f"ALTER TABLE runtime_status ADD COLUMN {name} {definition}")
        db.execute("""CREATE TABLE IF NOT EXISTS deal_temperature_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
            temperature REAL NOT NULL, observed_at TEXT NOT NULL,
            UNIQUE(thread_id, temperature, observed_at))""")
        db.execute("""CREATE TABLE IF NOT EXISTS temperature_momentum_state (
            thread_id TEXT PRIMARY KEY, above_threshold INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL)""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_temperature_snapshots_thread_time ON deal_temperature_snapshots(thread_id, observed_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_temperature_snapshots_observed_at ON deal_temperature_snapshots(observed_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_runs_finished_at ON scan_runs(finished_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_error_alerts_last_seen_at ON error_alerts(last_seen_at)"
        )
        self._migrate_alert_ownership(db)
        db.commit()

    def _migrate_alert_ownership(self, db):
        legacy_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None
        legacy_user = os.getenv("TELEGRAM_USER_ID", "").strip() or None
        row = None
        if legacy_user:
            row = db.execute(
                "SELECT id FROM users WHERE telegram_user_id=?", (legacy_user,)
            ).fetchone()
        if row is None and legacy_chat:
            row = db.execute(
                "SELECT id FROM users WHERE telegram_chat_id=?", (legacy_chat,)
            ).fetchone()
        has_rules = (
            db.execute("SELECT 1 FROM alert_rules LIMIT 1").fetchone() is not None
        )
        # Without a configured destination there is no safe identity to invent.
        # Such rows remain legacy/unowned until the operator configures Telegram.
        if row is None and (legacy_chat or legacy_user) and (legacy_chat or has_rules):
            now = datetime.now(UTC).isoformat()
            row = (
                db.execute(
                    "INSERT INTO users(telegram_user_id,telegram_chat_id,username,enabled,created_at,updated_at) VALUES (?,?,?,1,?,?)",
                    (legacy_user, legacy_chat, "legacy/default", now, now),
                ).lastrowid,
            )
        if row:
            db.execute(
                "UPDATE alert_rules SET user_id=? WHERE user_id IS NULL", (row[0],)
            )
        schema = (
            db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_rules'"
            ).fetchone()
            or ("",)
        )[0] or ""
        # Older schemas used a table-level UNIQUE constraint that ignored
        # ownership. Rebuild only that table, copying and validating every
        # alert row inside a savepoint; all match/observation/feed state lives
        # in separate tables and is therefore retained untouched.
        global_unique = "UNIQUE(query" in schema or "UNIQUE (query" in schema
        if global_unique:
            db.execute("SAVEPOINT alert_ownership_migration")
            try:
                before = db.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0]
                db.execute("ALTER TABLE alert_rules RENAME TO alert_rules_legacy")
                db.execute("""CREATE TABLE alert_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT,
                    product_type TEXT, brand TEXT, max_price TEXT NOT NULL,
                    price_unit TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, original_text TEXT, structured_rule TEXT,
                    schema_version INTEGER, user_id INTEGER REFERENCES users(id)
                )""")
                db.execute("""INSERT INTO alert_rules
                    (id,query,product_type,brand,max_price,price_unit,enabled,state,
                     created_at,updated_at,original_text,structured_rule,schema_version,user_id)
                    SELECT id,query,product_type,brand,max_price,price_unit,enabled,state,
                     created_at,updated_at,original_text,structured_rule,schema_version,user_id
                    FROM alert_rules_legacy""")
                after = db.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0]
                if before != after:
                    raise RuntimeError(
                        "alert ownership migration did not preserve all rules"
                    )
                db.execute("DROP TABLE alert_rules_legacy")
                db.execute("RELEASE SAVEPOINT alert_ownership_migration")
            except Exception:
                db.execute("ROLLBACK TO SAVEPOINT alert_ownership_migration")
                db.execute("RELEASE SAVEPOINT alert_ownership_migration")
                raise
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_rules_owner_identity ON alert_rules(user_id,query,product_type,brand,max_price,price_unit)"
        )

    def user_for_id(self, user_id):
        row = self.db.execute(
            "SELECT id,telegram_user_id,telegram_chat_id,username,first_name,enabled,created_at,updated_at FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        return (
            User(
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                bool(row[5]),
                as_utc(row[6]),
                as_utc(row[7]),
            )
            if row
            else None
        )

    def user_for_telegram_id(self, telegram_user_id):
        return self.db.execute(
            "SELECT id,telegram_user_id,telegram_chat_id,username,first_name,enabled,created_at,updated_at FROM users WHERE telegram_user_id=?",
            (str(telegram_user_id),),
        ).fetchone()

    def user_for_chat(self, chat_id):
        row = self.db.execute(
            "SELECT id FROM users WHERE telegram_chat_id=?", (str(chat_id),)
        ).fetchone()
        return self.user_for_id(row[0]) if row else None

    def legacy_user(self):
        row = self.db.execute(
            "SELECT id FROM users WHERE username='legacy/default' ORDER BY id LIMIT 1"
        ).fetchone()
        return self.user_for_id(row[0]) if row else None

    def create_user(
        self,
        *,
        telegram_user_id=None,
        telegram_chat_id=None,
        username=None,
        first_name=None,
        enabled=True,
    ):
        now = datetime.now(UTC).isoformat()
        cur = self.db.execute(
            "INSERT INTO users(telegram_user_id,telegram_chat_id,username,first_name,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (
                None if telegram_user_id is None else str(telegram_user_id),
                None if telegram_chat_id is None else str(telegram_chat_id),
                username,
                first_name,
                int(enabled),
                now,
                now,
            ),
        )
        self.db.commit()
        return self.user_for_id(cur.lastrowid)

    def update_user_metadata(self, user_id, identity):
        self.db.execute(
            "UPDATE users SET telegram_chat_id=?,username=?,first_name=?,updated_at=? WHERE id=?",
            (
                identity.telegram_chat_id,
                identity.username,
                identity.first_name,
                datetime.now(UTC).isoformat(),
                user_id,
            ),
        )
        self.db.commit()

    def list_users(self):
        return self.db.execute(
            "SELECT u.id,u.telegram_user_id,u.telegram_chat_id,u.enabled,COUNT(r.id) FROM users u LEFT JOIN alert_rules r ON r.user_id=u.id GROUP BY u.id ORDER BY u.id"
        ).fetchall()

    def notification_chat_id(self, rule_id):
        row = self.db.execute(
            "SELECT u.telegram_chat_id FROM alert_rules r LEFT JOIN users u ON u.id=r.user_id WHERE r.id=?",
            (rule_id,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def close(self):
        """Close the calling thread's connection (the safe SQLite ownership boundary)."""
        self.close_current_thread()

    def close_current_thread(self):
        thread_id = threading.get_ident()
        with self._connections_lock:
            db = self._connections.pop(thread_id, None)
        if db is not None:
            db.close()

    def record_temperature_snapshot(self, thread_id, temperature, observed_at=None):
        stamp = as_utc(observed_at or datetime.now(UTC)).isoformat()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO deal_temperature_snapshots(thread_id,temperature,observed_at) VALUES (?,?,?)",
            (str(thread_id), float(temperature), stamp),
        )
        self.db.commit()
        return cur.rowcount == 1

    def temperature_snapshots(self, thread_id, since=None):
        query = "SELECT thread_id,temperature,observed_at FROM deal_temperature_snapshots WHERE thread_id=?"
        params = [str(thread_id)]
        if since is not None:
            query += " AND observed_at>=?"
            params.append(as_utc(since).isoformat())
        query += " ORDER BY observed_at"
        from .temperature_momentum import TemperatureSnapshot

        return [
            TemperatureSnapshot(row[0], row[1], as_utc(row[2]))
            for row in self.db.execute(query, params)
        ]

    def reset_old_temperature_snapshots(self, before=None):
        before = as_utc(before or datetime.now(UTC))
        cur = self.db.execute(
            "DELETE FROM deal_temperature_snapshots WHERE observed_at < ?",
            (before.isoformat(),),
        )
        self.db.commit()
        return cur.rowcount

    def retention_last_finished_at(self):
        value = self._runtime_row().get("last_retention_finished_at")
        return as_utc(value)

    def retention_started(self, at=None):
        stamp = as_utc(at or datetime.now(UTC)).isoformat()
        self._runtime_row()
        self.db.execute(
            "UPDATE runtime_status SET last_retention_started_at=?, last_retention_status=? WHERE id=1",
            (stamp, "RUNNING"),
        )
        self.db.commit()

    def retention_finished(self, at=None, status="SUCCESS", error_type=None):
        stamp = as_utc(at or datetime.now(UTC)).isoformat()
        self._runtime_row()
        self.db.execute(
            "UPDATE runtime_status SET last_retention_finished_at=?, last_retention_status=?, last_retention_error_type=? WHERE id=1",
            (stamp, status, error_type),
        )
        self.db.commit()

    def retention_counts(self, settings, *, now=None):
        now = as_utc(now or datetime.now(UTC))
        cutoffs = self._retention_cutoffs(settings, now)
        return {
            "snapshots": self._eligible_count(
                "SELECT COUNT(*) FROM deal_temperature_snapshots WHERE observed_at < ?",
                (cutoffs["snapshots"],),
            ),
            "cache_entries": self._eligible_count(
                """SELECT COUNT(*) FROM product_extractions p
                   JOIN deals d ON d.deal_id=p.deal_id
                   WHERE d.first_seen_at < ?""",
                (cutoffs["cache"],),
            ),
            "error_history": self._eligible_count(
                "SELECT COUNT(*) FROM error_alerts WHERE last_seen_at < ?",
                (cutoffs["errors"],),
            ),
            "scan_runs": self._eligible_count(
                "SELECT COUNT(*) FROM scan_runs WHERE finished_at IS NOT NULL AND finished_at < ?",
                (cutoffs["scan_runs"],),
            ),
        }

    def _eligible_count(self, sql, params):
        row = self.db.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _retention_cutoffs(settings, now):
        return {
            "snapshots": (now - timedelta(hours=settings.snapshots_hours)).isoformat(),
            "cache": (now - timedelta(days=settings.llm_cache_days)).isoformat(),
            "errors": (now - timedelta(days=settings.error_history_days)).isoformat(),
            "scan_runs": (now - timedelta(days=settings.scan_history_days)).isoformat(),
        }

    def prune_retention(self, settings, *, now=None, dry_run=False):
        now = as_utc(now or datetime.now(UTC))
        cutoffs = self._retention_cutoffs(settings, now)
        if dry_run:
            counts = self.retention_counts(settings, now=now)
            return RetentionResult(
                dry_run=True,
                deleted_snapshots=counts["snapshots"],
                deleted_cache_entries=counts["cache_entries"],
                deleted_error_history=counts["error_history"],
                deleted_scan_runs=counts["scan_runs"],
            )

        total_batches = 0
        deleted = {}
        operations = (
            (
                "snapshots",
                """DELETE FROM deal_temperature_snapshots WHERE rowid IN
             (SELECT rowid FROM deal_temperature_snapshots WHERE observed_at < ? LIMIT ?)""",
                cutoffs["snapshots"],
            ),
            (
                "cache_entries",
                """DELETE FROM product_extractions WHERE rowid IN
             (SELECT p.rowid FROM product_extractions p JOIN deals d ON d.deal_id=p.deal_id
              WHERE d.first_seen_at < ? LIMIT ?)""",
                cutoffs["cache"],
            ),
            (
                "error_history",
                """DELETE FROM error_alerts WHERE rowid IN
             (SELECT rowid FROM error_alerts WHERE last_seen_at < ? LIMIT ?)""",
                cutoffs["errors"],
            ),
            (
                "scan_runs",
                """DELETE FROM scan_runs WHERE rowid IN
             (SELECT rowid FROM scan_runs WHERE finished_at IS NOT NULL AND finished_at < ? LIMIT ?)""",
                cutoffs["scan_runs"],
            ),
        )
        for name, sql, cutoff in operations:
            # One batch per category keeps an automatic run bounded. A later
            # run continues from the same cutoff, so cleanup remains idempotent.
            with self.db:
                cur = self.db.execute(sql, (cutoff, settings.batch_size))
            count = max(cur.rowcount, 0)
            total_batches += int(count > 0)
            deleted[name] = count
        return RetentionResult(
            dry_run=False,
            deleted_snapshots=deleted.get("snapshots", 0),
            deleted_cache_entries=deleted.get("cache_entries", 0),
            deleted_error_history=deleted.get("error_history", 0),
            deleted_scan_runs=deleted.get("scan_runs", 0),
            batches=total_batches,
        )

    def vacuum(self):
        """Explicit physical compaction; never part of automatic retention."""
        self.db.commit()
        self.db.execute("VACUUM")
        self.db.commit()

    def temperature_momentum_above(self, thread_id):
        row = self.db.execute(
            "SELECT above_threshold FROM temperature_momentum_state WHERE thread_id=?",
            (str(thread_id),),
        ).fetchone()
        return bool(row[0]) if row else False

    def set_temperature_momentum_above(self, thread_id, above):
        self.db.execute(
            "INSERT INTO temperature_momentum_state(thread_id,above_threshold,updated_at) VALUES (?,?,?) ON CONFLICT(thread_id) DO UPDATE SET above_threshold=excluded.above_threshold,updated_at=excluded.updated_at",
            (str(thread_id), int(above), datetime.now(UTC).isoformat()),
        )
        self.db.commit()

    def list_alert_rules(self, enabled_only=False, user_id=None):
        sql = "SELECT id, query, product_type, brand, max_price, price_unit, enabled FROM alert_rules"
        if enabled_only:
            sql += " WHERE enabled=1"
        if user_id is not None:
            sql += " AND user_id=?" if enabled_only else " WHERE user_id=?"
            return self.db.execute(sql, (user_id,)).fetchall()
        rows = self.db.execute(sql).fetchall()
        return rows

    def apply_alert_intent(self, intent: AlertIntent, user_id=None):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        if intent.action == "list":
            return self.list_alert_rules(user_id=user_id)
        query = intent.query or intent.product_type or intent.brand
        scope = " AND user_id=?" if user_id is not None else ""
        row = self.db.execute(
            f"SELECT id FROM alert_rules WHERE query IS ? AND COALESCE(brand,'')=COALESCE(?, ''){scope}",
            (query, intent.brand, user_id)
            if user_id is not None
            else (query, intent.brand),
        ).fetchone()
        # A temperature-only alert has no price at all. The legacy column is
        # NOT NULL and `save_alert_rule` already writes "0" for every alert
        # without an absolute price, so the same placeholder is used here: the
        # structured rule is what says the alert has no price (and no reader
        # ever sees the text "None").
        legacy_price = str(intent.max_price) if intent.max_price is not None else "0"
        # `price_unit` is NOT NULL as well, and its default dimension is the
        # total price of the deal: an alert that only filters by temperature
        # says nothing about a per-unit price.
        legacy_unit = intent.price_unit or "absolute"
        if intent.action == "create":
            duplicate = self.db.execute(
                f"SELECT 1 FROM alert_rules WHERE query IS ? AND product_type IS ? AND brand IS ? AND max_price=? AND price_unit=?{scope}",
                (
                    query,
                    intent.product_type,
                    intent.brand,
                    legacy_price,
                    legacy_unit,
                    user_id,
                )
                if user_id is not None
                else (
                    query,
                    intent.product_type,
                    intent.brand,
                    legacy_price,
                    legacy_unit,
                ),
            ).fetchone()
            if not duplicate:
                self.db.execute(
                    "INSERT INTO alert_rules(query,product_type,brand,max_price,price_unit,enabled,state,created_at,updated_at,user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        query,
                        intent.product_type,
                        intent.brand,
                        legacy_price,
                        legacy_unit,
                        0,
                        "INITIALIZING",
                        now,
                        now,
                        user_id,
                    ),
                )
        elif row:
            if intent.action == "update":
                update_scope = " AND user_id=?" if user_id is not None else ""
                self.db.execute(
                    "UPDATE alert_rules SET max_price=?,price_unit=?,updated_at=?,enabled=1,state='ACTIVE' WHERE id=?"
                    + update_scope,
                    (legacy_price, legacy_unit, now, row[0], user_id)
                    if user_id is not None
                    else (legacy_price, legacy_unit, now, row[0]),
                )
            elif intent.action in {"delete", "disable"}:
                if intent.action == "delete":
                    self.delete_alert_rule(row[0], user_id=user_id)
                else:
                    self.db.execute(
                        "UPDATE alert_rules SET enabled=0,updated_at=? WHERE id=?"
                        + (" AND user_id=?" if user_id is not None else ""),
                        (now, row[0], user_id)
                        if user_id is not None
                        else (now, row[0]),
                    )
            elif intent.action == "enable":
                self.db.execute(
                    "UPDATE alert_rules SET enabled=1,state='ACTIVE',updated_at=? WHERE id=?"
                    + (" AND user_id=?" if user_id is not None else ""),
                    (now, row[0], user_id) if user_id is not None else (now, row[0]),
                )
        self.db.commit()
        return self.list_alert_rules(user_id=user_id)

    def delete_alert_rule(self, rule_id, user_id=None) -> bool:
        """Remove one stored alert by its id (the reference was resolved already).

        The row keeps its identity until this point, so the deletion is of the
        alert the operator described, not of a text that happened to be equal.
        """
        sql = "DELETE FROM alert_rules WHERE id=?" + (
            " AND user_id=?" if user_id is not None else ""
        )
        cur = self.db.execute(
            sql, (rule_id, user_id) if user_id is not None else (rule_id,)
        )
        self.db.commit()
        return cur.rowcount == 1

    def replace_alert_rule(
        self, rule_id, rule: AlertRule, original_text=None, user_id=None
    ) -> bool:
        """Rewrite one stored alert in place with the rule of an update.

        The row keeps its id, its creation time and its matches: an update
        changes the alert, it never creates a second one. The legacy columns
        and the structured rule are written together so both readers stay in
        sync.
        """
        now = datetime.now(UTC).isoformat()
        price, unit = legacy_price_columns(rule)
        try:
            cur = self.db.execute(
                """UPDATE alert_rules SET query=?,product_type=?,brand=?,max_price=?,
                price_unit=?,updated_at=?,original_text=COALESCE(?,original_text),
                structured_rule=?,schema_version=? WHERE id=?"""
                + (" AND user_id=?" if user_id is not None else ""),
                (
                    rule.query,
                    rule.product,
                    rule.brand,
                    price,
                    unit,
                    now,
                    original_text,
                    rule.model_dump_json(),
                    rule.schema_version,
                    rule_id,
                    user_id,
                )
                if user_id is not None
                else (
                    rule.query,
                    rule.product,
                    rule.brand,
                    price,
                    unit,
                    now,
                    original_text,
                    rule.model_dump_json(),
                    rule.schema_version,
                    rule_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # `(query, product_type, brand, max_price, price_unit)` is unique:
            # the update would produce an alert that already exists.
            raise ValueError(
                "ya tienes otra alerta con esas mismas condiciones"
            ) from exc
        self.db.commit()
        return cur.rowcount == 1

    def save_alert_rule(
        self, rule: AlertRule, original_text: str, enabled=True, user_id=None
    ):
        now = datetime.now(UTC).isoformat()
        constraints = rule.constraints
        cur = self.db.execute(
            """INSERT INTO alert_rules
            (query,product_type,brand,max_price,price_unit,enabled,state,created_at,updated_at,original_text,structured_rule,schema_version,user_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                user_id,
            ),
        )
        self.db.commit()
        return cur.lastrowid

    def load_alert_rule(self, rule_id, user_id=None):
        row = self.db.execute(
            "SELECT structured_rule FROM alert_rules WHERE id=?"
            + (" AND user_id=?" if user_id is not None else ""),
            (rule_id, user_id) if user_id is not None else (rule_id,),
        ).fetchone()
        if not row or not row[0]:
            return None
        return AlertRule.model_validate_json(row[0])

    def rule_from_row(self, row):
        """Canonical DB boundary, with deterministic legacy fallback."""
        if row[7]:
            return AlertRule.model_validate_json(row[7])
        _, query, product, brand, maximum, unit, _, _ = row
        # Pre-structured rows have no separate product column. Preserve their
        # positive query relevance when it is an unambiguous one-word subject;
        # generic shopping queries remain intentionally product-less.
        if product is None and brand is None and query:
            tokens = product_tokens(query)
            if len(tokens) == 1 and tokens[0] not in {"oferta", "ofertas"}:
                product = query
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

    def rule_by_id(self, rule_id, user_id=None):
        """Load any persisted rule (structured or legacy) through `rule_from_row`."""
        row = self.db.execute(
            "SELECT id, query, product_type, brand, max_price, price_unit, enabled FROM alert_rules WHERE id=?"
            + (" AND user_id=?" if user_id is not None else ""),
            (rule_id, user_id) if user_id is not None else (rule_id,),
        ).fetchone()
        if row is None:
            return None
        structured = self.db.execute(
            "SELECT structured_rule FROM alert_rules WHERE id=?"
            + (" AND user_id=?" if user_id is not None else ""),
            (rule_id, user_id) if user_id is not None else (rule_id,),
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
        normalized_terms = [t.casefold() for t in terms if t]
        rows = list(self._related_deal_rows(normalized_terms))
        if not normalized_terms and not category:
            rows = list(
                self.db.execute(
                    f"SELECT {DEAL_COLUMNS} FROM deals ORDER BY first_seen_at DESC, deal_id DESC"
                )
            )
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

    def attach_alert_rule(self, rule_id, rule, original_text=None, user_id=None):
        self.db.execute(
            "UPDATE alert_rules SET structured_rule=?,schema_version=?,original_text=COALESCE(?,original_text) WHERE id=?"
            + (" AND user_id=?" if user_id is not None else ""),
            (
                rule.model_dump_json(),
                rule.schema_version,
                original_text,
                rule_id,
                user_id,
            )
            if user_id is not None
            else (rule.model_dump_json(), rule.schema_version, original_text, rule_id),
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

    def get_rule(self, rule_id, user_id=None):
        return self.db.execute(
            "SELECT id,query,product_type,brand,max_price,price_unit,enabled,state FROM alert_rules WHERE id=?"
            + (" AND user_id=?" if user_id is not None else ""),
            (rule_id, user_id) if user_id is not None else (rule_id,),
        ).fetchone()

    def alert_rule_created_at(self, rule_id):
        """Real creation timestamp of a rule: the lower bound of its alerts."""
        row = self.db.execute(
            "SELECT created_at FROM alert_rules WHERE id=?", (rule_id,)
        ).fetchone()
        return as_utc(row[0]) if row and row[0] else None

    def alert_rule_original_text(self, rule_id):
        """The text the operator wrote for one alert, when it is stored.

        It is the alert's own name, so the notification can quote the alert
        that matched instead of inferring anything from the deal.
        """
        row = self.db.execute(
            "SELECT original_text FROM alert_rules WHERE id=?", (rule_id,)
        ).fetchone()
        return row[0] if row and row[0] else None

    def feed_state(self, key):
        row = self.db.execute(
            "SELECT value FROM feed_state WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_feed_state(self, key, value):
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """INSERT INTO feed_state(key,value,updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
            updated_at=excluded.updated_at""",
            (key, value, now),
        )
        self.db.commit()

    def feed_is_initialized(self):
        """False until the first discovery cycle snapshotted the current feed."""
        return self.feed_state(FEED_BOOTSTRAP_KEY) is not None

    def mark_feed_initialized(self, at=None):
        self.set_feed_state(
            FEED_BOOTSTRAP_KEY, as_utc(at or datetime.now(UTC)).isoformat()
        )

    def seen_feed_thread_ids(self, thread_ids=None):
        """Thread ids already present in the feed store (all, or the given ones)."""
        if thread_ids is None:
            rows = self.db.execute("SELECT thread_id FROM feed_threads")
            return {row[0] for row in rows}
        wanted = [str(thread_id) for thread_id in thread_ids]
        if not wanted:
            return set()
        seen = set()
        # Chunked because SQLite has a limit on bound parameters, and the feed
        # window is small but the store grows over time.
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"SELECT thread_id FROM feed_threads WHERE thread_id IN ({placeholders})",
                chunk,
            )
            seen.update(row[0] for row in rows)
        return seen

    def feed_thread_count(self):
        """How many distinct threads the feed store already knows about.

        The discovery cycle uses it as the guard of the "no overlap" risk
        signal: a window that overlaps nothing only means something when there
        is a history to overlap with.
        """
        row = self.db.execute("SELECT COUNT(*) FROM feed_threads").fetchone()
        return row[0] if row else 0

    def record_feed_threads(self, deals, *, at=None):
        """Register feed threads as seen. Bulk, idempotent, never re-timestamps."""
        now = as_utc(at or datetime.now(UTC)).isoformat()
        rows = [
            (
                deal.deal_id,
                deal.published_at.isoformat() if deal.published_at else None,
                now,
            )
            for deal in deals
            if deal.deal_id
        ]
        if not rows:
            return 0
        cur = self.db.executemany(
            """INSERT OR IGNORE INTO feed_threads
            (thread_id,published_at,first_seen_at) VALUES (?,?,?)""",
            rows,
        )
        self.db.commit()
        return cur.rowcount

    def feed_watermark(self):
        """Newest `published_at` of the previous cycle, or None."""
        return as_utc(self.feed_state(FEED_WATERMARK_KEY))

    def set_feed_watermark(self, published_at):
        if published_at is None:
            return
        self.set_feed_state(FEED_WATERMARK_KEY, as_utc(published_at).isoformat())

    def pending_rule_notifications(self, limit=50):
        """Matched (rule, deal) pairs awaiting Telegram, oldest first.

        These are the rows a Telegram failure leaves behind: the match is
        already durable, the notification is not, so the next cycle can retry
        them even when the deal has already left the provider window.
        """
        return [
            (rule_id, deal_id)
            for rule_id, deal_id, _reason in self.pending_notification_rows(limit)
        ]

    def pending_notification_rows(self, limit=50):
        """The same pending pairs, with why each one is still pending.

        `pending_reason` is `TELEGRAM_FAILURE` when a delivery really failed and
        `NOTIFICATION_SCHEDULE` when the alert's own window is holding the
        match back. Both stay pending until a delivery succeeds, which is what
        keeps a chollo found at 03:00 from being lost.
        """
        rows = self.db.execute(
            """SELECT o.rule_id, o.deal_id, o.pending_reason
            FROM rule_deal_observations o
            JOIN alert_rules r ON r.id = o.rule_id
            WHERE r.enabled = 1 AND o.matched = 1 AND o.notified_at IS NULL
            ORDER BY o.first_seen_at, o.deal_id LIMIT ?""",
            (limit,),
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def get_deal(self, deal_id):
        """Rebuild a persisted deal (used to re-render a pending notification)."""
        row = self.db.execute(
            f"SELECT {DEAL_COLUMNS} FROM deals WHERE deal_id=?", (deal_id,)
        ).fetchone()
        return self.deal_from_row(row) if row else None

    def set_rule_state(self, rule_id, state, enabled=None, user_id=None):
        scope = " AND user_id=?" if user_id is not None else ""
        if enabled is None:
            self.db.execute(
                "UPDATE alert_rules SET state=?,updated_at=? WHERE id=?" + scope,
                (state, datetime.now(UTC).isoformat(), rule_id, user_id)
                if user_id is not None
                else (state, datetime.now(UTC).isoformat(), rule_id),
            )
        else:
            self.db.execute(
                "UPDATE alert_rules SET state=?,enabled=?,updated_at=? WHERE id=?"
                + scope,
                (
                    state,
                    int(enabled),
                    datetime.now(UTC).isoformat(),
                    rule_id,
                    user_id,
                )
                if user_id is not None
                else (state, int(enabled), datetime.now(UTC).isoformat(), rule_id),
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
        self, rule_id, deal_id, matched, rejection_reason=None, *, evidence=None
    ):
        # A row that carries a verdict is not a baseline snapshot any more, so
        # the flag is cleared here: baseline rows have no verdict by definition.
        stored = json.dumps(evidence, default=str) if evidence is not None else None
        self.db.execute(
            "UPDATE rule_deal_observations SET matched=?,baseline=0,rejection_reason=?,"
            "evidence=?,pending_reason=NULL WHERE rule_id=? AND deal_id=?",
            (int(matched), rejection_reason, stored, rule_id, deal_id),
        )
        self.db.commit()

    def rule_observation_evidence(self, rule_id, deal_id):
        """The evidence stored with the verdict of one (rule, deal) pair."""
        row = self.db.execute(
            "SELECT evidence FROM rule_deal_observations WHERE rule_id=? AND deal_id=?",
            (rule_id, deal_id),
        ).fetchone()
        if row is None or not row[0]:
            return None
        try:
            payload = json.loads(row[0])
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def mark_rule_observation_notified(self, rule_id, deal_id):
        """The delivery succeeded: the pair is neither pending nor retried."""
        self.db.execute(
            "UPDATE rule_deal_observations SET notified_at=?,pending_reason=NULL "
            "WHERE rule_id=? AND deal_id=?",
            (datetime.now(UTC).isoformat(), rule_id, deal_id),
        )
        self.db.commit()

    def mark_rule_observation_pending(self, rule_id, deal_id, reason):
        """Record why a durable match is still waiting for Telegram."""
        self.db.execute(
            "UPDATE rule_deal_observations SET pending_reason=? "
            "WHERE rule_id=? AND deal_id=?",
            (reason, rule_id, deal_id),
        )
        self.db.commit()

    def rule_observation_pending_reason(self, rule_id, deal_id):
        row = self.db.execute(
            "SELECT pending_reason FROM rule_deal_observations "
            "WHERE rule_id=? AND deal_id=?",
            (rule_id, deal_id),
        ).fetchone()
        return row[0] if row else None

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

    def release_telegram_update(self, update_id):
        """Allow a failed update to be delivered again on the next poll."""
        self.db.execute("DELETE FROM telegram_updates WHERE update_id=?", (update_id,))
        self.db.commit()

    def set_alert_context(self, chat_id, rule_ids):
        """Remember the alert(s) the bot showed or created last for one chat."""
        now = datetime.now(UTC).isoformat()
        ids = json.dumps([int(rule_id) for rule_id in rule_ids])
        self.db.execute(
            """INSERT INTO alert_context(chat_id,rule_ids,updated_at) VALUES (?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET rule_ids=excluded.rule_ids,
            updated_at=excluded.updated_at""",
            (str(chat_id), ids, now),
        )
        self.db.commit()

    def get_alert_context(self, chat_id):
        """The rule ids the bot showed or created last, oldest first."""
        row = self.db.execute(
            "SELECT rule_ids FROM alert_context WHERE chat_id=?", (str(chat_id),)
        ).fetchone()
        if row is None:
            return []
        try:
            values = json.loads(row[0])
        except ValueError:
            return []
        if not isinstance(values, list):
            return []
        stored = []
        for value in values:
            try:
                stored.append(int(value))
            except (TypeError, ValueError):
                continue
        return stored

    def clear_alert_context(self, chat_id):
        self.db.execute("DELETE FROM alert_context WHERE chat_id=?", (str(chat_id),))
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

    def record_scan_run(self, query, rule_id=None, run_id=None, **metrics):
        run_id = run_id or uuid.uuid4().hex
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

    def _runtime_row(self):
        row = self.db.execute("SELECT * FROM runtime_status WHERE id=1").fetchone()
        if row is None:
            now = datetime.now(UTC).isoformat()
            self.db.execute(
                "INSERT INTO runtime_status(id, created_at) VALUES (1, ?)",
                (now,),
            )
            self.db.commit()
            row = self.db.execute("SELECT * FROM runtime_status WHERE id=1").fetchone()
        columns = [
            column[1] for column in self.db.execute("PRAGMA table_info(runtime_status)")
        ]
        return dict(zip(columns, row, strict=True))

    def runtime_status(self):
        """Return the local daemon status without performing network work."""
        return self._runtime_row()

    def runtime_daemon_started(self):
        now = datetime.now(UTC).isoformat()
        self._runtime_row()
        self.db.execute(
            "UPDATE runtime_status SET daemon_started_at=?, daemon_heartbeat_at=? WHERE id=1",
            (now, now),
        )
        self.db.commit()

    def runtime_heartbeat(self):
        now = datetime.now(UTC).isoformat()
        self._runtime_row()
        self.db.execute(
            "UPDATE runtime_status SET daemon_heartbeat_at=? WHERE id=1", (now,)
        )
        self.db.commit()

    def runtime_scan_started(self, run_id=None):
        now = datetime.now(UTC).isoformat()
        run_id = run_id or uuid.uuid4().hex
        self._runtime_row()
        self.db.execute(
            """UPDATE runtime_status SET last_scan_run_id=?, last_scan_started_at=?
            WHERE id=1""",
            (run_id, now),
        )
        self.db.commit()
        return run_id

    def runtime_scan_finished(self, run_id, status, error_type=None):
        now = datetime.now(UTC).isoformat()
        self._runtime_row()
        if status == "SUCCESS":
            self.db.execute(
                """UPDATE runtime_status SET last_scan_run_id=?, last_scan_finished_at=?,
                last_scan_completed_at=?, consecutive_failures=0, last_error_type=NULL
                WHERE id=1""",
                (run_id, now, now),
            )
        else:
            self.db.execute(
                """UPDATE runtime_status SET last_scan_run_id=?, last_scan_finished_at=?,
                last_scan_failed_at=?, consecutive_failures=consecutive_failures + 1,
                last_error_type=? WHERE id=1""",
                (run_id, now, now, error_type),
            )
        self.db.commit()

    def runtime_telegram_activity(self):
        now = datetime.now(UTC).isoformat()
        self._runtime_row()
        self.db.execute(
            """UPDATE runtime_status SET last_telegram_activity_at=?,
            telegram_consecutive_failures=0 WHERE id=1""",
            (now,),
        )
        self.db.commit()

    def runtime_telegram_failure(self, error_type="TELEGRAM_POLL_ERROR"):
        now = datetime.now(UTC).isoformat()
        self._runtime_row()
        self.db.execute(
            """UPDATE runtime_status SET last_telegram_error_at=?,
            telegram_consecutive_failures=telegram_consecutive_failures + 1,
            last_error_type=? WHERE id=1""",
            (now, error_type),
        )
        self.db.commit()

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

    def get_extraction(self, deal_id, product_text=None):
        row = self.db.execute(
            "SELECT payload, content_fingerprint, extractor_version FROM product_extractions WHERE deal_id=?",
            (deal_id,),
        ).fetchone()
        if row is None:
            return None
        # Rows from before fingerprinting remain usable for callers that only
        # know the deal id. New evaluations require a matching fingerprint;
        # changed provider content must not inherit stale facts.
        if product_text is not None and row[1] is not None:
            if row[1] != extraction_fingerprint(product_text):
                return None
            if row[2] != EXTRACTION_CACHE_VERSION:
                return None
        return json.loads(row[0])

    def save_extraction(self, deal_id, payload, product_text=None):
        self.db.execute(
            "INSERT OR REPLACE INTO product_extractions VALUES (?,?,?,?)",
            (
                deal_id,
                json.dumps(payload, default=str),
                extraction_fingerprint(product_text)
                if product_text is not None
                else None,
                EXTRACTION_CACHE_VERSION if product_text is not None else None,
            ),
        )
        self.db.commit()
