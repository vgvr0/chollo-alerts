"""Shared configuration and provider facade for Pepper sites.

The first production caller remains Chollometro.  Promodescuentos is exposed
for contract validation only; enabling a second site in the runtime is a
separate migration.
"""

from .graphql_feed import GraphQLFeedClient
from .pepper_config import (
    CHOLLOMETRO,
    DEALABS,
    HOTUKDEALS,
    MYDEALZ,
    PEPPER_NL,
    PEPPER_PL,
    PEPPER_SITES,
    PEPPER_US,
    PEPPERDEALS_SE,
    PREISJAEGER,
    PROMODESCUENTOS,
    PepperSiteConfig,
    get_pepper_site,
)


class PepperGraphQLProvider(GraphQLFeedClient):
    """Reusable Pepper GraphQL provider with a site-specific configuration."""

    def __init__(self, site: PepperSiteConfig, *args, **kwargs):
        self.site_config = site
        kwargs.setdefault("site_config", site)
        super().__init__(*args, **kwargs)

    def get_recent_deals(self, limit=None):
        """Provider-neutral name for the shared newest-deals operation."""
        return self.latest(limit=limit)

    def get_deal(self, deal_id):
        """Fetch one deal through the shared Pepper detail query."""
        return self.detail(deal_id)


__all__ = [
    "CHOLLOMETRO",
    "DEALABS",
    "HOTUKDEALS",
    "MYDEALZ",
    "PEPPERDEALS_SE",
    "PEPPER_NL",
    "PEPPER_PL",
    "PEPPER_SITES",
    "PEPPER_US",
    "PREISJAEGER",
    "PROMODESCUENTOS",
    "PepperGraphQLProvider",
    "PepperSiteConfig",
    "get_pepper_site",
]
