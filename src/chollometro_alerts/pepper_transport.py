"""Site-selectable HTTP transports for Pepper GraphQL sessions."""

from __future__ import annotations

import requests
from curl_cffi import requests as curl_requests

from .pepper_config import PepperSiteConfig


class CurlCffiSession:
    """Small requests-compatible adapter with an explicit browser profile."""

    def __init__(self, impersonate: str = "chrome") -> None:
        self.impersonate = impersonate
        self._session = curl_requests.Session(impersonate=impersonate)

    @property
    def cookies(self):
        return self._session.cookies

    @property
    def headers(self):
        return self._session.headers

    def get(self, *args, **kwargs):
        return self._request(self._session.get, *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._request(self._session.post, *args, **kwargs)

    @staticmethod
    def _request(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except curl_requests.RequestsError as exc:
            raise requests.RequestException(str(exc)) from exc


def build_session(site: PepperSiteConfig):
    """Build the configured session; never silently fall back between transports."""
    if site.transport == "curl_cffi":
        return CurlCffiSession(site.impersonate or "chrome")
    return requests.Session()


def extra_headers(site: PepperSiteConfig) -> dict[str, str]:
    if site.transport != "curl_cffi":
        return {}
    return {
        "x-request-type": "application/vnd.pepper.v1+json",
        "x-requested-with": "XMLHttpRequest",
        "x-pepper-txn": site.pepper_txn or "threads.index",
    }
