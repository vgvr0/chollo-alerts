from typing import Protocol, runtime_checkable

from ..product import ProductExtraction


@runtime_checkable
class ProductExtractor(Protocol):
    def __call__(
        self, product_text: str, deal_id: str | None = None
    ) -> ProductExtraction: ...
