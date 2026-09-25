"""GraphQL discovery feed: the newest Chollometro threads as `Deal` objects.

Second provider beside the HTML client. It speaks to the site's internal Pepper
GraphQL API with a persistent session: one GET to obtain the session cookies
(`pepper_session`, `xsrf_t`), then one POST per cycle with the field selection
validated against the live schema. Everything downstream — pricing, rules,
Telegram — consumes the same `Deal` model the HTML parser produces, so nothing
else in the pipeline knows which provider answered.

Two properties matter for the scanner and are implemented here:

* the fetch is a single request per cycle with the client's bounded retry
  policy (no per-alert requests, no unbounded loop);
* the batch carries its own window metrics (how many threads came back, how old
  the oldest one is), which is what lets the cycle warn. A saturated window is
  not that signal — with `limit` omitted the endpoint always fills it — so the
  warning reacts to a window that overlaps nothing already recorded instead.

Window semantics, measured against the live endpoint (2026-09-23):

* `threads(filter: {})` **without** `limit` returns 30 threads. That is the
  production default here, because it is the widest answer the endpoint gives.
* `limit` between 1 and 20 returns exactly that many threads.
* `limit` of 21 or more is silently clamped to 20 — no GraphQL error, no
  warning — so `limit: 30` is *not* the way to ask for 30 and is never sent.
* `threads` has no cursor/offset/page argument, no `pageInfo`/`hasNextPage` and
  no temporal filter. `threadId: {in: [...]}` works; `gt`/`lt`/`ge`/`le` are
  accepted but ignored, and `sort` is accepted but does not change the order.

Gap recovery (DESIGNED, NOT IMPLEMENTED). The only verified way to look past
the newest window is to ask for ids explicitly, so a future recovery pass can
be built without new schema surface:

1. Detect the loss signal: after a mature cycle, the window holds threads but
   none of them is already in `feed_threads` (`overlap == 0`).
2. Take `floor_id` = smallest `threadId` in `feed_threads` and ask for
   `threads(filter: {threadId: {in: [floor_id - 1, ..., floor_id - N]}})`
   (no `limit`, no `gt`/`lt`, no `sort`), newest-first as always.
3. Repeat with `floor_id` = smallest id of the previous answer. Ids are *not*
   dense (roughly 13-15 of every 20 consecutive ids exist), so `N` has to be
   several times the number of threads wanted per window.
4. Stop as soon as an answer contains an id already present in `feed_threads`,
   i.e. the recovered tail overlaps the known history. A numeric jump is never
   treated as a missing deal on its own.

Recovery costs one request per window (bounded by a small constant) and only
ever widens the tail; it must never mark a thread as seen before it is
evaluated. It is deliberately left out of this change until the restart/no-
overlap case is observed in production.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote

import requests

from .categories import CategoryRef
from .client import RATE_LIMIT_STATUS, RETRYABLE_STATUS
from .config import (
    GRAPHQL_DEFAULT_WINDOW,
    GRAPHQL_MAX_EXPLICIT_WINDOW,
    ChollometroSettings,
    GraphQLFeedSettings,
)
from .errors import (
    ChollometroError,
    ChollometroGraphQLError,
    ChollometroHTTPError,
    ChollometroNetworkError,
    ChollometroParseError,
    ChollometroRateLimitError,
    ChollometroTimeoutError,
)
from .filters import category_for
from .models import Deal

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.chollometro.com"
USER_AGENT = "chollo-alerts/0.1 (+https://www.chollometro.com)"
XSRF_COOKIE = "xsrf_t"
SESSION_COOKIE = "pepper_session"

# HTTP statuses that mean "the session cookies are stale" rather than "the
# request is wrong". They get one fresh handshake before giving up.
STALE_SESSION_STATUS = frozenset({403, 419})

# The site builds card images from `mainImage.path` + `mainImage.name`; the same
# template is used by the image mark-up the API returns inside descriptions.
IMAGE_URL_TEMPLATE = (
    "https://static.chollometro.com/{path}/{name}/fs/895x577/qt/65/{name}.jpg"
)

# Only the fields the `Deal` model and the window metrics need. The selection
# was validated field by field against the live schema (introspection is
# disabled, so an unknown field is rejected instead of ignored).
_FEED_SELECTION = """
threadId
title
url
price
nextBestPrice
temperature
publishedAt
status
isExpired
descriptionPurified(maxLength: 400)
merchant {
  merchantId
  merchantName
  merchantUrlName
}
groups {
  threadGroupId
  threadGroupName
  threadGroupUrlName
}
mainImage {
  name
  path
}
""".strip("\n")


def _feed_query(operation_name, arguments):
    """Build a feed query from the one shared selection, so the two variants
    (with and without `limit`) can never drift apart."""
    selection = "\n".join(
        f"    {line}" if line.strip() else "" for line in _FEED_SELECTION.splitlines()
    )
    return (
        f"query {operation_name} {{\n  threads({arguments}) {{\n{selection}\n  }}\n}}"
    )


# Production default: `threads(filter: {})` with **no `limit` argument at all**.
# The server then answers with its own window of 30 threads. `limit: 30` is not
# an equivalent request — an explicit limit above 20 is silently clamped to 20 —
# so the argument has to be absent, not set to the same number.
FEED_QUERY = _feed_query("RecentThreads", "filter: {}")
FEED_OPERATION = "RecentThreads"

# Explicit narrow window (1..20), kept for the rare case where fewer threads are
# wanted per cycle. The client rejects anything above 20 instead of sending it.
LIMITED_FEED_QUERY = _feed_query("RecentThreadsWithLimit", "filter: {}, limit: $limit")
LIMITED_FEED_OPERATION = "RecentThreadsWithLimit"


@dataclass(frozen=True)
class FeedBatch:
    """One fetch of the discovery feed, with the metrics of its window.

    `window_limit` is the explicit `limit` that was requested, or `None` when
    the request carried no `limit` argument (the production default, which is
    what makes the server answer with its widest window).
    """

    deals: tuple[Deal, ...]
    window_limit: int | None
    xsrf_present: bool
    fetched_at: datetime

    @property
    def received(self) -> int:
        return len(self.deals)

    @property
    def expected_window(self) -> int:
        """How wide the window should be: the explicit limit or the default 30."""
        if self.window_limit is None:
            return GRAPHQL_DEFAULT_WINDOW
        return self.window_limit

    @property
    def oldest_published_at(self) -> datetime | None:
        stamps = [deal.published_at for deal in self.deals if deal.published_at]
        return min(stamps) if stamps else None

    @property
    def newest_published_at(self) -> datetime | None:
        stamps = [deal.published_at for deal in self.deals if deal.published_at]
        return max(stamps) if stamps else None

    @property
    def oldest_age_seconds(self) -> float | None:
        oldest = self.oldest_published_at
        if oldest is None:
            return None
        return (self.fetched_at - oldest).total_seconds()

    @property
    def window_full(self) -> bool:
        """The batch filled the *explicitly requested* window.

        Only meaningful for a `limit` between 1 and 20. With the argument
        omitted the server picks the window (30) and a saturated answer is the
        normal case, so it carries no information about lost threads.
        """
        return self.window_limit is not None and self.received >= self.window_limit

    @property
    def server_window_full(self) -> bool:
        """The answer saturated the server-side default window (no `limit`)."""
        return self.window_limit is None and self.received >= GRAPHQL_DEFAULT_WINDOW


def _price(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _published_at(value):
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def image_url(main_image) -> str | None:
    """Rebuild the card image URL from the API's `path`/`name` pair."""
    if not isinstance(main_image, dict):
        return None
    name, path = main_image.get("name"), main_image.get("path")
    if not name or not path:
        return None
    return IMAGE_URL_TEMPLATE.format(path=str(path).strip("/"), name=name)


def thread_to_deal(thread: dict, source_query: str = "") -> Deal | None:
    """Map one GraphQL thread to the provider-neutral `Deal` model.

    `threadId` is the deal identity, exactly the value the HTML parser reads
    from `article[id="thread_<id>"]`, so `seen`/`new` bookkeeping is shared.
    `nextBestPrice` becomes `original_price` and `publishedAt` (epoch seconds)
    becomes an aware UTC timestamp. Returns None when the payload has no usable
    identity or body, so a malformed row can never enter the pipeline.
    """
    if not isinstance(thread, dict):
        return None
    thread_id = thread.get("threadId")
    title = (thread.get("title") or "").strip()
    url = (thread.get("url") or "").strip()
    if thread_id in (None, "") or not title or not url:
        return None
    merchant = thread.get("merchant") or {}
    merchant_name = (merchant.get("merchantName") or "").strip() or None
    description = (thread.get("descriptionPurified") or "").strip()
    groups = [
        (group.get("threadGroupName") or "").strip()
        for group in thread.get("groups") or []
        if isinstance(group, dict)
    ]
    category_refs = tuple(
        CategoryRef(
            id=str(group.get("threadGroupId"))
            if group.get("threadGroupId") is not None
            else None,
            slug=group.get("threadGroupUrlName"),
            name=group.get("threadGroupName"),
        )
        for group in (thread.get("groups") or [])
        if isinstance(group, dict)
        and (group.get("threadGroupId") or group.get("threadGroupName"))
    )
    temperature = thread.get("temperature")
    product_text = " ".join(
        part
        for part in (title, description, merchant_name, " ".join(filter(None, groups)))
        if part
    )
    return Deal(
        str(thread_id),
        title,
        url,
        _price(thread.get("price")),
        merchant_name,
        round(float(temperature)) if isinstance(temperature, (int, float)) else None,
        category_for(title, source_query),
        _published_at(thread.get("publishedAt")),
        product_text=product_text,
        description=description,
        original_price=_price(thread.get("nextBestPrice")),
        image=image_url(thread.get("mainImage")),
        source_query=source_query,
        status=thread.get("status"),
        is_expired=thread.get("isExpired"),
        categories=category_refs,
    )


class GraphQLFeedClient:
    """Fetch the newest Chollometro threads through the internal GraphQL API."""

    def __init__(
        self,
        session=None,
        base_url=DEFAULT_BASE_URL,
        timeout=None,
        retries=None,
        settings=None,
        feed_settings=None,
        sleep=None,
        clock=None,
        session_ttl_seconds=1800,
    ):
        self.feed_settings = feed_settings or GraphQLFeedSettings.from_env()
        self.settings = settings or ChollometroSettings.from_env()
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.home_url = f"{self.base_url}/"
        self.endpoint = f"{self.base_url}{self.feed_settings.path}"
        self.window_limit = self.feed_settings.window_limit
        # Same HTTP policy as the HTML client: one timeout, bounded retries.
        self.timeout = self.settings.timeout if timeout is None else timeout
        self.retries = self.settings.retries if retries is None else retries
        if self.retries < 0:
            raise ValueError("retries debe ser >= 0")
        self.backoff = self.settings.backoff_seconds
        self.max_backoff = self.settings.max_backoff_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self._sleep = sleep or time.sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._session_started_at = None
        self._xsrf_present = False
        # Result of the last fetch: the scanner reads its window metrics.
        self.last_feed: FeedBatch | None = None
        self.last_http_status = None

    def latest(self, limit=None):
        """Return the newest threads as `Deal` objects (one request per cycle).

        By default the request carries **no `limit` argument** (`threads(filter:
        {})`), which is the only way to receive the endpoint's widest window
        (30 threads). An explicit `limit` between 1 and 20 is still honoured for
        the rare case where a narrower window is wanted; anything above 20 is
        rejected before the request, because the server would silently clamp it
        to 20 and the caller would never learn it.
        """
        window = self.window_limit if limit is None else limit
        if window is not None and not 1 <= window <= GRAPHQL_MAX_EXPLICIT_WINDOW:
            raise ValueError(
                f"limit debe estar entre 1 y {GRAPHQL_MAX_EXPLICIT_WINDOW}, o "
                "omitirse: el endpoint recorta a 20 cualquier limit mayor y sin "
                f"limit devuelve {GRAPHQL_DEFAULT_WINDOW}"
            )
        payload = self._payload(window)
        for attempt in range(1, self.retries + 2):
            try:
                response = self._post(payload, attempt)
                threads = self._threads(response, attempt)
            except ChollometroError as exc:
                if self._retry(attempt, exc):
                    continue
                raise
            deals = self._deals(threads)
            self.last_feed = FeedBatch(
                deals=deals,
                window_limit=window,
                xsrf_present=self._xsrf_present,
                fetched_at=self._clock(),
            )
            return list(deals)
        raise AssertionError("retry loop must return or raise")

    @staticmethod
    def _payload(window):
        """Serialize the request: the default body has no `variables` at all."""
        if window is None:
            body = {"query": FEED_QUERY, "operationName": FEED_OPERATION}
        else:
            body = {
                "query": LIMITED_FEED_QUERY,
                "operationName": LIMITED_FEED_OPERATION,
                "variables": {"limit": window},
            }
        return json.dumps(body)

    @staticmethod
    def _deals(threads):
        """Map threads to deals, keeping the first occurrence of every thread id.

        The window is ordered newest-first, so the first occurrence is the one
        that counts and a repeated `threadId` can never be evaluated (or
        notified) twice in the same cycle.
        """
        deals = []
        seen = set()
        for thread in threads:
            deal = thread_to_deal(thread)
            if deal is None or deal.deal_id in seen:
                continue
            seen.add(deal.deal_id)
            deals.append(deal)
        return tuple(deals)

    def _retry(self, attempt, error):
        """Retry only transient failures, never more than the configured budget."""
        retryable = error.retryable or error.status_code in STALE_SESSION_STATUS
        if not retryable or attempt > self.retries:
            return False
        if error.status_code in STALE_SESSION_STATUS:
            # A stale cookie is fixed by a new handshake, not by waiting.
            self._session_started_at = None
        delay = self._delay(attempt, self._retry_after(error.response))
        logger.warning(
            "feed_request_retry http_status=%s error_type=%s attempt=%s "
            "retry_in_seconds=%.3f",
            error.status_code,
            error.error_type,
            attempt,
            delay,
        )
        self._sleep(delay)
        return True

    def _post(self, payload, attempt):
        self._ensure_session()
        try:
            return self.session.post(
                self.endpoint,
                headers=self._headers(),
                data=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise ChollometroTimeoutError(
                f"Chollometro GraphQL timeout after {self.timeout:g}s",
                attempt=attempt,
            ) from exc
        except requests.RequestException as exc:
            error = ChollometroNetworkError(
                f"Chollometro GraphQL {type(exc).__name__}", attempt=attempt
            )
            error.retryable = isinstance(exc, requests.ConnectionError)
            raise error from exc

    def _threads(self, response, attempt):
        status = getattr(response, "status_code", None)
        self.last_http_status = status
        if status is not None and status >= 400:
            raise self._http_error(response, status, attempt)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ChollometroParseError(
                "Chollometro GraphQL payload is not JSON",
                attempt=attempt,
                status_code=status,
                response=response,
            ) from exc
        if not isinstance(payload, dict):
            raise ChollometroParseError(
                "Chollometro GraphQL payload is not an object",
                attempt=attempt,
                status_code=status,
                response=response,
            )
        errors = payload.get("errors")
        if errors:
            raise ChollometroGraphQLError(
                f"Chollometro GraphQL error: {self._first_message(errors)}",
                attempt=attempt,
                status_code=status,
                response=response,
            )
        data = payload.get("data") or {}
        threads = data.get("threads") if isinstance(data, dict) else None
        if not isinstance(threads, list):
            raise ChollometroParseError(
                "Chollometro GraphQL payload has no threads list",
                attempt=attempt,
                status_code=status,
                response=response,
            )
        return threads

    @staticmethod
    def _first_message(errors):
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message", first))[:200]
            return str(first)[:200]
        return "unknown error"

    @staticmethod
    def _http_error(response, status, attempt):
        error_class = (
            ChollometroRateLimitError
            if status == RATE_LIMIT_STATUS
            else ChollometroHTTPError
        )
        error = error_class(
            f"Chollometro GraphQL HTTP {status}",
            attempt=attempt,
            status_code=status,
            response=response,
        )
        error.retryable = status in RETRYABLE_STATUS
        return error

    def _headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": self.base_url,
            "Referer": self.home_url,
        }
        # The site's own axios client sends the URL-decoded cookie value; the
        # endpoint also answers without it for read-only queries.
        token = self.session.cookies.get(XSRF_COOKIE)
        if token:
            headers["X-XSRF-TOKEN"] = unquote(token)
        return headers

    def _ensure_session(self, force=False):
        """Warm the session once, and again when the cookies may have expired."""
        now = self._clock()
        if not force and self._session_started_at is not None:
            age = (now - self._session_started_at).total_seconds()
            if age < self.session_ttl_seconds:
                return
        try:
            response = self.session.get(self.home_url, timeout=self.timeout)
        except requests.Timeout as exc:
            raise ChollometroTimeoutError(
                f"Chollometro session timeout after {self.timeout:g}s"
            ) from exc
        except requests.RequestException as exc:
            raise ChollometroNetworkError(
                f"Chollometro session {type(exc).__name__}"
            ) from exc
        status = getattr(response, "status_code", None)
        self.last_http_status = status
        if status is not None and status >= 400:
            raise self._http_error(response, status, attempt=1)
        self._session_started_at = now
        cookies = getattr(self.session, "cookies", {})
        self._xsrf_present = bool(cookies.get(XSRF_COOKIE))
        logger.debug(
            "feed_session_ready xsrf=%s pepper_session=%s",
            self._xsrf_present,
            bool(cookies.get(SESSION_COOKIE)),
        )

    @staticmethod
    def _retry_after(response):
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        try:
            seconds = float(str(headers.get("Retry-After")).strip())
        except (AttributeError, TypeError, ValueError):
            return None
        return seconds if seconds >= 0 else None

    def _delay(self, attempt, retry_after=None):
        if retry_after is not None:
            return min(retry_after, self.max_backoff)
        return min(self.backoff * (2 ** (attempt - 1)), self.max_backoff)
