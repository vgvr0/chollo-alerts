from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..product import ProductExtraction


@runtime_checkable
class ProductExtractor(Protocol):
    def __call__(
        self, product_text: str, deal_id: str | None = None
    ) -> ProductExtraction: ...


@dataclass(frozen=True)
class LLMRequest:
    """Provider-neutral request used by the multi-provider extraction layer."""

    text: str
    deal_id: str | None = None


LLMResponse = ProductExtraction


class LLMError(Exception):
    """Base class for errors safe to expose from the provider boundary."""


class RetryableLLMError(LLMError):
    """A transient provider error for which another provider may be tried."""

    def __init__(self, reason: str, provider: str = "unknown"):
        self.reason = reason
        self.provider = provider
        super().__init__(f"{provider}: {reason}")


class NonRetryableLLMError(LLMError):
    """A configuration, authentication, validation or programming error."""

    def __init__(self, reason: str, provider: str = "unknown"):
        self.reason = reason
        self.provider = provider
        super().__init__(f"{provider}: {reason}")


class AllLLMProvidersFailed(LLMError):
    """All configured providers failed, with secret-free summarized reasons."""

    def __init__(self, failures: list[tuple[str, str]]):
        self.failures = tuple(failures)
        summary = ", ".join(f"{provider}: {reason}" for provider, reason in failures)
        super().__init__(f"Todos los proveedores LLM fallaron: {summary}")


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def generate(self, request: LLMRequest) -> LLMResponse: ...
