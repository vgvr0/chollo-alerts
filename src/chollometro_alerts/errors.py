"""Typed failures of the Chollometro provider layer and the scan status model.

The provider layer never returns "0 deals" to signal a problem: a search that
could not be completed raises one of these exceptions, and the scan status
(`SCAN_SUCCESS` / `SCAN_PARTIAL` / `SCAN_FAILED`) records the outcome. That is
what keeps an empty-but-successful search distinguishable from a provider
failure.

The network/HTTP/timeout errors subclass the `requests` exceptions they
replace, so existing `except requests.RequestException` handlers (and the
original `raise ... from exc` cause) keep working.
"""

import requests

# Scan outcomes persisted in `scan_runs.status` and reported by the CLI.
SCAN_SUCCESS = "SUCCESS"
SCAN_PARTIAL = "PARTIAL"
SCAN_FAILED = "FAILED"


class ChollometroError(Exception):
    """Base class for every failure while fetching a Chollometro search page."""

    code = "CHOLLOMETRO_ERROR"
    retryable = False

    def __init__(
        self,
        message,
        *,
        query=None,
        page=None,
        attempt=None,
        status_code=None,
        response=None,
    ):
        Exception.__init__(self, message)
        self.query = query
        self.page = page
        self.attempt = attempt
        self.status_code = status_code
        self.response = response

    @property
    def error_type(self) -> str:
        """Stable label used by logs, `scan_runs.error_type` and error alerts."""
        return self.code


class ChollometroNetworkError(ChollometroError, requests.ConnectionError):
    """Connection refused/reset, DNS or another transport failure."""

    code = "NETWORK_ERROR"
    retryable = True


class ChollometroTimeoutError(ChollometroError, requests.Timeout):
    """The request did not complete within the configured timeout."""

    code = "TIMEOUT"
    retryable = True


class ChollometroHTTPError(ChollometroError, requests.HTTPError):
    """Unexpected HTTP status. 4xx are permanent unless stated otherwise."""

    code = "HTTP_ERROR"
    retryable = False

    @property
    def error_type(self) -> str:
        return f"HTTP_{self.status_code}" if self.status_code else self.code


class ChollometroRateLimitError(ChollometroHTTPError):
    """HTTP 429. Transient, and `Retry-After` is honoured when present."""

    code = "RATE_LIMIT"
    retryable = True


class ChollometroParseError(ChollometroError):
    """The response was not a search results page we know how to read.

    An unexpected payload (captcha, error page, changed markup, empty body) is
    a provider failure, never an empty result set.
    """

    code = "PARSE_ERROR"
    retryable = False
