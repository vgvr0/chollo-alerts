from decimal import Decimal

import pytest
import requests

from chollometro_alerts.client import ChollometroClient
from chollometro_alerts.config import InterestRule
from chollometro_alerts.errors import ChollometroHTTPError, ChollometroTimeoutError
from chollometro_alerts.filters import category_for
from chollometro_alerts.models import Deal
from chollometro_alerts.parser import parse_search
from chollometro_alerts.service import AlertService

FULL = """<article id="thread_123"><div class="threadListCard-header"><span>Publicado hace 2 h</span><button>268°</button></div><a class="thread-title" href="/ofertas/cerveza-123">Pack cerveza Mahou</a><span class="thread-price">12,50€</span><a data-t="merchantLink">Carrefour</a></article>"""
MISSING = """<article id="thread_124"><div class="threadListCard-header"><span>Publicado hace 3 días</span></div><a class="thread-title--list" href="/ofertas/leche-124">Leche Pascual</a></article>"""


def test_complete_and_absolute_url():
    d = parse_search(FULL, "cerveza")[0]
    assert (
        d.deal_id == "123"
        and d.price == Decimal("12.50")
        and d.merchant == "Carrefour"
        and d.temperature == 268
        and d.category == "beer"
        and d.url.startswith("https://")
        and d.published_at
    )


def test_optional_fields_and_changed_html():
    d = parse_search(MISSING, "leche")[0]
    assert (
        d.price is None
        and d.merchant is None
        and d.temperature is None
        and d.category == "milk"
        and d.published_at
    )


def test_classification_and_irrelevant():
    assert category_for("Puleva leche") == "milk"
    assert category_for("Estrella Galicia 1906") == "beer"
    assert (
        parse_search(
            '<article id="thread_1"><a class="thread-title">Cafetera</a></article>',
            "leche",
        )
        == []
    )


def test_empty_and_bad_date():
    assert parse_search("", "leche") == []
    assert (
        parse_search(
            '<article id="thread_1"><a class="thread-title">Leche</a><span>Publicado hace ahora</span></article>',
            "leche",
        )[0].published_at
        is None
    )


class Client:
    def recent(self, *a):
        return [Deal("1", "Leche", "https://x", None, None, None, "milk", None)]


class Repo:
    def __init__(self):
        self.ids = set()
        self.extractions = {}
        self.notified = set()

    def exists(self, deal_id):
        return deal_id in self.ids

    def get_extraction(self, deal_id):
        return self.extractions.get(deal_id)

    def save_extraction(self, deal_id, payload):
        self.extractions[deal_id] = payload

    def upsert(self, d):
        self.ids.add(d.deal_id)

    def was_notified(self, i):
        return i in self.notified

    def mark_notified(self, i):
        self.notified.add(i)


class Notifier:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, d):
        if self.fail:
            raise RuntimeError
        self.sent.append(d)


def test_idempotency_and_notified_at():
    r, n = Repo(), Notifier()
    s = AlertService(Client(), r, n)
    assert s.run(["leche"]) == 1
    assert s.run(["leche"]) == 0
    assert len(n.sent) == 1


def test_telegram_failure():
    r = Repo()
    with pytest.raises(RuntimeError):
        AlertService(Client(), r, Notifier(True)).run(["leche"])
    assert not r.notified


def test_baseline_initial_inserts_without_telegram():
    r, n = Repo(), Notifier()
    s = AlertService(Client(), r, n)
    assert s.baseline(["leche"]) == 1
    assert r.ids == {"1"}
    assert r.notified == {"1"}
    assert n.sent == []


def test_second_baseline_is_idempotent():
    r, n = Repo(), Notifier()
    s = AlertService(Client(), r, n)
    s.baseline(["leche"])
    assert s.baseline(["leche"]) == 1
    assert len(r.ids) == 1 and len(r.notified) == 1 and n.sent == []


def test_baseline_dry_run_does_not_modify_database():
    r, n = Repo(), Notifier()
    assert AlertService(Client(), r, n).baseline(["leche"], dry_run=True) == 1
    assert r.ids == set() and r.notified == set()


def test_check_after_baseline_has_no_messages():
    r, n = Repo(), Notifier()
    s = AlertService(Client(), r, n)
    s.baseline(["leche"])
    assert s.run(["leche"]) == 0
    assert n.sent == []


def test_check_after_baseline_sends_one_new_message():
    r, n = Repo(), Notifier()
    s = AlertService(Client(), r, n)
    s.baseline(["leche"])
    new = Deal("2", "Puleva leche", "https://y", None, None, None, "milk", None)
    s.client.recent = lambda *a: [new]
    assert s.run(["leche"]) == 1
    assert [d.deal_id for d in n.sent] == ["2"]


class Response:
    def __init__(self, status, headers=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class Session:
    """Offline HTTP transport: scripted responses, never the real network."""

    def __init__(self, value):
        self.value = value
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("timeout")))
        value = self.value.pop(0) if isinstance(self.value, list) else self.value
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value)


def offline_client(session, **kwargs):
    """Client whose backoff is recorded instead of slept."""
    delays = []
    return ChollometroClient(session, sleep=delays.append, **kwargs), delays


@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_errors(status):
    session = Session(status)
    client, delays = offline_client(session)
    with pytest.raises(requests.HTTPError) as excinfo:
        client.search("leche")
    assert isinstance(excinfo.value, ChollometroHTTPError)
    assert excinfo.value.status_code == status
    assert excinfo.value.error_type == f"HTTP_{status}"
    # 403 is permanent; 429/500 are retried within the configured budget.
    if status == 403:
        assert len(session.calls) == 1 and delays == []
    else:
        assert len(session.calls) == 3 and delays == [0.5, 1.0]


def test_timeout():
    session = Session(requests.Timeout())
    client, delays = offline_client(session, retries=1)
    with pytest.raises(requests.Timeout) as excinfo:
        client.search("leche")
    assert isinstance(excinfo.value, ChollometroTimeoutError)
    assert (len(session.calls), delays) == (2, [0.5])


def test_interest_rules():
    from chollometro_alerts.filters import apply_rule

    deal = Deal(
        "x", "Puleva leche", "https://x", Decimal(4), "Amazon", 100, "milk", None
    )
    assert apply_rule(deal, InterestRule("milk", Decimal(5), 90)).reason == "ACCEPTED"
    assert apply_rule(deal, InterestRule("milk", Decimal(3))).reason == "REJECTED_PRICE"
    assert (
        apply_rule(deal, InterestRule("milk", min_temperature=101)).reason
        == "REJECTED_TEMPERATURE"
    )
    assert apply_rule(deal, InterestRule("milk", include_keywords=("puleva",))).accepted
    assert (
        apply_rule(deal, InterestRule("milk", exclude_keywords=("puleva",))).reason
        == "REJECTED_KEYWORD"
    )
    assert apply_rule(
        deal, InterestRule("milk", include_merchants=("amazon",))
    ).accepted
    assert (
        apply_rule(deal, InterestRule("milk", exclude_merchants=("amazon",))).reason
        == "REJECTED_MERCHANT"
    )
    assert apply_rule(deal, InterestRule("milk")).accepted


@pytest.mark.parametrize(
    ("price_per_liter", "accepted"),
    [
        (Decimal("0.79"), True),
        (Decimal("0.799"), True),
        (Decimal("0.80"), False),
        (Decimal("0.81"), False),
        (None, False),
    ],
)
def test_milk_price_per_liter_is_exclusive(price_per_liter, accepted):
    from chollometro_alerts.filters import apply_rule

    deal = Deal(
        "milk-price",
        "Puleva leche",
        "https://x",
        Decimal("1.00"),
        "Amazon",
        100,
        "milk",
        None,
        total_volume_l=Decimal(1),
        price_per_liter=price_per_liter,
    )
    rule = InterestRule("milk", max_price_per_liter=Decimal("0.80"))
    assert apply_rule(deal, rule).accepted is accepted


def test_milk_price_per_liter_is_configurable(monkeypatch):
    from chollometro_alerts.config import load_rules
    from chollometro_alerts.filters import apply_rule

    deal = Deal(
        "milk-config",
        "Puleva leche",
        "https://x",
        Decimal("1.00"),
        "Amazon",
        100,
        "milk",
        None,
        total_volume_l=Decimal(1),
        price_per_liter=Decimal("0.78"),
    )
    monkeypatch.setenv("MILK_MAX_PRICE_PER_LITER", "0.75")
    assert not apply_rule(deal, load_rules()["milk"]).accepted
    monkeypatch.setenv("MILK_MAX_PRICE_PER_LITER", "0.80")
    assert apply_rule(deal, load_rules()["milk"]).accepted


def test_error_alert_cooldown_and_recovery(tmp_path):
    class ErrorNotifier:
        def __init__(self):
            self.alerts = []

        def send_system_alert(self, *args):
            self.alerts.append(args)

    from chollometro_alerts.repository import DealRepository

    r = DealRepository(tmp_path / "errors.db")
    n = ErrorNotifier()
    s = AlertService(Client(), r, n)
    assert s.notify_error("HTTP_429", "ChollometroClient", "busy", 60)
    assert not s.notify_error("HTTP_429", "ChollometroClient", "busy", 60)
    assert len(n.alerts) == 1
    assert s.notify_error("HTTP_429", "ChollometroClient", "busy", -1)
    assert len(n.alerts) == 2
    r.db.close()
