from decimal import Decimal
from unittest.mock import Mock

import pytest

from chollometro_alerts.config import InterestRule
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.models import Deal
from chollometro_alerts.product import ProductExtraction
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.service import AlertService
from chollometro_alerts.telegram import TelegramNotifier
from chollometro_alerts.telegram_rules import TelegramRuleController


def deal(
    *,
    price="200",
    units=None,
    price_per_unit=None,
    price_per_liter=None,
    total_volume_l=None,
):
    return Deal(
        "d",
        "producto",
        "https://example.test/d",
        Decimal(price) if price is not None else None,
        "Amazon",
        100,
        "generic",
        None,
        units=units,
        total_volume_l=total_volume_l,
        price_per_unit=price_per_unit,
        price_per_liter=price_per_liter,
        product_extraction=ProductExtraction(),
    )


@pytest.mark.parametrize(
    "price, accepted", [("199.99", True), ("200", False), ("200.01", False)]
)
def test_strict_total_price_boundary_and_evidence(price, accepted):
    result = apply_rule(
        deal(price=price), InterestRule("generic", max_price=Decimal(200))
    )
    assert result.accepted is accepted
    if accepted:
        assert result.checks[-1].detail == "199,99 € < 200 €"


def test_unknown_total_price_is_not_a_match():
    result = apply_rule(
        deal(price=None), InterestRule("generic", max_price=Decimal(200))
    )
    assert (result.accepted, result.reason) == (False, "REJECTED_UNKNOWN_PRICE")


def test_strict_unit_and_liter_evidence_uses_less_than():
    unit = apply_rule(
        deal(price="10", units=2, price_per_unit=Decimal("4.99")),
        InterestRule("generic", max_price_per_unit=Decimal(5)),
    )
    liter = apply_rule(
        deal(price="10", price_per_liter=Decimal("0.99"), total_volume_l=Decimal(10)),
        InterestRule("generic", max_price_per_liter=Decimal(1)),
    )
    assert unit.checks[-1].detail == "4,99 €/ud < 5 €/ud"
    assert liter.checks[-1].detail == "0,99 €/L < 1 €/L"


def test_telegram_notifier_retries_transient_status_without_real_network(monkeypatch):
    class Response:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"Retry-After": "3"}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    responses = iter([Response(429), Response(200)])
    calls = []
    sleeps = []
    monkeypatch.setattr(
        "chollometro_alerts.telegram.requests.post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or next(responses),
    )
    TelegramNotifier("token", "chat", sleep=sleeps.append).send(deal())
    assert len(calls) == 2
    assert sleeps == [3]


def test_failed_telegram_update_is_retryable(tmp_path):
    repository = DealRepository(tmp_path / "alerts.sqlite3")
    controller = TelegramRuleController(
        bot_token="token",
        authorized_chat_id="7",
        repository=repository,
        translator=None,
    )
    controller._reply_to = lambda text: "ok"
    attempts = iter([RuntimeError("temporary"), None])

    def send_message(text):
        failure = next(attempts)
        if failure is not None:
            raise failure

    controller.send_message = send_message
    update = {"update_id": 91, "message": {"chat": {"id": 7}, "text": "x"}}
    with pytest.raises(RuntimeError):
        controller.process_update(update)
    assert controller.process_update(update) == "ok"
    assert repository.claim_telegram_update(91) is False


def test_rule_lookup_does_not_use_first_rule_as_query_fallback(tmp_path):
    client = Mock()
    service = AlertService(client, DealRepository(tmp_path / "alerts.sqlite3"), Mock())
    service.client.recent.return_value = []
    assert service.run(["unknown-query"], rules={"other": InterestRule("other")}) == 0
    service.client.recent.assert_not_called()
