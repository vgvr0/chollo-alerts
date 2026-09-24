"""Chollometro provider: HTTP policy, bounded retries and scan outcomes.

Every request carries an explicit timeout, transient failures are retried with
bounded backoff and permanent ones fail immediately. The client never turns a
failure into an empty result list: it raises a typed `ChollometroError` and
records what actually happened in `last_scan`.
"""

import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

from .config import ChollometroSettings
from .errors import (
    SCAN_FAILED,
    SCAN_PARTIAL,
    SCAN_SUCCESS,
    ChollometroError,
    ChollometroHTTPError,
    ChollometroNetworkError,
    ChollometroParseError,
    ChollometroRateLimitError,
    ChollometroTimeoutError,
)
from .models import Deal
from .parser import parse_search_page

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.chollometro.com"

# Only transient failures are retried. 400/401/403/404 and payload failures are
# permanent: retrying them adds load without making them succeed.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RATE_LIMIT_STATUS = 429


@dataclass(frozen=True)
class ScanOutcome:
    """What one `recent()` batch achieved, including the incomplete case."""

    query: str
    status: str
    deals: tuple[Deal, ...] = ()
    error: ChollometroError | None = None
    pages_requested: int = 0
    pages_fetched: int = 0
    http_status: int | None = None
    fetched_items: int = 0
    parsed_items: int = 0

    @property
    def error_type(self) -> str | None:
        return self.error.error_type if self.error is not None else None

    @property
    def complete(self) -> bool:
        return self.status == SCAN_SUCCESS


class ChollometroClient:
    def __init__(
        self,
        session=None,
        base_url=DEFAULT_BASE_URL,
        timeout=None,
        retries=None,
        settings=None,
        sleep=None,
    ):
        self.settings = settings or ChollometroSettings.from_env()
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        # One timeout per request and one backoff source for the whole client.
        self.timeout = self.settings.timeout if timeout is None else timeout
        self.retries = self.settings.retries if retries is None else retries
        if self.retries < 0:
            raise ValueError("retries debe ser >= 0")
        self.backoff = self.settings.backoff_seconds
        self.max_backoff = self.settings.max_backoff_seconds
        # Injectable so tests exercise the retry policy without waiting.
        self._sleep = sleep or time.sleep
        self.session.headers.update(
            {"User-Agent": "chollometro-alerts/0.1 (+https://www.chollometro.com)"}
        )
        self.last_search = {}
        # Outcome of the last `recent()` batch: SUCCESS, PARTIAL or FAILED.
        self.last_scan: ScanOutcome | None = None

    def search(self, query: str, page: int = 1):
        """Fetch and parse one search page, or raise a typed provider error."""
        page_result, http_status = self._fetch_page(query, page)
        self.last_search = {
            "query": query,
            "page": page,
            "http_status": http_status,
            "fetched_items": page_result.items_found,
            "parsed_items": len(page_result.deals),
        }
        return page_result.deals

    def feed_page(self, page: int = 1):
        """Fetch one public chronological ``/nuevos`` page.

        This is intentionally separate from ``recent``: the normal HTML
        provider remains query-based, while GraphQL gap recovery uses this
        endpoint only after a continuity-loss signal.
        """
        page_result, http_status = self._fetch_page_url("/nuevos", page)
        self.last_search = {
            "query": "__feed__",
            "page": page,
            "http_status": http_status,
            "fetched_items": page_result.items_found,
            "parsed_items": len(page_result.deals),
        }
        return page_result.deals

    def recent(self, queries: list[str], pages: int = 1):
        """Fetch the requested pages, recording a `ScanOutcome` in `last_scan`.

        Raises a typed `ChollometroError` as soon as a page fails, so an
        incomplete scan is never returned as if it were complete. Deals already
        fetched from earlier pages stay available in `last_scan.deals`, which is
        only meaningful together with `last_scan.status == SCAN_PARTIAL`.
        """
        collected: dict[str, Deal] = {}
        pages_requested = pages_fetched = fetched_items = parsed_items = 0
        http_status = None
        self.last_scan = None
        for query in queries:
            for page in range(1, pages + 1):
                pages_requested += 1
                try:
                    deals = self.search(query, page)
                except ChollometroError as exc:
                    self.last_scan = ScanOutcome(
                        query=query,
                        status=SCAN_PARTIAL if pages_fetched else SCAN_FAILED,
                        deals=tuple(collected.values()),
                        error=exc,
                        pages_requested=pages_requested,
                        pages_fetched=pages_fetched,
                        http_status=exc.status_code or http_status,
                        fetched_items=fetched_items,
                        parsed_items=parsed_items,
                    )
                    raise
                pages_fetched += 1
                http_status = self.last_search.get("http_status")
                fetched_items += self.last_search.get("fetched_items") or 0
                parsed_items += self.last_search.get("parsed_items") or 0
                for deal in deals:
                    collected[deal.deal_id] = deal
        self.last_scan = ScanOutcome(
            query=queries[-1] if queries else "",
            status=SCAN_SUCCESS,
            deals=tuple(collected.values()),
            pages_requested=pages_requested,
            pages_fetched=pages_fetched,
            http_status=http_status,
            fetched_items=fetched_items,
            parsed_items=parsed_items,
        )
        return list(collected.values())

    def _fetch_page(self, query: str, page: int):
        params: dict[str, str | int] = {"q": query}
        if page > 1:
            params["page"] = page
        return self._fetch_url(f"/search?{urlencode(params)}", query, page)

    def _fetch_page_url(self, path: str, page: int):
        params = {"page": page} if page > 1 else {}
        suffix = f"?{urlencode(params)}" if params else ""
        return self._fetch_url(f"{path}{suffix}", "", page)

    def _fetch_url(self, path: str, query: str, page: int):
        url = f"{self.base_url}{path}"
        for attempt in range(1, self.retries + 2):
            response = None
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return self._parse_page(response, query, page, attempt)
            except requests.RequestException as exc:
                error = self._classify(exc, query, page, attempt)
                # The `as` binding is cleared when the except block ends.
                cause = exc
            if not error.retryable or attempt > self.retries:
                logger.error(
                    "chollometro_request_failed query=%s page=%s http_status=%s "
                    "error_type=%s attempt=%s attempts=%s",
                    query,
                    page,
                    error.status_code,
                    error.error_type,
                    attempt,
                    self.retries + 1,
                )
                raise error from cause
            delay = self._delay(attempt, self._retry_after(response))
            logger.warning(
                "chollometro_request_retry query=%s page=%s http_status=%s "
                "error_type=%s attempt=%s retry_in_seconds=%.3f",
                query,
                page,
                error.status_code,
                error.error_type,
                attempt,
                delay,
            )
            self._sleep(delay)
        raise AssertionError("retry loop must return or raise")

    @staticmethod
    def _parse_page(response, query, page, attempt):
        response.encoding = "utf-8"
        try:
            page_result = parse_search_page(response.text, query)
        except ChollometroParseError as exc:
            raise ChollometroParseError(
                f"Chollometro parse error query={query} page={page}: {exc}",
                query=query,
                page=page,
                attempt=attempt,
                status_code=getattr(response, "status_code", None),
                response=response,
            ) from exc
        return page_result, getattr(response, "status_code", None)

    def _classify(self, exc, query, page, attempt) -> ChollometroError:
        context = {"query": query, "page": page, "attempt": attempt}
        if isinstance(exc, requests.Timeout):
            return ChollometroTimeoutError(
                f"Chollometro timeout after {self.timeout:g}s "
                f"query={query} page={page}",
                **context,
            )
        if isinstance(exc, requests.HTTPError):
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            error_class = (
                ChollometroRateLimitError
                if status == RATE_LIMIT_STATUS
                else ChollometroHTTPError
            )
            error: ChollometroError = error_class(
                f"Chollometro HTTP {status} query={query} page={page}",
                status_code=status,
                response=response,
                **context,
            )
            error.retryable = status in RETRYABLE_STATUS
            return error
        error = ChollometroNetworkError(
            f"Chollometro {type(exc).__name__} query={query} page={page}",
            **context,
        )
        # A plain connection failure is transient; anything else that reaches
        # this point (invalid URL, redirect loop, TLS failure) is not.
        error.retryable = isinstance(exc, requests.ConnectionError)
        return error

    @staticmethod
    def _retry_after(response):
        """Seconds from `Retry-After`, when the server sends a numeric value."""
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
