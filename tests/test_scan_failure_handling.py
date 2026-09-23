"""Chollometro failure handling at the scanner, state and daemon level.

All tests are offline: the provider is a scripted transport and the backoff is
recorded, never slept. They prove the two properties the failure model rests
on: a successful empty scan is not a provider failure, and a temporary
Chollometro failure neither corrupts the stored state nor stops the daemon.
"""

import logging
import threading
from decimal import Decimal

import pytest
import requests
from test_provider_resilience import (
    EMPTY_RESULTS,
    UNEXPECTED_PAYLOAD,
    Response,
    Transport,
    make_client,
    page,
)

from chollometro_alerts import cli
from chollometro_alerts.alert_rule import AlertRule
from chollometro_alerts.errors import (
    SCAN_FAILED,
    SCAN_PARTIAL,
    SCAN_SUCCESS,
    ChollometroHTTPError,
)
from chollometro_alerts.product import extract_product
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.runtime import run_daemon
from chollometro_alerts.service import AlertService

SNAPSHOT_TABLES = (
    "deals",
    "alert_rules",
    "rule_deal_observations",
    "deal_rule_matches",
)


@pytest.fixture(autouse=True)
def offline_env(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


class RecordingNotifier:
    dry_run = False

    def __init__(self):
        self.sent = []
        self.alerts = []

    def send(self, deal, evidence=None):
        self.sent.append(deal)

    def send_system_alert(self, error_type, component, message, run_id):
        self.alerts.append((error_type, component, message))


class CountingExtractor:
    """Deterministic stand-in that records every LLM call a scan would spend."""

    def __init__(self):
        self.calls = 0

    def __call__(self, product_text, deal_id=None):
        self.calls += 1
        return extract_product(product_text)


def make_service(tmp_path, *responses, notifier=None, extractor=None):
    repository = DealRepository(tmp_path / "scan.db")
    client, delays = make_client(Transport(*responses))
    extractor = extractor or CountingExtractor()
    notifier = notifier or RecordingNotifier()
    service = AlertService(client, repository, notifier, extractor)
    return service, repository, client, delays


def add_rule(repository, query="leche"):
    return repository.save_alert_rule(AlertRule(query=query), query)


def rule_for(service, repository, rule_id):
    return service._interest_rule(repository.rule_by_id(rule_id))


def scan_runs(repository):
    columns = [row[1] for row in repository.db.execute("PRAGMA table_info(scan_runs)")]
    return [
        dict(zip(columns, row))
        for row in repository.db.execute("SELECT * FROM scan_runs ORDER BY rowid")
    ]


def snapshot(repository):
    state = {}
    for table in SNAPSHOT_TABLES:
        state[table] = [
            tuple(row)
            for row in repository.db.execute(f"SELECT * FROM {table} ORDER BY rowid")
        ]
    return state


def test_successful_empty_scan_is_recorded_as_success(tmp_path):
    service, repository, _client, delays = make_service(
        tmp_path, Response(200, EMPTY_RESULTS)
    )
    rule_id = add_rule(repository)

    sent = service.run_rule(rule_id, "leche", rule_for(service, repository, rule_id))

    assert sent == 0
    assert service.last_scan_status == SCAN_SUCCESS
    assert service.last_summary.found == 0
    rows = scan_runs(repository)
    assert [
        (row["status"], row["error_type"], row["relevant_items"]) for row in rows
    ] == [(SCAN_SUCCESS, None, 0)]
    assert repository.deal_count() == 0
    assert repository.rule_observations(rule_id) == []
    assert service.notifier.sent == []
    assert service.extractor.calls == 0
    assert delays == []


@pytest.mark.parametrize(
    "responses, error_type",
    [
        ([requests.Timeout()] * 3, "TIMEOUT"),
        ([requests.ConnectionError()] * 3, "NETWORK_ERROR"),
        ([Response(503)] * 3, "HTTP_503"),
        ([Response(403)] * 1, "HTTP_403"),
        ([Response(200, UNEXPECTED_PAYLOAD)], "PARSE_ERROR"),
    ],
)
def test_total_provider_failure_is_failed_not_empty(tmp_path, responses, error_type):
    service, repository, _client, _delays = make_service(tmp_path, *responses)
    rule_id = add_rule(repository)
    before = snapshot(repository)

    sent = service.run_rule(rule_id, "leche", rule_for(service, repository, rule_id))

    assert sent == 0
    assert service.last_scan_status == SCAN_FAILED
    assert service.last_summary.scan_error_type == error_type
    rows = scan_runs(repository)
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["error_type"]) == (SCAN_FAILED, error_type)
    # No deal, observation, match or baseline change, and no notification.
    assert snapshot(repository) == before
    assert service.notifier.sent == []
    assert service.extractor.calls == 0


def test_partial_scan_keeps_valid_deals_but_is_never_a_success(tmp_path):
    service, repository, _client, _delays = make_service(
        tmp_path, Response(200, page(1)), Response(503), Response(503), Response(503)
    )
    rule_id = add_rule(repository)

    sent = service.run_rule(
        rule_id, "leche", rule_for(service, repository, rule_id), pages=2
    )

    assert sent == 1
    assert service.last_scan_status == SCAN_PARTIAL
    assert [deal.deal_id for deal in service.notifier.sent] == ["1"]
    assert service.extractor.calls == 1
    row = scan_runs(repository)[-1]
    assert (row["status"], row["error_type"], row["http_status"]) == (
        SCAN_PARTIAL,
        "HTTP_503",
        503,
    )
    assert (row["relevant_items"], row["fetched_items"]) == (1, 1)
    observations = repository.rule_observations(rule_id)
    assert len(observations) == 1
    # rule_deal_observations: (..., baseline, matched, rejection_reason, notified_at)
    assert (observations[0][4], observations[0][5]) == (0, 1)
    assert observations[0][7] is not None


def test_partial_scan_is_completed_by_the_next_cycle(tmp_path):
    service, repository, client, _delays = make_service(
        tmp_path, Response(200, page(1)), Response(503), Response(503), Response(503)
    )
    rule_id = add_rule(repository)
    rule = rule_for(service, repository, rule_id)
    assert service.run_rule(rule_id, "leche", rule, pages=2) == 1

    # The provider recovers and the page that failed is discovered again.
    client.session.responses.extend(
        [Response(200, page(1, 2)), Response(200, EMPTY_RESULTS)]
    )
    assert service.run_rule(rule_id, "leche", rule, pages=2) == 1

    assert service.last_scan_status == SCAN_SUCCESS
    assert [deal.deal_id for deal in service.notifier.sent] == ["1", "2"]
    assert [row["status"] for row in scan_runs(repository)] == [
        SCAN_PARTIAL,
        SCAN_SUCCESS,
    ]
    assert len(repository.rule_observations(rule_id)) == 2


def test_failed_scan_preserves_the_existing_baseline(tmp_path):
    service, repository, client, _delays = make_service(
        tmp_path, Response(200, page(1, 2, 3))
    )
    rule_id = add_rule(repository)
    assert service.baseline_rule(rule_id, "leche") == 3
    before = snapshot(repository)
    assert len(before["deals"]) == 3
    assert len(before["rule_deal_observations"]) == 3

    client.session.responses.extend([Response(503)] * 3)
    assert (
        service.run_rule(rule_id, "leche", rule_for(service, repository, rule_id)) == 0
    )

    assert service.last_scan_status == SCAN_FAILED
    # The baseline is neither emptied nor rewritten: A B C stays A B C.
    assert snapshot(repository) == before
    assert repository.deal_count() == 3
    assert service.notifier.sent == []
    assert [row["status"] for row in scan_runs(repository)] == [SCAN_FAILED]


def test_baseline_initialization_fails_closed_on_provider_failure(tmp_path):
    service, repository, _client, _delays = make_service(
        tmp_path, Response(503), Response(503), Response(503)
    )
    rule_id = add_rule(repository)

    with pytest.raises(ChollometroHTTPError):
        service.baseline_rule(rule_id, "leche")

    rule = repository.get_rule(rule_id)
    assert (rule[6], rule[7]) == (0, "INITIALIZING_FAILED")
    assert repository.deal_count() == 0
    assert repository.rule_observations(rule_id) == []


def test_provider_failure_raises_one_operational_alert_per_scan(tmp_path):
    notifier = RecordingNotifier()
    service, repository, client, _delays = make_service(
        tmp_path, Response(503), Response(503), Response(503), notifier=notifier
    )
    rule_id = add_rule(repository)
    rule = rule_for(service, repository, rule_id)

    service.run_rule(rule_id, "leche", rule)

    # Three internal HTTP attempts, one logical failure, one message.
    assert len(client.session.requests) == 3
    assert [alert[:2] for alert in notifier.alerts] == [
        ("HTTP_503", "ChollometroClient")
    ]

    # A second failing scan inside the cooldown adds nothing.
    client.session.responses.extend([Response(503)] * 3)
    service.run_rule(rule_id, "leche", rule)
    assert len(notifier.alerts) == 1


def test_dry_run_failure_writes_nothing_and_notifies_nobody(tmp_path):
    service, repository, _client, _delays = make_service(
        tmp_path, Response(503), Response(503), Response(503)
    )
    rule_id = add_rule(repository)

    report = service.run_rule(
        rule_id, "leche", rule_for(service, repository, rule_id), dry_run=True
    )

    assert report == []
    assert service.last_scan_status == SCAN_FAILED
    assert scan_runs(repository) == []
    assert repository.deal_count() == 0
    assert repository.rule_observations(rule_id) == []
    assert service.notifier.sent == [] and service.notifier.alerts == []
    assert repository.db.execute("SELECT COUNT(*) FROM error_alerts").fetchone()[0] == 0


class NoWaitEvent(threading.Event):
    """Timed waits return immediately: the daemon test never sleeps."""

    def wait(self, timeout=None):
        if self.is_set():
            return True
        super().wait(0)
        return False


class StubController:
    def __init__(self, repository):
        self.repository = repository

    def listen_forever(self, stop_event=None):
        if stop_event is not None:
            stop_event.wait()


class CycleAwareService(AlertService):
    """Stops the daemon after a fixed number of scan cycles."""

    def __init__(self, *args, stop_event, cycles, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_event = stop_event
        self.cycles = cycles
        self.scans = 0

    def run_active_rules(self, pages=1):
        self.scans += 1
        result = super().run_active_rules(pages=pages)
        if self.scans >= self.cycles:
            self.stop_event.set()
        return result


def test_daemon_recovers_after_a_temporary_provider_failure(tmp_path, caplog):
    repository = DealRepository(tmp_path / "daemon.db")
    add_rule(repository)
    transport = Transport(
        Response(503), Response(503), Response(503), Response(200, page(1))
    )
    client, _delays = make_client(transport)
    notifier = RecordingNotifier()
    extractor = CountingExtractor()
    stop = NoWaitEvent()
    service = CycleAwareService(
        client,
        repository,
        notifier,
        extractor,
        stop_event=stop,
        cycles=2,
    )

    with caplog.at_level(logging.INFO):
        run_daemon(
            StubController(repository),
            service,
            interval_minutes=1,
            pages=1,
            stop_event=stop,
        )

    assert service.scans == 2
    assert [row["status"] for row in scan_runs(repository)] == [
        SCAN_FAILED,
        SCAN_SUCCESS,
    ]
    # The daemon kept running and the recovered cycle worked normally.
    assert [deal.deal_id for deal in notifier.sent] == ["1"]
    assert service.last_scan_status == SCAN_SUCCESS
    assert extractor.calls == 1
    messages = [record.message for record in caplog.records]
    assert any(message.startswith("scan_started") for message in messages)
    assert "scan_finished status=FAILED" in messages
    assert "scan_finished status=SUCCESS" in messages


def test_check_cli_signals_a_failed_scan(monkeypatch, tmp_path, capsys):
    service, repository, client, _delays = make_service(
        tmp_path, Response(503), Response(503), Response(503)
    )
    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli, "ChollometroClient", lambda: client)
    monkeypatch.setattr(cli, "DealRepository", lambda path: repository)
    monkeypatch.setattr(cli, "AlertService", lambda *args: service)
    monkeypatch.setattr(cli, "load_rules", dict)
    monkeypatch.setattr(
        "sys.argv", ["chollometro-alerts", "--db", ":memory:", "check", "--dry-run"]
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    output = capsys.readouterr().out
    assert "SCAN_STATUS=FAILED" in output
    assert "SCAN_ERROR_TYPE=HTTP_503" in output
    assert repository.deal_count() == 0


def test_baseline_cli_reports_a_failed_scan_without_writing_state(
    monkeypatch, tmp_path, capsys
):
    service, repository, client, _delays = make_service(
        tmp_path, Response(200, page(1)), Response(503), Response(503), Response(503)
    )
    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli, "ChollometroClient", lambda: client)
    monkeypatch.setattr(cli, "DealRepository", lambda path: repository)
    monkeypatch.setattr(cli, "AlertService", lambda *args: service)
    monkeypatch.setattr(
        "sys.argv", ["chollometro-alerts", "--db", ":memory:", "baseline"]
    )
    # The first query succeeds and the second one fails: no baseline is written.
    client.session.responses.extend([Response(503)])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    output = capsys.readouterr().out
    assert "SCAN_STATUS=FAILED" in output and "ERROR_TYPE=HTTP_503" in output
    assert repository.deal_count() == 0


def test_new_telegram_alert_reports_the_failure_and_can_be_retried(tmp_path):
    from unittest.mock import Mock

    from chollometro_alerts.intent import AlertIntent
    from chollometro_alerts.telegram_rules import TelegramRuleController

    service, repository, _client, _delays = make_service(
        tmp_path, Response(503), Response(503), Response(503), Response(200, page(1))
    )
    translator = Mock()
    translator.interpret_alert.return_value = AlertIntent(
        action="create",
        query="leche",
        product_type="leche",
        max_price=Decimal(5),
        price_unit="absolute",
    )
    controller = TelegramRuleController(
        bot_token="token",
        authorized_chat_id="123",
        repository=repository,
        translator=translator,
        service=service,
    )
    sent = []
    controller.send_message = sent.append
    update = {
        "update_id": 1,
        "message": {"chat": {"id": "123"}, "text": "avísame de leche por < 5€"},
    }

    reply = controller.process_update(update)

    # The user is told the truth: no baseline, rule not active.
    assert "HTTP_503" in reply and "no se ha activado" in reply
    rule_id = repository.list_alert_rules()[0][0]
    assert repository.get_rule(rule_id)[6:] == (0, "INITIALIZING_FAILED")
    assert repository.deal_count() == 0

    # Sending the message again completes the initialization once Chollometro
    # answers, without creating a second rule or a false baseline.
    update["update_id"] = 2
    reply = controller.process_update(update)

    assert "Alerta creada" in reply and "1 ofertas actuales" in reply
    assert len(repository.list_alert_rules()) == 1
    assert repository.get_rule(rule_id)[6:] == (1, "ACTIVE")
    assert repository.deal_count() == 1
