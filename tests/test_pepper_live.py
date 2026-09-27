"""Opt-in live contract checks; normal pytest runs remain offline."""

import os

import pytest
import requests

from chollometro_alerts.pepper import CHOLLOMETRO, PROMODESCUENTOS

pytestmark = pytest.mark.live


@pytest.mark.parametrize("site", [CHOLLOMETRO, PROMODESCUENTOS])
def test_live_pepper_graphql_contract(site):
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_TESTS=1 to enable network probes")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "chollo-alerts/0.1 (Pepper GraphQL provider)",
            "Accept-Language": site.locale,
        }
    )
    home = session.get(f"{site.base_url}/", timeout=20)
    assert home.status_code == 200
    response = session.post(
        f"{site.base_url}{site.graphql_path}",
        json={
            "query": "query { threads(filter: {}, limit: 1) { threadId title publishedAt temperature } }"
        },
        timeout=20,
    )
    assert response.status_code == 200
    rows = response.json()["data"]["threads"]
    assert rows and all(
        rows[0].get(key) is not None
        for key in ("threadId", "title", "publishedAt", "temperature")
    )
