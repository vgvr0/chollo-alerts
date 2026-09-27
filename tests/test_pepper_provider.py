import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from chollometro_alerts.config import ChollometroSettings, GraphQLFeedSettings
from chollometro_alerts.graphql_feed import thread_to_deal
from chollometro_alerts.pepper import (
    CHOLLOMETRO,
    PROMODESCUENTOS,
    PepperGraphQLProvider,
)
from chollometro_alerts.pepper_config import PepperSiteConfig

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["data"]["threads"][
        0
    ]


@pytest.mark.parametrize(
    ("site", "fixture", "currency"),
    [
        (CHOLLOMETRO, "pepper_chollometro.json", "EUR"),
        (PROMODESCUENTOS, "pepper_promodescuentos.json", "MXN"),
    ],
)
def test_shared_parser_maps_both_pepper_sites(site, fixture, currency):
    deal = thread_to_deal(load_fixture(fixture), site_config=site)

    assert deal.site == site.name
    assert deal.currency == currency
    assert deal.deal_id.isdecimal()
    assert deal.title
    assert deal.url.startswith(site.base_url)
    assert deal.temperature is not None
    assert deal.published_at is not None
    assert deal.price is not None
    assert deal.merchant
    assert deal.image.startswith(site.image_base_url)


def test_site_config_is_immutable_and_ids_are_site_local():
    assert CHOLLOMETRO.currency == "EUR"
    assert PROMODESCUENTOS.currency == "MXN"
    with pytest.raises(AttributeError):
        CHOLLOMETRO.currency = "MXN"

    chollo = thread_to_deal(
        load_fixture("pepper_chollometro.json"), site_config=CHOLLOMETRO
    )
    promo = thread_to_deal(
        load_fixture("pepper_chollometro.json"), site_config=PROMODESCUENTOS
    )
    assert chollo.deal_id == promo.deal_id
    assert (chollo.site, chollo.deal_id) != (promo.site, promo.deal_id)


def test_provider_uses_site_configuration_without_site_conditionals():
    assert PepperGraphQLProvider(CHOLLOMETRO).site_config is CHOLLOMETRO
    assert PepperGraphQLProvider(PROMODESCUENTOS).base_url == PROMODESCUENTOS.base_url


def test_fixture_timestamp_is_aware_utc():
    deal = thread_to_deal(
        load_fixture("pepper_promodescuentos.json"), site_config=PROMODESCUENTOS
    )
    assert deal.published_at == datetime.fromtimestamp(1790511828, UTC)


def test_future_pepper_sites_need_configuration_only():
    site = PepperSiteConfig(
        name="example",
        base_url="https://www.example.test",
        country="XX",
        locale="en-XX",
        currency="XXX",
    )
    deal = thread_to_deal(load_fixture("pepper_promodescuentos.json"), site_config=site)
    assert deal.site == "example"
    assert deal.currency == "XXX"
    assert deal.image.startswith("https://static.example.test/")


class _Response:
    status_code = 200
    headers: ClassVar = {"Content-Type": "application/json"}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.headers = {}
        self.cookies = {}
        self.payload = payload
        self.posts = []

    def get(self, url, **kwargs):
        self.cookies.update(
            {"pepper_session": "%22SESSION%22", "xsrf_t": "%22TOKEN%22"}
        )
        return _Response({})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response(self.payload)


@pytest.mark.parametrize("site", [CHOLLOMETRO, PROMODESCUENTOS])
def test_same_graphql_transport_runs_against_both_site_configs(site):
    fixture = load_fixture(
        "pepper_chollometro.json"
        if site is CHOLLOMETRO
        else "pepper_promodescuentos.json"
    )
    session = _Session({"data": {"threads": [fixture]}})
    provider = PepperGraphQLProvider(
        site,
        session=session,
        settings=ChollometroSettings(),
        feed_settings=GraphQLFeedSettings(window_limit=1),
    )

    deals = provider.get_recent_deals()

    assert len(deals) == 1
    assert session.posts[0][0] == f"{site.base_url}{site.graphql_path}"
    assert deals[0].site == site.name
