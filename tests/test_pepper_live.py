"""Opt-in live contract checks; normal pytest runs remain offline."""

import os

import pytest
import requests

from chollometro_alerts.pepper import (
    CHOLLOMETRO,
    DEALABS,
    HOTUKDEALS,
    MYDEALZ,
    PEPPER_PL,
    PREISJAEGER,
    PROMODESCUENTOS,
    PepperGraphQLProvider,
)

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


@pytest.mark.parametrize(
    "site", [CHOLLOMETRO, DEALABS, MYDEALZ, HOTUKDEALS, PEPPER_PL, PREISJAEGER]
)
def test_live_validated_sites_normalize_recent_thread(site):
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_TESTS=1 to enable network probes")

    provider = PepperGraphQLProvider(site)
    deals = provider.get_recent_deals()
    assert deals
    deal = deals[0]
    assert deal.site == site.name
    assert deal.currency == site.currency
    assert deal.title and deal.deal_id and deal.url.startswith(("http://", "https://"))
    if deal.image:
        assert deal.image.startswith(site.image_base_url)
