"""Immutable configuration shared by the validated Pepper country sites."""

import os
from dataclasses import dataclass

from .config import ConfigurationError, load_project_dotenv


@dataclass(frozen=True, slots=True)
class PepperSiteConfig:
    name: str
    base_url: str
    country: str
    locale: str
    currency: str
    graphql_path: str = "/graphql"
    deal_path: str | None = None
    recent_path: str = "/nuevos"
    hottest_path: str = "/top"
    search_path: str = "/search"
    group_path: str | None = None
    static_base_url: str | None = None

    @property
    def image_base_url(self) -> str:
        if self.static_base_url:
            return self.static_base_url.rstrip("/")
        host = self.base_url.removeprefix("https://").removeprefix("http://")
        host = host.split("/", 1)[0]
        host = host.removeprefix("www.")
        return f"https://static.{host}"


CHOLLOMETRO = PepperSiteConfig(
    name="chollometro",
    base_url="https://www.chollometro.com",
    country="ES",
    locale="es-ES",
    currency="EUR",
    graphql_path="/graphql",
    deal_path="/ofertas",
    recent_path="/nuevos",
    hottest_path="/top",
    search_path="/search",
    group_path="/grupo",
    static_base_url="https://static.chollometro.com",
)

DEALABS = PepperSiteConfig(
    name="dealabs",
    base_url="https://www.dealabs.com",
    country="FR",
    locale="fr-FR",
    currency="EUR",
    recent_path="/nouveaux",
    hottest_path="/top",
    search_path="/search",
    deal_path="/bons-plans",
    group_path="/groupe",
    static_base_url="https://static-pepper.dealabs.com",
)
MYDEALZ = PepperSiteConfig(
    name="mydealz",
    base_url="https://www.mydealz.de",
    country="DE",
    locale="de-DE",
    currency="EUR",
    recent_path="/new",
    hottest_path="/hot",
    search_path="/search",
    deal_path="/deals",
    static_base_url="https://static.mydealz.de",
)
HOTUKDEALS = PepperSiteConfig(
    name="hotukdeals",
    base_url="https://www.hotukdeals.com",
    country="GB",
    locale="en-GB",
    currency="GBP",
    recent_path="/new",
    hottest_path="/hottest",
    search_path="/search",
    deal_path="/deals",
    static_base_url="https://images.hotukdeals.com",
)
PEPPER_PL = PepperSiteConfig(
    name="pepper_pl",
    base_url="https://www.pepper.pl",
    country="PL",
    locale="pl-PL",
    currency="PLN",
    recent_path="/nowe",
    hottest_path="/najgoretsze",
    search_path="/search",
    deal_path="/promocje",
    group_path="/grupa",
    static_base_url="https://static.pepper.pl",
)
PREISJAEGER = PepperSiteConfig(
    name="preisjaeger",
    base_url="https://www.preisjaeger.at",
    country="AT",
    locale="de-AT",
    currency="EUR",
    recent_path="/neu",
    hottest_path="/heisseste",
    search_path="/search",
    deal_path="/deals",
    static_base_url="https://static.preisjaeger.at",
)

PROMODESCUENTOS = PepperSiteConfig(
    name="promodescuentos",
    base_url="https://www.promodescuentos.com",
    country="MX",
    locale="es-MX",
    currency="MXN",
    graphql_path="/graphql",
    recent_path="/nuevas",
    hottest_path="/hot",
    search_path="/search",
    deal_path="/ofertas",
    group_path="/grupo",
    static_base_url="https://static.promodescuentos.com",
)

PEPPER_NL = PepperSiteConfig(
    name="pepper_nl",
    base_url="https://nl.pepper.com",
    country="NL",
    locale="nl-NL",
    currency="EUR",
    graphql_path="/graphql",
    recent_path="/nieuw",
    hottest_path="/heet",
    search_path="/search",
    deal_path="/aanbiedingen",
    group_path="/groep",
    static_base_url="https://static.pepper.com",
)

PEPPERDEALS_SE = PepperSiteConfig(
    name="pepperdeals_se",
    base_url="https://www.pepperdeals.se",
    country="SE",
    locale="sv-SE",
    currency="SEK",
    graphql_path="/graphql",
    recent_path="/",
    hottest_path="/het",
    search_path="/search",
    deal_path="/deals",
    group_path="/kategorier",
    static_base_url="https://static.pepperdeals.se",
)

PEPPER_SITES = {
    site.name: site
    for site in (
        CHOLLOMETRO,
        DEALABS,
        MYDEALZ,
        HOTUKDEALS,
        PEPPER_PL,
        PREISJAEGER,
        PROMODESCUENTOS,
        PEPPER_NL,
        PEPPERDEALS_SE,
    )
}


def get_pepper_site(value: str | None = None) -> PepperSiteConfig:
    """Resolve the configured site, retaining Chollometro as the default."""
    load_project_dotenv()
    key = (
        (value if value is not None else os.getenv("PEPPER_SITE", "chollometro"))
        .strip()
        .casefold()
    )
    try:
        return PEPPER_SITES[key]
    except KeyError as exc:
        supported = ", ".join(PEPPER_SITES)
        raise ConfigurationError(
            f"Unsupported Pepper site: {key}\nSupported sites: {supported}"
        ) from exc
