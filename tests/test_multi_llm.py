import pytest

from chollometro_alerts.llm import MultiProviderExtractor, create_extractor
from chollometro_alerts.llm.base import (
    AllLLMProvidersFailed,
    LLMRequest,
    NonRetryableLLMError,
    RetryableLLMError,
)
from chollometro_alerts.product import ProductExtraction


def extraction():
    return ProductExtraction(
        product_type="beer",
        brand="Mahou",
        variant=None,
        units=6,
        unit_volume_l=0.33,
        total_volume_l=1.98,
        confidence=0.9,
        extraction_source="llm",
    )


class FakeProvider:
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error = name, result, error
        self.calls = 0

    def generate(self, request: LLMRequest):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_chain_respects_order_and_stops_after_success():
    first = FakeProvider("deepseek", error=RetryableLLMError("timeout", "deepseek"))
    second = FakeProvider("openai", result=extraction())
    third = FakeProvider("gemini", result=extraction())

    assert MultiProviderExtractor([first, second, third])("beer").brand == "Mahou"
    assert (first.calls, second.calls, third.calls) == (1, 1, 0)


@pytest.mark.parametrize(
    "reason", ["http_429", "http_500", "connectionerror", "invalid_response"]
)
def test_retryable_errors_fallback(reason):
    first = FakeProvider("deepseek", error=RetryableLLMError(reason, "deepseek"))
    second = FakeProvider("openai", result=extraction())
    assert MultiProviderExtractor([first, second])("beer").extraction_source == "llm"


@pytest.mark.parametrize("reason", ["http_400", "http_401", "http_403"])
def test_non_retryable_errors_do_not_fallback(reason):
    first = FakeProvider("deepseek", error=NonRetryableLLMError(reason, "deepseek"))
    second = FakeProvider("openai", result=extraction())
    with pytest.raises(NonRetryableLLMError):
        MultiProviderExtractor([first, second])("beer")
    assert second.calls == 0


def test_all_failures_are_summarized_without_exception_details():
    providers = [
        FakeProvider("deepseek", error=RetryableLLMError("timeout", "deepseek")),
        FakeProvider("openai", error=RetryableLLMError("rate_limit", "openai")),
    ]
    with pytest.raises(AllLLMProvidersFailed) as caught:
        MultiProviderExtractor(providers)("beer")
    assert (
        str(caught.value)
        == "Todos los proveedores LLM fallaron: deepseek: timeout, openai: rate_limit"
    )
    assert "secret" not in str(caught.value).lower()


def test_provider_without_key_is_skipped(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDERS", "deepseek,openai,gemini")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    extractor = create_extractor()
    assert [provider.name for provider in extractor.providers] == ["openai"]
