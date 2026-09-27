from .base import (
    AllLLMProvidersFailed,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProductExtractor,
)
from .chain import MultiProviderExtractor
from .deepseek import DeepSeekProductExtractor, DeepSeekProvider
from .providers import GeminiProvider, OpenAIProvider

__all__ = [
    "AllLLMProvidersFailed",
    "DeepSeekProductExtractor",
    "DeepSeekProvider",
    "GeminiProvider",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "MultiProviderExtractor",
    "OpenAIProvider",
    "ProductExtractor",
    "create_extractor",
]


def create_extractor() -> ProductExtractor | None:
    import os

    from ..config import ConfigurationError, LLMSettings

    settings = LLMSettings.from_env()
    if not settings.enabled:
        return None
    # Preserve the historical single-provider extractor when the new chain is
    # not explicitly requested. This keeps its deterministic parsing fallback
    # and CLI metrics fully backwards compatible.
    if "LLM_PROVIDERS" not in os.environ and settings.providers == ("deepseek",):
        return DeepSeekProductExtractor(
            api_key=settings.api_key,
            model=settings.model,
            timeout=settings.timeout,
            retries=settings.retries,
        )
    keys = dict(settings.provider_keys)
    models = dict(settings.provider_models)
    providers: list[LLMProvider] = []
    for name in settings.providers:
        api_key = keys.get(name, "")
        if not api_key:
            continue
        kwargs = {
            "api_key": api_key,
            "model": models[name],
            "timeout": settings.timeout,
        }
        if name == "deepseek":
            providers.append(DeepSeekProvider(retries=settings.retries, **kwargs))
        elif name == "openai":
            providers.append(OpenAIProvider(**kwargs))
        elif name == "gemini":
            providers.append(GeminiProvider(**kwargs))
    if not providers:
        raise ConfigurationError("LLM_ENABLED=true pero ningún proveedor tiene API key")
    return MultiProviderExtractor(providers)
