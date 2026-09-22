from .base import ProductExtractor
from .deepseek import DeepSeekProductExtractor

__all__ = ["DeepSeekProductExtractor", "ProductExtractor", "create_extractor"]


def create_extractor() -> ProductExtractor | None:
    from ..config import ConfigurationError, LLMSettings

    settings = LLMSettings.from_env()
    if not settings.enabled:
        return None
    if settings.provider != "deepseek":
        raise ConfigurationError(f"LLM_PROVIDER no soportado: {settings.provider}")
    return DeepSeekProductExtractor(
        api_key=settings.api_key,
        model=settings.model,
        timeout=settings.timeout,
        retries=settings.retries,
    )
