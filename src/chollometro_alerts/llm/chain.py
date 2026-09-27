"""Provider chain and fallback policy for the LLM boundary."""

import logging
from time import perf_counter

from prometheus_client import Counter, Histogram

from .base import (
    AllLLMProvidersFailed,
    LLMRequest,
    NonRetryableLLMError,
    RetryableLLMError,
)

logger = logging.getLogger(__name__)
_requests = Counter("llm_requests", "LLM requests by provider", ("provider",))
_failures = Counter(
    "llm_failures", "LLM failures by provider and reason", ("provider", "reason")
)
_fallbacks = Counter(
    "llm_fallback", "LLM fallbacks between providers", ("from_provider", "to_provider")
)
_latency = Histogram("llm_latency_seconds", "LLM latency by provider", ("provider",))


class MultiProviderExtractor:
    """Callable ProductExtractor that knows nothing about provider SDKs."""

    def __init__(self, providers):
        self.providers = tuple(providers)
        self._metrics = {
            "LLM_CALLS": 0,
            "LLM_SUCCESSES": 0,
            "LLM_FAILURES": 0,
            "LLM_DURATION_SECONDS": 0.0,
            "LLM_TOKENS": 0,
            "LLM_INPUT_TOKENS": 0,
            "LLM_OUTPUT_TOKENS": 0,
        }

    def __getattr__(self, name):
        # Compatibility for callers that historically inspected the DeepSeek
        # extractor directly (model, timeout, retries, api_key, etc.).
        if name not in {"providers", "_metrics"} and self.providers:
            return getattr(self.providers[0], name)
        raise AttributeError(name)

    def __call__(self, product_text, deal_id=None):
        request = LLMRequest(product_text, deal_id)
        failures = []
        previous = None
        for provider in self.providers:
            try:
                if previous:
                    _fallbacks.labels(previous, provider.name).inc()
                    logger.warning(
                        "llm.fallback from_provider=%s to_provider=%s",
                        previous,
                        provider.name,
                    )
                started = perf_counter()
                _requests.labels(provider.name).inc()
                self._metrics["LLM_CALLS"] += 1
                result = provider.generate(request)
                elapsed = perf_counter() - started
                _latency.labels(provider.name).observe(elapsed)
                self._metrics["LLM_DURATION_SECONDS"] += elapsed
                self._metrics["LLM_SUCCESSES"] += 1
                return result
            except RetryableLLMError as exc:
                previous = provider.name
                failures.append((provider.name, exc.reason))
                _failures.labels(provider.name, exc.reason).inc()
                self._metrics["LLM_FAILURES"] += 1
                logger.warning(
                    "llm.provider_failed provider=%s reason=%s",
                    provider.name,
                    exc.reason,
                )
            except NonRetryableLLMError:
                raise
        raise AllLLMProvidersFailed(failures)

    @property
    def metrics(self):
        return dict(self._metrics)

    def interpret_alert_rule(self, text):
        """Keep Telegram's existing provider-specific adapter compatible."""
        for provider in self.providers:
            method = getattr(provider, "interpret_alert_rule", None)
            if method is not None:
                return method(text)
        raise AllLLMProvidersFailed(
            [(p.name, "unsupported_operation") for p in self.providers]
        )
