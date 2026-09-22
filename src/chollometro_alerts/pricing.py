from dataclasses import replace

from .models import Deal
from .product import ProductExtraction


class PricingEngine:
    def evaluate(self, deal: Deal, extraction: ProductExtraction) -> Deal:
        volume = extraction.total_volume_l
        return replace(
            deal,
            product_extraction=extraction,
            units=extraction.units,
            unit_volume_l=extraction.unit_volume_l,
            total_volume_l=volume,
            price_per_liter=(
                deal.price / volume
                if deal.price is not None and volume is not None and volume > 0
                else None
            ),
        )
