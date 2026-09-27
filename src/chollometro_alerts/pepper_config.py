"""Immutable configuration shared by Pepper country sites."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PepperSiteConfig:
    name: str
    base_url: str
    country: str
    locale: str
    currency: str
    graphql_path: str = "/graphql"
    deal_path: str | None = None

    @property
    def image_base_url(self) -> str:
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
)

PROMODESCUENTOS = PepperSiteConfig(
    name="promodescuentos",
    base_url="https://www.promodescuentos.com",
    country="MX",
    locale="es-MX",
    currency="MXN",
    graphql_path="/graphql",
    deal_path="/ofertas",
)
