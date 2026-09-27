import json
from pathlib import Path

import pytest

from chollometro_alerts.config import ConfigurationError
from chollometro_alerts.graphql_feed import thread_to_deal
from chollometro_alerts.models import format_amount
from chollometro_alerts.pepper import (
    CHOLLOMETRO,
    DEALABS,
    HOTUKDEALS,
    MYDEALZ,
    PEPPER_NL,
    PEPPER_PL,
    PEPPERDEALS_SE,
    PREISJAEGER,
    PROMODESCUENTOS,
    PepperGraphQLProvider,
)
from chollometro_alerts.pepper_config import get_pepper_site

FIXTURE = Path(__file__).parent / "fixtures" / "pepper_chollometro.json"


@pytest.mark.parametrize(
    (
        "site",
        "country",
        "locale",
        "currency",
        "recent",
        "hottest",
        "search",
        "deal",
        "group",
        "image",
    ),
    [
        (
            CHOLLOMETRO,
            "ES",
            "es-ES",
            "EUR",
            "/nuevos",
            "/top",
            "/search",
            "/ofertas",
            "/grupo",
            "https://static.chollometro.com",
        ),
        (
            DEALABS,
            "FR",
            "fr-FR",
            "EUR",
            "/nouveaux",
            "/top",
            "/search",
            "/bons-plans",
            "/groupe",
            "https://static-pepper.dealabs.com",
        ),
        (
            MYDEALZ,
            "DE",
            "de-DE",
            "EUR",
            "/new",
            "/hot",
            "/search",
            "/deals",
            None,
            "https://static.mydealz.de",
        ),
        (
            HOTUKDEALS,
            "GB",
            "en-GB",
            "GBP",
            "/new",
            "/hottest",
            "/search",
            "/deals",
            None,
            "https://images.hotukdeals.com",
        ),
        (
            PEPPER_PL,
            "PL",
            "pl-PL",
            "PLN",
            "/nowe",
            "/najgoretsze",
            "/search",
            "/promocje",
            "/grupa",
            "https://static.pepper.pl",
        ),
        (
            PREISJAEGER,
            "AT",
            "de-AT",
            "EUR",
            "/neu",
            "/heisseste",
            "/search",
            "/deals",
            None,
            "https://static.preisjaeger.at",
        ),
        (
            PROMODESCUENTOS,
            "MX",
            "es-MX",
            "MXN",
            "/nuevas",
            "/hot",
            "/search",
            "/ofertas",
            "/grupo",
            "https://static.promodescuentos.com",
        ),
        (
            PEPPER_NL,
            "NL",
            "nl-NL",
            "EUR",
            "/nieuw",
            "/heet",
            "/search",
            "/aanbiedingen",
            "/groep",
            "https://static.pepper.com",
        ),
        (
            PEPPERDEALS_SE,
            "SE",
            "sv-SE",
            "SEK",
            "/",
            "/het",
            "/search",
            "/deals",
            "/kategorier",
            "https://static.pepperdeals.se",
        ),
    ],
)
def test_validated_site_config(
    site, country, locale, currency, recent, hottest, search, deal, group, image
):
    assert (site.country, site.locale, site.currency) == (country, locale, currency)
    assert (
        site.recent_path,
        site.hottest_path,
        site.search_path,
        site.deal_path,
        site.group_path,
    ) == (
        recent,
        hottest,
        search,
        deal,
        group,
    )
    assert site.image_base_url == image


@pytest.mark.parametrize(
    "name",
    [
        "chollometro",
        "dealabs",
        "mydealz",
        "hotukdeals",
        "pepper_pl",
        "preisjaeger",
        "promodescuentos",
        "pepper_nl",
        "pepperdeals_se",
    ],
)
def test_site_selection(name, monkeypatch):
    monkeypatch.setenv("PEPPER_SITE", name)
    assert get_pepper_site().name == name


def test_unknown_site_has_actionable_error(monkeypatch):
    monkeypatch.setenv("PEPPER_SITE", "foo")
    with pytest.raises(ConfigurationError, match="Supported sites:.*dealabs"):
        get_pepper_site()


def test_promodescuentos_live_routes_regression():
    assert PROMODESCUENTOS.recent_path == "/nuevas"
    assert PROMODESCUENTOS.hottest_path == "/hot"
    assert PROMODESCUENTOS.recent_path not in {"/nuevos", "/top"}
    assert PROMODESCUENTOS.hottest_path not in {"/nuevos", "/top"}


@pytest.mark.parametrize(
    ("site", "expected"),
    [
        (CHOLLOMETRO, "€"),
        (DEALABS, "€"),
        (MYDEALZ, "€"),
        (HOTUKDEALS, "£"),
        (PEPPER_PL, "zł"),
        (PREISJAEGER, "€"),
        (PROMODESCUENTOS, "MXN"),
        (PEPPER_NL, "€"),
        (PEPPERDEALS_SE, "SEK"),
    ],
)
def test_provider_normalization_uses_site_currency_and_image_host(site, expected):
    thread = json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]["threads"][0]
    deal = thread_to_deal(thread, site_config=site)
    assert deal.site == site.name
    assert deal.currency == site.currency
    assert deal.image.startswith(site.static_base_url)
    assert format_amount(deal.price, deal.currency).endswith(expected)


def test_provider_endpoint_and_context_are_configured_from_site():
    provider = PepperGraphQLProvider(HOTUKDEALS)
    assert provider.endpoint == "https://www.hotukdeals.com/graphql"
    assert provider.site_config.locale == "en-GB"


def test_relative_graphql_url_is_resolved_against_selected_site():
    thread = json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]["threads"][0]
    thread["url"] = "/deals/example"
    deal = thread_to_deal(thread, site_config=DEALABS)
    assert deal.url == "https://www.dealabs.com/deals/example"


@pytest.mark.parametrize(
    ("site", "url"),
    [
        (PROMODESCUENTOS, "https://www.promodescuentos.com/ofertas/example-123"),
        (PEPPER_NL, "https://nl.pepper.com/aanbiedingen/example-123"),
        (PEPPERDEALS_SE, "https://www.pepperdeals.se/deals/example-123"),
    ],
)
def test_absolute_graphql_urls_are_preserved(site, url):
    thread = json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]["threads"][0]
    thread["url"] = url
    assert thread_to_deal(thread, site_config=site).url == url
