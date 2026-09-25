import sqlite3
import threading
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import ClassVar

from chollometro_alerts.alert_rule import (
    AlertConstraints,
    AlertRule,
    NotificationWindow,
)
from chollometro_alerts.models import Deal
from chollometro_alerts.repository import (
    PENDING_TELEGRAM_FAILURE,
    DealRepository,
)
from chollometro_alerts.schedule import window_from_alert_rule
from chollometro_alerts.service import AlertService, RunSummary
from chollometro_alerts.telegram import TelegramNotifier
from chollometro_alerts.telegram_rules import TelegramRuleController


def rule(query):
    return AlertRule(
        query=query,
        product=query,
        constraints=AlertConstraints(max_price=Decimal(100)),
    )


def update(update_id, user_id, chat_id, text):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id, "username": f"u{user_id}", "first_name": "Test"},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def typed_update(update_id, user_id, chat_id, chat_type, text):
    value = update(update_id, user_id, chat_id, text)
    value["message"]["chat"]["type"] = chat_type
    return value


def test_legacy_seven_rules_are_assigned_once(monkeypatch, tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE alert_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,
        product_type TEXT, brand TEXT, max_price TEXT NOT NULL,
        price_unit TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(query, product_type, brand, max_price, price_unit)
    )""")
    for index in range(7):
        db.execute(
            "INSERT INTO alert_rules(query,max_price,price_unit,created_at,updated_at) VALUES (?,?,?,?,?)",
            (f"legacy-{index}", "100", "absolute", "created", "updated"),
        )
    db.commit()
    db.close()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "legacy-chat")
    monkeypatch.delenv("TELEGRAM_USER_ID", raising=False)

    repository = DealRepository(path)
    assert len(repository.list_users()) == 1
    user_id = repository.list_users()[0][0]
    assert (
        repository.db.execute(
            "SELECT COUNT(*) FROM alert_rules WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        == 7
    )
    repository.close()
    reopened = DealRepository(path)
    assert len(reopened.list_users()) == 1
    assert (
        reopened.db.execute(
            "SELECT COUNT(*) FROM alert_rules WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        == 7
    )


def test_multiuser_list_and_delete_are_scoped(tmp_path):
    repository = DealRepository(tmp_path / "users.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    first_rule = repository.save_alert_rule(rule("leche"), "leche", user_id=first.id)
    second_rule = repository.save_alert_rule(
        rule("relojes"), "relojes", user_id=second.id
    )
    assert [row[0] for row in repository.list_alert_rules(user_id=first.id)] == [
        first_rule
    ]
    assert [row[0] for row in repository.list_alert_rules(user_id=second.id)] == [
        second_rule
    ]
    assert repository.delete_alert_rule(second_rule, user_id=first.id) is False
    assert (
        repository.replace_alert_rule(first_rule, rule("cafe"), user_id=second.id)
        is False
    )
    assert repository.delete_alert_rule(second_rule, user_id=second.id) is True


def test_same_rule_can_belong_to_two_users(tmp_path):
    repository = DealRepository(tmp_path / "same-rule.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")

    first_rule = repository.save_alert_rule(rule("leche"), "leche", user_id=first.id)
    second_rule = repository.save_alert_rule(rule("leche"), "leche", user_id=second.id)

    assert first_rule != second_rule
    assert [row[0] for row in repository.list_alert_rules(user_id=first.id)] == [
        first_rule
    ]
    assert [row[0] for row in repository.list_alert_rules(user_id=second.id)] == [
        second_rule
    ]


def test_user_cannot_read_another_users_rule_by_known_id(tmp_path):
    repository = DealRepository(tmp_path / "read-isolation.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    rule_id = repository.save_alert_rule(rule("leche"), "leche", user_id=first.id)

    assert repository.rule_by_id(rule_id, user_id=second.id) is None
    assert repository.get_rule(rule_id, user_id=second.id) is None


def test_user_cannot_delete_another_users_rule_by_known_id(tmp_path):
    repository = DealRepository(tmp_path / "delete-isolation.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    rule_id = repository.save_alert_rule(rule("leche"), "leche", user_id=first.id)

    assert repository.delete_alert_rule(rule_id, user_id=second.id) is False
    assert repository.rule_by_id(rule_id, user_id=first.id) is not None


def test_user_cannot_update_another_users_rule_by_known_id(tmp_path):
    repository = DealRepository(tmp_path / "update-isolation.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    rule_id = repository.save_alert_rule(rule("leche"), "leche", user_id=first.id)

    assert (
        repository.replace_alert_rule(rule_id, rule("cafe"), user_id=second.id) is False
    )
    assert repository.rule_by_id(rule_id, user_id=first.id).query == "leche"


def test_user_cannot_enable_or_disable_another_users_rule_by_known_id(tmp_path):
    repository = DealRepository(tmp_path / "state-isolation.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    rule_id = repository.save_alert_rule(rule("leche"), "leche", user_id=first.id)

    repository.set_rule_state(rule_id, "DISABLED", enabled=False, user_id=second.id)
    assert repository.get_rule(rule_id, user_id=first.id)[6:8] == (1, "ACTIVE")


def test_alert_context_is_independent_per_chat(tmp_path):
    repository = DealRepository(tmp_path / "context-isolation.sqlite3")
    repository.set_alert_context("chat-a", [1, 2])
    repository.set_alert_context("chat-b", [3])

    assert repository.get_alert_context("chat-a") == [1, 2]
    assert repository.get_alert_context("chat-b") == [3]
    repository.clear_alert_context("chat-a")
    assert repository.get_alert_context("chat-a") == []
    assert repository.get_alert_context("chat-b") == [3]


def test_unknown_multiuser_is_rejected_and_known_user_can_list(tmp_path):
    repository = DealRepository(tmp_path / "telegram.sqlite3")
    user = repository.create_user(telegram_user_id="42", telegram_chat_id="chat-a")
    repository.save_alert_rule(rule("leche"), "leche", user_id=user.id)
    replies = []

    class Controller(TelegramRuleController):
        def send_message(self, text):
            replies.append(text)

    controller = Controller(
        bot_token="token",
        authorized_chat_id="legacy",
        repository=repository,
        translator=None,
        multiuser_enabled=True,
        auto_register=False,
    )
    assert (
        controller.process_update(
            typed_update(1, "99", "chat-x", "private", "Qué alertas tengo")
        )
        is None
    )
    assert "No tienes acceso" in replies[-1]
    assert controller.process_update(
        typed_update(2, "42", "chat-a", "private", "Qué alertas tengo")
    )
    assert "leche" in replies[-1].casefold()


def test_private_only_policy_rejects_non_private_updates_before_resolution(tmp_path):
    repository = DealRepository(tmp_path / "private-only.sqlite3")
    users_before = len(repository.list_users())
    rules_before = len(repository.list_alert_rules())
    known = repository.create_user(telegram_user_id="42", telegram_chat_id="chat-a")
    repository.save_alert_rule(rule("leche"), "leche", user_id=known.id)
    replies = []

    class Translator:
        def interpret_alert(self, _text):
            raise AssertionError("group updates must not reach the LLM")

    class Controller(TelegramRuleController):
        def send_message(self, text):
            replies.append(text)

    controller = Controller(
        bot_token="token",
        authorized_chat_id="legacy",
        repository=repository,
        translator=Translator(),
        multiuser_enabled=True,
        auto_register=True,
    )
    for index, chat_type in enumerate(("group", "supergroup", "channel"), start=1):
        assert (
            controller.process_update(
                typed_update(
                    index,
                    "unknown",
                    f"{chat_type}-chat",
                    chat_type,
                    "Avísame de crear alerta",
                )
            )
            is None
        )
    assert len(repository.list_users()) == users_before + 1
    assert len(repository.list_alert_rules()) == rules_before + 1
    assert len(repository.db.execute("SELECT * FROM telegram_updates").fetchall()) == 0
    assert all("únicamente por chat privado" in message for message in replies)


def test_notifier_routes_by_rule_owner(monkeypatch, tmp_path):
    repository = DealRepository(tmp_path / "routing.sqlite3")
    user = repository.create_user(telegram_user_id="7", telegram_chat_id="chat-owner")
    rule_id = repository.save_alert_rule(rule("leche"), "leche", user_id=user.id)
    calls = []

    class Response:
        status_code = 200
        headers: ClassVar = {}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        "chollometro_alerts.telegram.requests.post",
        lambda url, **kwargs: calls.append(kwargs) or Response(),
    )
    notifier = TelegramNotifier("token", "global-chat", repository=repository)
    from chollometro_alerts.models import Deal

    notifier.send(
        Deal("d", "Leche", "https://example.test", Decimal(1), "shop", 1, "milk", None),
        rule_id=rule_id,
    )
    assert calls[0]["json"]["chat_id"] == "chat-owner"


def test_same_deal_is_notified_independently_to_two_users(monkeypatch, tmp_path):
    repository = DealRepository(tmp_path / "two-destinations.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    first_rule = repository.save_alert_rule(rule("leche"), "leche", user_id=first.id)
    second_rule = repository.save_alert_rule(rule("leche"), "leche", user_id=second.id)
    calls = []

    class Response:
        status_code = 200
        headers: ClassVar = {}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        "chollometro_alerts.telegram.requests.post",
        lambda url, **kwargs: calls.append(kwargs) or Response(),
    )
    notifier = TelegramNotifier("token", "global-chat", repository=repository)
    deal = Deal(
        "shared",
        "Leche",
        "https://example.test/shared",
        Decimal(1),
        "shop",
        1,
        "milk",
        None,
    )

    notifier.send(deal, rule_id=first_rule)
    notifier.send(deal, rule_id=second_rule)

    assert [call["json"]["chat_id"] for call in calls] == ["chat-a", "chat-b"]
    repository.record_rule_match(deal.deal_id, first_rule)
    repository.record_rule_match(deal.deal_id, second_rule)
    repository.mark_rule_match_notified(deal.deal_id, first_rule)
    matches = repository.db.execute(
        "SELECT rule_id,notified_at FROM deal_rule_matches WHERE deal_id=? ORDER BY rule_id",
        (deal.deal_id,),
    ).fetchall()
    assert matches[0][1] is not None and matches[1][1] is None


def test_telegram_failure_for_one_user_does_not_affect_another(tmp_path):
    repository = DealRepository(tmp_path / "failure-isolation.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    first_rule = repository.save_alert_rule(rule("leche"), "leche", user_id=first.id)
    second_rule = repository.save_alert_rule(rule("leche"), "leche", user_id=second.id)

    class Notifier:
        dry_run = False

        def send(self, deal, evidence=None, rule_id=None):
            if rule_id == first_rule:
                raise RuntimeError("chat-a unavailable")

        def send_system_alert(self, *args, **kwargs):
            return None

    service = AlertService(client=None, repository=repository, notifier=Notifier())
    service.last_summary = RunSummary()
    deal = Deal(
        "shared",
        "Leche",
        "https://example.test/shared",
        Decimal(1),
        "shop",
        1,
        "milk",
        None,
    )

    repository.claim_rule_observation(first_rule, deal.deal_id)
    repository.claim_rule_observation(second_rule, deal.deal_id)
    assert service._deliver(first_rule, deal) == 0
    assert service._deliver(second_rule, deal) == 1
    assert (
        repository.rule_observation_pending_reason(first_rule, deal.deal_id)
        == PENDING_TELEGRAM_FAILURE
    )
    assert repository.rule_observation_pending_reason(second_rule, deal.deal_id) is None
    assert repository.get_rule_observation(second_rule, deal.deal_id)[7] is not None


def test_schedule_isolation_keeps_one_users_match_pending(tmp_path):
    repository = DealRepository(tmp_path / "schedule-isolation.sqlite3")
    first = repository.create_user(telegram_user_id="1", telegram_chat_id="chat-a")
    second = repository.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    first_rule = AlertRule(
        query="leche",
        product="leche",
        constraints=AlertConstraints(max_price=Decimal(100)),
        notification_window=NotificationWindow(
            start=time(10), end=time(11), timezone="UTC"
        ),
    )
    second_rule = AlertRule(
        query="leche",
        product="leche",
        constraints=AlertConstraints(max_price=Decimal(100)),
        notification_window=NotificationWindow(
            start=time(0), end=time(23, 59), timezone="UTC"
        ),
    )
    first_id = repository.save_alert_rule(first_rule, "leche", user_id=first.id)
    second_id = repository.save_alert_rule(second_rule, "leche", user_id=second.id)

    class Notifier:
        dry_run = False

        def __init__(self):
            self.sent = []

        def send(self, deal, evidence=None, rule_id=None):
            self.sent.append(rule_id)

        def send_system_alert(self, *args, **kwargs):
            return None

    notifier = Notifier()
    service = AlertService(
        client=None,
        repository=repository,
        notifier=notifier,
        clock=lambda: datetime(2026, 9, 25, 12, tzinfo=UTC),
    )
    service.last_summary = RunSummary()
    deal = Deal(
        "shared",
        "Leche",
        "https://example.test/shared",
        Decimal(1),
        "shop",
        1,
        "milk",
        None,
    )
    repository.claim_rule_observation(first_id, deal.deal_id)
    repository.claim_rule_observation(second_id, deal.deal_id)

    assert (
        service._deliver(first_id, deal, window=window_from_alert_rule(first_rule)) == 0
    )
    assert (
        service._deliver(second_id, deal, window=window_from_alert_rule(second_rule))
        == 1
    )
    assert (
        repository.rule_observation_pending_reason(first_id, deal.deal_id)
        == "NOTIFICATION_SCHEDULE"
    )
    assert repository.get_rule_observation(second_id, deal.deal_id)[7] is not None
    assert notifier.sent == [second_id]


def test_legacy_migration_preserves_state_and_is_idempotent(monkeypatch, tmp_path):
    path = tmp_path / "legacy-complete.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,
            product_type TEXT, brand TEXT, max_price TEXT NOT NULL,
            price_unit TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(query, product_type, brand, max_price, price_unit)
        );
        CREATE TABLE deals (
            deal_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
            price TEXT, merchant TEXT, temperature INTEGER, category TEXT NOT NULL,
            published_at TEXT, first_seen_at TEXT NOT NULL, notified_at TEXT
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
            rejection_reason TEXT, notified_at TEXT,
            PRIMARY KEY(rule_id, deal_id)
        );
        CREATE TABLE feed_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE deal_temperature_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
            temperature REAL NOT NULL, observed_at TEXT NOT NULL
        );
        CREATE TABLE runtime_status (
            id INTEGER PRIMARY KEY CHECK (id = 1), created_at TEXT NOT NULL,
            daemon_started_at TEXT, daemon_heartbeat_at TEXT,
            last_scan_run_id TEXT, last_scan_started_at TEXT,
            last_scan_finished_at TEXT, last_scan_completed_at TEXT,
            last_scan_failed_at TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error_type TEXT, last_telegram_activity_at TEXT,
            last_telegram_error_at TEXT, telegram_consecutive_failures INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    db.execute(
        "INSERT INTO alert_rules(query,max_price,price_unit,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("leche", "100", "absolute", "created", "updated"),
    )
    db.execute(
        "INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "deal-1",
            "Leche",
            "https://example.test/1",
            "1",
            "shop",
            1,
            "milk",
            None,
            "seen",
            None,
        ),
    )
    db.execute(
        "INSERT INTO deal_rule_matches VALUES (?,?,?,?)", ("deal-1", 1, "matched", None)
    )
    db.execute(
        "INSERT INTO rule_deal_observations(rule_id,deal_id,first_seen_at,last_seen_at,matched) VALUES (?,?,?,?,?)",
        (1, "deal-1", "first", "last", 1),
    )
    db.execute(
        "INSERT INTO feed_state VALUES (?,?,?)", ("watermark", "value", "updated")
    )
    db.execute(
        "INSERT INTO deal_temperature_snapshots(thread_id,temperature,observed_at) VALUES (?,?,?)",
        ("deal-1", 12, "seen"),
    )
    db.execute("INSERT INTO runtime_status(id,created_at) VALUES (1,'created')")
    db.commit()
    db.close()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "legacy-chat")
    monkeypatch.delenv("TELEGRAM_USER_ID", raising=False)

    repository = DealRepository(path)
    owner_id = repository.list_users()[0][0]
    assert repository.db.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0] == 1
    assert (
        repository.db.execute("SELECT user_id FROM alert_rules").fetchone()[0]
        == owner_id
    )
    assert (
        repository.db.execute("SELECT COUNT(*) FROM deal_rule_matches").fetchone()[0]
        == 1
    )
    assert (
        repository.db.execute("SELECT COUNT(*) FROM rule_deal_observations").fetchone()[
            0
        ]
        == 1
    )
    assert repository.feed_state("watermark") == "value"
    assert (
        repository.db.execute(
            "SELECT COUNT(*) FROM deal_temperature_snapshots"
        ).fetchone()[0]
        == 1
    )
    repository.close()

    reopened = DealRepository(path)
    assert len(reopened.list_users()) == 1
    assert reopened.db.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0] == 1
    second = reopened.create_user(telegram_user_id="2", telegram_chat_id="chat-b")
    assert reopened.save_alert_rule(rule("leche"), "leche", user_id=second.id)


def test_two_users_can_write_same_sqlite_concurrently(tmp_path):
    repository = DealRepository(tmp_path / "concurrent-users.sqlite3")
    barrier = threading.Barrier(2)
    failures = []

    def worker(index):
        try:
            user = repository.create_user(
                telegram_user_id=str(index), telegram_chat_id=f"chat-{index}"
            )
            barrier.wait()
            repository.save_alert_rule(rule("leche"), "leche", user_id=user.id)
        except Exception as exc:  # noqa: BLE001 - assertion below reports the error
            failures.append(exc)
        finally:
            repository.close_current_thread()

    threads = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len([row for row in repository.list_users() if row[1] in {"1", "2"}]) == 2
    assert (
        repository.db.execute(
            "SELECT COUNT(*) FROM alert_rules WHERE query='leche'"
        ).fetchone()[0]
        == 2
    )
