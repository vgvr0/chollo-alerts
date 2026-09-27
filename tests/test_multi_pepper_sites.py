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
    PEPPER_PL,
    PREISJAEGER,
    PepperGraphQLProvider,
)
from chollometro_alerts.pepper_config import get_pepper_site

FIXTURE = Path(__file__).parent / "fixtures" / "pepper_chollometro.json"


@pytest.mark.parametrize(
    ("site", "country", "locale", "currency", "recent", "hottest", "deal", "image"),
    [
        (
            CHOLLOMETRO,
            "ES",
            "es-ES",
            "EUR",
            "/nuevos",
            "/top",
            "/ofertas",
            "https://static.chollometro.com",
        ),
        (
            DEALABS,
            "FR",
            "fr-FR",
            "EUR",
            "/nouveaux",
            "/top",
            "/bons-plans",
            "https://static-pepper.dealabs.com",
        ),
        (
            MYDEALZ,
            "DE",
            "de-DE",
            "EUR",
            "/new",
            "/hot",
            "/deals",
            "https://static.mydealz.de",
        ),
        (
            HOTUKDEALS,
            "GB",
            "en-GB",
            "GBP",
            "/new",
            "/hottest",
            "/deals",
            "https://images.hotukdeals.com",
        ),
        (
            PEPPER_PL,
            "PL",
            "pl-PL",
            "PLN",
            "/nowe",
            "/najgoretsze",
            "/promocje",
            "https://static.pepper.pl",
        ),
        (
            PREISJAEGER,
            "AT",
            "de-AT",
            "EUR",
            "/neu",
            "/heisseste",
            "/deals",
            "https://static.preisjaeger.at",
        ),
    ],
)
def test_validated_site_config(
    site, country, locale, currency, recent, hottest, deal, image
):
    assert (site.country, site.locale, site.currency) == (country, locale, currency)
    assert (site.recent_path, site.hottest_path, site.deal_path) == (
        recent,
        hottest,
        deal,
    )
    assert site.image_base_url == image


@pytest.mark.parametrize(
    "name",
    ["chollometro", "dealabs", "mydealz", "hotukdeals", "pepper_pl", "preisjaeger"],
)
def test_site_selection(name, monkeypatch):
    monkeypatch.setenv("PEPPER_SITE", name)
    assert get_pepper_site().name == name


def test_unknown_site_has_actionable_error(monkeypatch):
    monkeypatch.setenv("PEPPER_SITE", "foo")
    with pytest.raises(ConfigurationError, match="Supported sites:.*dealabs"):
        get_pepper_site()


@pytest.mark.parametrize(
    ("site", "expected"),
    [
        (CHOLLOMETRO, "€"),
        (DEALABS, "€"),
        (MYDEALZ, "€"),
        (HOTUKDEALS, "£"),
        (PEPPER_PL, "zł"),
        (PREISJAEGER, "€"),
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
