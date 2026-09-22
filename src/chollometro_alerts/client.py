from urllib.parse import urlencode

import requests

from .parser import parse_search


class ChollometroClient:
    def __init__(
        self,
        session=None,
        base_url="https://www.chollometro.com",
        timeout=20,
        retries=2,
    ):
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.session.headers.update(
            {"User-Agent": "chollometro-alerts/0.1 (+https://www.chollometro.com)"}
        )

    def search(self, query: str, page: int = 1):
        params = {"q": query}
        if page > 1:
            params["page"] = page
        last = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(
                    f"{self.base_url}/search?{urlencode(params)}", timeout=self.timeout
                )
                response.raise_for_status()
                break
            except (
                requests.Timeout,
                requests.ConnectionError,
                requests.HTTPError,
            ) as exc:
                last = exc
                if attempt == self.retries:
                    raise
        if last and "response" not in locals():
            raise last
        response.encoding = "utf-8"
        return parse_search(response.text, query)

    def recent(self, queries: list[str], pages: int = 1):
        result = {}
        for query in queries:
            for page in range(1, pages + 1):
                for deal in self.search(query, page):
                    result[deal.deal_id] = deal
        return list(result.values())
