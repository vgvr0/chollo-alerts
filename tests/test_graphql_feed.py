"""Offline tests of the GraphQL discovery provider.

Every test injects a scripted HTTP session and a recording sleep: the suite
never touches the real Chollometro service and never waits for a real backoff.
They pin the handshake (session cookies + XSRF header), the request policy (one
POST per fetch, bounded retries) and the thread -> `Deal` mapping.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import requests

from chollometro_alerts.config import (
    GRAPHQL_DEFAULT_WINDOW,
    ChollometroSettings,
    ConfigurationError,
    GraphQLFeedSettings,
)
from chollometro_alerts.errors import (
    ChollometroGraphQLError,
    ChollometroHTTPError,
    ChollometroParseError,
    ChollometroTimeoutError,
)
from chollometro_alerts.graphql_feed import (
    FEED_OPERATION,
    FEED_QUERY,
    LIMITED_FEED_QUERY,
    GraphQLFeedClient,
    image_url,
    thread_to_deal,
)

PUBLISHED = 1790112449  # 2026-09-22T21:27:29Z, a real value from the endpoint.


class Response:
    def __init__(self, status=200, payload=None, text=None, headers=None):
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self.payload is None:
            raise ValueError("payload is not JSON")
        return self.payload


class Session:
    """Scripted stand-in for `requests.Session` with a cookie jar."""

    def __init__(self, home=None, *posts):
        self.headers = {}
        self.cookies = {}
        self.home = home if home is not None else Response(200, payload={})
        self.posts = list(posts)
        self.gets = []
        self.requests = []

    def get(self, url, **kwargs):
        self.gets.append({"url": url, "timeout": kwargs.get("timeout")})
        if isinstance(self.home, Exception):
            raise self.home
        self.cookies.update(
            {"pepper_session": "%22SESSION%22", "xsrf_t": "%22TOKEN%22"}
        )
        return self.home

    def post(self, url, **kwargs):
        self.requests.append(
            {
                "url": url,
                "headers": kwargs.get("headers"),
                "timeout": kwargs.get("timeout"),
                "body": json.loads(kwargs["data"]),
            }
        )
        if not self.posts:
            raise AssertionError("transport script exhausted")
        result = self.posts.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def thread(thread_id="2012039", **overrides):
    data = {
        "threadId": thread_id,
        "title": f"Chollo {thread_id}",
        "url": f"https://www.chollometro.com/ofertas/chollo-{thread_id}",
        "price": 412.0,
        "nextBestPrice": 950.0,
        "temperature": 165.91,
        "publishedAt": PUBLISHED,
        "status": "Activated",
        "isExpired": False,
        "descriptionPurified": "Vuelos de ida y vuelta",
        "merchant": {"merchantId": "7383", "merchantName": "Google Flights"},
        "groups": [{"threadGroupId": "17", "threadGroupName": "Viajes y ocio"}],
        "mainImage": {"name": f"{thread_id}_1", "path": "threads/raw/n9obc"},
    }
    data.update(overrides)
    return data


def feed(*threads):
    return Response(200, payload={"data": {"threads": list(threads)}})


def make_client(session, **kwargs):
    delays = []
    kwargs.setdefault("settings", ChollometroSettings())
    kwargs.setdefault("feed_settings", GraphQLFeedSettings())
    kwargs.setdefault("sleep", delays.append)
    client = GraphQLFeedClient(session, **kwargs)
    return client, delays


def test_one_handshake_then_one_post_per_fetch():
    session = Session(Response(200, payload={}), feed(thread("1")), feed(thread("2")))
    client, delays = make_client(session)

    first = client.latest()
    second = client.latest()

    assert [deal.deal_id for deal in first] == ["1"]
    assert [deal.deal_id for deal in second] == ["2"]
    # The session is warmed once and reused: one GET for two cycles.
    assert len(session.gets) == 1
    assert session.gets[0]["url"] == "https://www.chollometro.com/"
    assert len(session.requests) == 2
    request = session.requests[0]
    assert request["url"] == "https://www.chollometro.com/graphql"
    assert request["timeout"] == 20
    # The site's axios client sends the URL-decoded cookie value.
    assert request["headers"]["X-XSRF-TOKEN"] == '"TOKEN"'
    assert request["body"]["operationName"] == FEED_OPERATION
    # The production request carries no `limit` argument at all: that is the
    # only way to receive the widest window the endpoint gives (30 threads).
    assert "variables" not in request["body"]
    assert "limit" not in request["body"]["query"]
    assert request["body"]["query"] == FEED_QUERY
    assert delays == []


def test_the_default_query_asks_for_the_feed_without_a_limit():
    query = FEED_QUERY

    assert "threads(filter: {})" in query
    # No `$limit` variable, no `limit:` argument: `limit: 30` would be clamped.
    assert "$limit" not in query
    assert "limit" not in query
    assert FEED_OPERATION == "RecentThreads"


def test_the_server_window_of_thirty_threads_is_returned_as_thirty_deals():
    """The widest answer the endpoint gives, with no `limit` argument sent."""
    threads = [thread(str(2012000 + index)) for index in range(30)]
    session = Session(Response(200, payload={}), feed(*threads))
    client, _delays = make_client(session)

    deals = client.latest()

    assert len(deals) == 30
    assert [deal.deal_id for deal in deals][:2] == ["2012000", "2012001"]
    batch = client.last_feed
    assert batch.received == 30
    assert batch.window_limit is None
    assert batch.expected_window == GRAPHQL_DEFAULT_WINDOW == 30
    assert batch.server_window_full is True
    # A saturated default window is not an explicit window: nothing to flag.
    assert batch.window_full is False
    assert "variables" not in session.requests[0]["body"]


def test_an_explicit_window_up_to_twenty_is_sent_as_a_limit_variable():
    session = Session(Response(200, payload={}), feed(thread("1")))
    client, _delays = make_client(
        session, feed_settings=GraphQLFeedSettings(window_limit=5)
    )

    client.latest()

    request = session.requests[0]
    assert request["body"]["query"] == LIMITED_FEED_QUERY
    assert request["body"]["operationName"] == "RecentThreadsWithLimit"
    assert request["body"]["variables"] == {"limit": 5}
    assert client.last_feed.window_limit == 5


def test_an_explicit_window_above_the_server_cap_is_never_sent():
    """`limit: 30` would come back as 20, so the client refuses it outright."""
    session = Session(Response(200, payload={}))
    client, _delays = make_client(session)

    with pytest.raises(ValueError, match="entre 1 y 20"):
        client.latest(limit=30)

    assert session.gets == []
    assert session.requests == []
    assert client.last_feed is None


def test_duplicate_thread_ids_in_one_window_are_kept_once():
    session = Session(
        Response(200, payload={}),
        feed(thread("7"), thread("8"), thread("7"), thread("9"), thread("8")),
    )
    client, _delays = make_client(session)

    deals = client.latest()

    assert [deal.deal_id for deal in deals] == ["7", "8", "9"]
    assert client.last_feed.received == 3


def test_the_session_is_rehandshaked_when_the_cookie_lifetime_expires():
    now = [datetime(2026, 9, 22, 12, 0, tzinfo=UTC)]
    session = Session(
        Response(200, payload={}),
        feed(thread("1")),
        feed(thread("1")),
        feed(thread("1")),
    )
    client, _delays = make_client(
        session, clock=lambda: now[0], session_ttl_seconds=1800
    )

    client.latest()
    now[0] += timedelta(seconds=1799)
    client.latest()
    assert len(session.gets) == 1

    now[0] += timedelta(seconds=2)
    client.latest()
    assert len(session.gets) == 2


def test_a_stale_session_is_renewed_once_before_giving_up():
    session = Session(Response(200, payload={}), Response(403), feed(thread("1")))
    client, _delays = make_client(session, retries=1)

    deals = client.latest()

    assert [deal.deal_id for deal in deals] == ["1"]
    assert len(session.gets) == 2
    assert len(session.requests) == 2


def test_graphql_errors_are_typed_and_never_retried():
    session = Session(
        Response(200, payload={}),
        Response(200, payload={"errors": [{"message": "Field 'x' is not defined"}]}),
    )
    client, delays = make_client(session, retries=2)

    with pytest.raises(ChollometroGraphQLError) as excinfo:
        client.latest()

    assert excinfo.value.error_type == "GRAPHQL_ERROR"
    assert "Field 'x' is not defined" in str(excinfo.value)
    assert len(session.requests) == 1
    assert delays == []


def test_transient_http_failures_are_retried_up_to_the_budget():
    session = Session(
        Response(200, payload={}), Response(503), Response(503), Response(503)
    )
    client, delays = make_client(session, retries=2)

    with pytest.raises(ChollometroHTTPError) as excinfo:
        client.latest()
    assert excinfo.value.error_type == "HTTP_503"
    assert len(session.requests) == 3
    assert delays == [0.5, 1.0]


def test_rate_limits_honour_retry_after():
    session = Session(
        Response(200, payload={}),
        Response(429, headers={"Retry-After": "3"}),
        feed(thread("1")),
    )
    client, delays = make_client(session, retries=1)

    assert [deal.deal_id for deal in client.latest()] == ["1"]
    assert delays == [3.0]


@pytest.mark.parametrize(
    "response",
    [
        Response(200, text="<html>Please verify you are human</html>", payload=None),
        Response(200, payload={"data": {}}),
        Response(200, payload={"data": {"threads": "nope"}}),
    ],
)
def test_unexpected_payloads_are_parse_errors(response):
    session = Session(Response(200, payload={}), response)
    client, _delays = make_client(session, retries=0)

    with pytest.raises(ChollometroParseError):
        client.latest()


def test_timeouts_are_typed_provider_failures():
    session = Session(
        Response(200, payload={}), requests.Timeout("slow"), requests.Timeout("slow")
    )
    client, delays = make_client(session, retries=1)

    with pytest.raises(ChollometroTimeoutError):
        client.latest()
    assert delays == [0.5]


def test_a_failing_handshake_is_a_provider_failure():
    session = Session(Response(503))
    client, delays = make_client(session, retries=1)

    with pytest.raises(ChollometroHTTPError) as excinfo:
        client.latest()

    assert excinfo.value.error_type == "HTTP_503"
    assert len(session.requests) == 0
    assert delays == [0.5]


def test_thread_to_deal_maps_every_shared_field():
    deal = thread_to_deal(thread("2012039"))

    assert deal.deal_id == "2012039"
    assert deal.title == "Chollo 2012039"
    assert deal.url.endswith("chollo-2012039")
    assert deal.price == Decimal("412.0")
    assert deal.original_price == Decimal("950.0")
    assert deal.temperature == 166
    assert deal.merchant == "Google Flights"
    assert deal.published_at == datetime(2026, 9, 22, 21, 27, 29, tzinfo=UTC)
    assert deal.description == "Vuelos de ida y vuelta"
    assert deal.status == "Activated" and deal.is_expired is False
    assert deal.category == "generic"
    assert "Viajes y ocio" in deal.product_text
    assert deal.image == (
        "https://static.chollometro.com/threads/raw/n9obc/2012039_1"
        "/fs/895x577/qt/65/2012039_1.jpg"
    )


def test_thread_to_deal_uses_the_shared_category_rules():
    assert thread_to_deal(thread("1", title="Leche entera 6x1L")).category == "milk"
    assert (
        thread_to_deal(thread("2", title="Cerveza Mahou 24 latas")).category == "beer"
    )


def test_thread_to_deal_tolerates_missing_optional_fields():
    deal = thread_to_deal(
        thread(
            "9",
            price=None,
            nextBestPrice=None,
            temperature=None,
            publishedAt=None,
            descriptionPurified=None,
            merchant=None,
            groups=None,
            mainImage=None,
            status=None,
            isExpired=None,
        )
    )

    assert deal.price is None and deal.original_price is None
    assert deal.temperature is None and deal.published_at is None
    assert deal.merchant is None and deal.image is None
    assert deal.description == "" and deal.status is None
    assert deal.is_expired is None


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"threadId": None, "title": "x", "url": "https://x"},
        {"threadId": "1", "title": "", "url": "https://x"},
        {"threadId": "1", "title": "x", "url": ""},
        "not-a-dict",
    ],
)
def test_unusable_threads_are_dropped(broken):
    assert thread_to_deal(broken) is None


def test_image_url_needs_both_parts():
    assert image_url(None) is None
    assert image_url({"name": "a"}) is None
    assert image_url({"path": "threads/raw/x"}) is None


def test_the_batch_reports_its_own_window():
    session = Session(
        Response(200, payload={}),
        feed(
            thread("1", publishedAt=PUBLISHED),
            thread("2", publishedAt=PUBLISHED + 60),
            thread("3", publishedAt=PUBLISHED + 120),
        ),
    )
    now = datetime(2026, 9, 22, 22, 0, tzinfo=UTC)
    client, _delays = make_client(session, clock=lambda: now)

    client.latest()

    batch = client.last_feed
    assert batch.received == 3
    # No `limit` was requested: the batch reports the server's own window.
    assert batch.window_limit is None
    assert batch.expected_window == GRAPHQL_DEFAULT_WINDOW
    assert batch.window_full is False
    assert batch.oldest_published_at == datetime(2026, 9, 22, 21, 27, 29, tzinfo=UTC)
    assert batch.newest_published_at == datetime(2026, 9, 22, 21, 29, 29, tzinfo=UTC)
    assert batch.oldest_age_seconds == pytest.approx(1951.0)
    assert batch.xsrf_present is True


def test_a_batch_that_fills_the_explicit_window_is_reported_as_full():
    session = Session(
        Response(200, payload={}),
        feed(*(thread(str(index)) for index in range(2))),
    )
    client, _delays = make_client(
        session, feed_settings=GraphQLFeedSettings(window_limit=2)
    )

    client.latest()

    assert client.last_feed.window_full is True


def test_a_default_window_of_thirty_is_not_an_explicit_window():
    """A saturated 30-thread answer must not look like a full explicit window."""
    session = Session(
        Response(200, payload={}),
        feed(*(thread(str(index)) for index in range(GRAPHQL_DEFAULT_WINDOW))),
    )
    client, _delays = make_client(session)

    client.latest()

    batch = client.last_feed
    assert batch.received == GRAPHQL_DEFAULT_WINDOW
    assert batch.window_full is False
    assert batch.server_window_full is True


# --- Configuration of the window ----------------------------------------- #


def _feed_settings(monkeypatch, window=None):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    monkeypatch.delenv("CHOLLOMETRO_GRAPHQL_WINDOW_LIMIT", raising=False)
    if window is not None:
        monkeypatch.setenv("CHOLLOMETRO_GRAPHQL_WINDOW_LIMIT", window)
    return GraphQLFeedSettings.from_env()


def test_the_default_configuration_omits_the_limit_argument(monkeypatch):
    assert _feed_settings(monkeypatch).window_limit is None


def test_an_explicit_small_window_is_accepted(monkeypatch):
    assert _feed_settings(monkeypatch, "5").window_limit == 5


@pytest.mark.parametrize("value", ["0", "-1", "21", "30"])
def test_a_window_outside_one_to_twenty_is_rejected(monkeypatch, value):
    with pytest.raises(ConfigurationError):
        _feed_settings(monkeypatch, value)
