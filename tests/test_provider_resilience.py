"""Offline HTTP policy tests for the Chollometro provider.

Every test injects a scripted transport and a recording sleep: the suite never
touches the real Chollometro service and never waits for a real backoff.
"""

import pytest
import requests

from chollometro_alerts.client import ChollometroClient
from chollometro_alerts.config import ChollometroSettings
from chollometro_alerts.errors import (
    SCAN_FAILED,
    SCAN_PARTIAL,
    SCAN_SUCCESS,
    ChollometroHTTPError,
    ChollometroNetworkError,
    ChollometroParseError,
    ChollometroRateLimitError,
    ChollometroTimeoutError,
)

ITEM = (
    '<article id="thread_{deal_id}">'
    '<a class="thread-title" href="/ofertas/leche-{deal_id}">Leche entera {deal_id}</a>'
    '<span class="thread-price">12,50€</span></article>'
)
# A results page with no items: the wording that proves the search worked.
EMPTY_RESULTS = (
    '<div class="threadList">No hemos encontrado resultados para tu búsqueda</div>'
)
# A 200 response that is not a Chollometro results page at all.
UNEXPECTED_PAYLOAD = "<html><body>Please verify you are human</body></html>"


def page(*deal_ids):
    items = "".join(ITEM.format(deal_id=deal_id) for deal_id in deal_ids)
    return f'<html><body><div class="threadList">{items}</div></body></html>'


class Response:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class Transport:
    """Scripted offline transport: each call consumes the next response."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.headers = {}
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append({"url": url, "timeout": kwargs.get("timeout")})
        if not self.responses:
            raise AssertionError("transport script exhausted")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_client(transport, **kwargs):
    """Client with an isolated HTTP policy and a recording sleep."""
    kwargs.setdefault("settings", ChollometroSettings())
    delays = []
    return ChollometroClient(transport, sleep=delays.append, **kwargs), delays


def test_every_request_carries_the_centralized_timeout():
    transport = Transport(Response(200, page(1)))
    client, delays = make_client(transport)

    assert [deal.deal_id for deal in client.search("leche")] == ["1"]

    assert client.timeout == 20
    assert transport.requests == [
        {"url": "https://www.chollometro.com/search?q=leche", "timeout": 20}
    ]
    assert delays == []
    assert client.last_search == {
        "query": "leche",
        "page": 1,
        "http_status": 200,
        "fetched_items": 1,
        "parsed_items": 1,
    }


def test_successful_empty_scan_is_a_success_not_a_failure():
    transport = Transport(Response(200, EMPTY_RESULTS))
    client, delays = make_client(transport)

    assert client.recent(["leche"]) == []

    outcome = client.last_scan
    assert outcome.status == SCAN_SUCCESS
    assert outcome.error is None and outcome.error_type is None
    assert (outcome.pages_fetched, outcome.pages_requested) == (1, 1)
    assert (outcome.fetched_items, outcome.parsed_items, outcome.deals) == (0, 0, ())
    assert len(transport.requests) == 1 and delays == []


@pytest.mark.parametrize("payload", [UNEXPECTED_PAYLOAD, "", "   "])
def test_unexpected_payload_is_a_parse_error_never_an_empty_result(payload):
    transport = Transport(Response(200, payload))
    client, delays = make_client(transport)

    with pytest.raises(ChollometroParseError) as excinfo:
        client.recent(["leche"])

    assert excinfo.value.error_type == "PARSE_ERROR"
    # Payload failures are permanent: one request, no retry, no sleep.
    assert len(transport.requests) == 1 and delays == []
    assert client.last_search == {}
    assert client.last_scan.status == SCAN_FAILED
    assert client.last_scan.error_type == "PARSE_ERROR"
    assert client.last_scan.deals == ()


def test_timeout_fails_after_bounded_retries():
    transport = Transport(
        requests.Timeout(),
        requests.Timeout(),
        requests.Timeout(),
        Response(200, page(1)),
    )
    client, delays = make_client(transport, retries=2)

    with pytest.raises(ChollometroTimeoutError) as excinfo:
        client.recent(["leche"])

    error = excinfo.value
    assert error.retryable and error.attempt == 3 and error.error_type == "TIMEOUT"
    assert isinstance(error, requests.Timeout)
    assert isinstance(error.__cause__, requests.Timeout)
    assert len(transport.requests) == 3 and delays == [0.5, 1.0]
    # The scripted fourth response is never consumed: retries are bounded.
    assert client.last_scan.status == SCAN_FAILED
    assert client.last_scan.error_type == "TIMEOUT"


def test_connection_error_is_retried_then_fails_as_a_network_error():
    transport = Transport(requests.ConnectionError(), requests.ConnectionError())
    client, delays = make_client(transport, retries=1)

    with pytest.raises(ChollometroNetworkError) as excinfo:
        client.recent(["leche"])

    assert isinstance(excinfo.value, requests.ConnectionError)
    assert excinfo.value.error_type == "NETWORK_ERROR"
    assert len(transport.requests) == 2 and delays == [0.5]
    assert client.last_scan.status == SCAN_FAILED
    assert client.last_scan.error_type == "NETWORK_ERROR"


def test_rate_limit_honours_retry_after_then_recovers():
    transport = Transport(
        Response(429, headers={"Retry-After": "2"}), Response(200, page(1))
    )
    client, delays = make_client(transport)

    assert [deal.deal_id for deal in client.recent(["leche"])] == ["1"]

    assert delays == [2.0]
    assert client.last_scan.status == SCAN_SUCCESS


def test_retry_after_is_capped_by_the_configured_maximum():
    settings = ChollometroSettings(
        timeout=5, retries=2, backoff_seconds=0.5, max_backoff_seconds=3
    )
    transport = Transport(
        Response(429, headers={"Retry-After": "600"}), Response(200, page(1))
    )
    client, delays = make_client(transport, settings=settings)

    client.recent(["leche"])

    assert delays == [3]


def test_permanent_rate_limit_fails_after_the_retry_budget():
    transport = Transport(Response(429), Response(429), Response(429))
    client, delays = make_client(transport)

    with pytest.raises(ChollometroRateLimitError) as excinfo:
        client.recent(["leche"])

    assert excinfo.value.error_type == "HTTP_429"
    assert isinstance(excinfo.value, ChollometroHTTPError)
    assert len(transport.requests) == 3 and delays == [0.5, 1.0]
    assert client.last_scan.status == SCAN_FAILED
    assert client.last_scan.http_status == 429


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_5xx_recovers_within_the_retry_budget(status):
    transport = Transport(Response(status), Response(status), Response(200, page(1)))
    client, delays = make_client(transport)

    assert [deal.deal_id for deal in client.recent(["leche"])] == ["1"]

    assert (len(transport.requests), delays) == (3, [0.5, 1.0])
    assert client.last_scan.status == SCAN_SUCCESS


def test_permanent_503_fails_as_http_503():
    transport = Transport(Response(503), Response(503), Response(503))
    client, delays = make_client(transport)

    with pytest.raises(ChollometroHTTPError) as excinfo:
        client.recent(["leche"])

    assert excinfo.value.error_type == "HTTP_503"
    assert excinfo.value.status_code == 503
    assert len(transport.requests) == 3 and delays == [0.5, 1.0]
    assert client.last_scan.status == SCAN_FAILED
    assert client.last_scan.error_type == "HTTP_503"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_4xx_is_not_retried(status):
    transport = Transport(Response(status), Response(200, page(1)))
    client, delays = make_client(transport)

    with pytest.raises(ChollometroHTTPError) as excinfo:
        client.recent(["leche"])

    assert excinfo.value.error_type == f"HTTP_{status}"
    assert not excinfo.value.retryable
    assert len(transport.requests) == 1 and delays == []
    assert client.last_scan.status == SCAN_FAILED
    assert client.last_scan.http_status == status


def test_partial_pagination_is_reported_as_partial():
    transport = Transport(
        Response(200, page(1, 2)), Response(503), Response(503), Response(503)
    )
    client, delays = make_client(transport)

    with pytest.raises(ChollometroHTTPError):
        client.recent(["leche"], pages=2)

    outcome = client.last_scan
    assert outcome.status == SCAN_PARTIAL
    assert [deal.deal_id for deal in outcome.deals] == ["1", "2"]
    assert (outcome.pages_fetched, outcome.pages_requested) == (1, 2)
    assert outcome.error_type == "HTTP_503"
    # Page 2 was retried inside its budget; page 1 was never re-fetched.
    assert [request["url"] for request in transport.requests] == [
        "https://www.chollometro.com/search?q=leche",
        "https://www.chollometro.com/search?q=leche&page=2",
        "https://www.chollometro.com/search?q=leche&page=2",
        "https://www.chollometro.com/search?q=leche&page=2",
    ]
    assert delays == [0.5, 1.0]


def test_empty_first_page_then_failure_is_still_partial():
    transport = Transport(Response(200, EMPTY_RESULTS), Response(503))
    client, _delays = make_client(transport, retries=0)

    with pytest.raises(ChollometroHTTPError):
        client.recent(["leche"], pages=2)

    assert client.last_scan.status == SCAN_PARTIAL
    assert client.last_scan.deals == ()
    assert client.last_scan.pages_fetched == 1


def test_parse_error_on_a_later_page_is_partial_and_not_an_empty_scan():
    transport = Transport(Response(200, page(1)), Response(200, UNEXPECTED_PAYLOAD))
    client, delays = make_client(transport, retries=2)

    with pytest.raises(ChollometroParseError):
        client.recent(["leche"], pages=2)

    outcome = client.last_scan
    assert outcome.status == SCAN_PARTIAL
    assert outcome.error_type == "PARSE_ERROR"
    assert [deal.deal_id for deal in outcome.deals] == ["1"]
    assert len(transport.requests) == 2 and delays == []
