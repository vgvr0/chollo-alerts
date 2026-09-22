import json

import pytest
import requests

from chollometro_alerts.llm import DeepSeekProductExtractor


class Response:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage or {"total_tokens": 17}

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(self.content)}
                    ],
                }
            ],
            "usage": self.usage,
        }


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.requests = []

    def post(self, *args, **kwargs):
        self.calls += 1
        self.requests.append((args, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def payload():
    return {
        "product_type": "beer",
        "brand": "Mahou",
        "variant": None,
        "units": 6,
        "unit_volume_l": 0.33,
        "total_volume_l": 1.98,
        "confidence": 0.91,
    }


def test_deepseek_json_is_validated_and_tokens_counted():
    session = Session([Response(payload())])
    extractor = DeepSeekProductExtractor(api_key="test", session=session)
    result = extractor("Pack Mahou", deal_id="new-1")
    assert result.brand == "Mahou"
    assert extractor.metrics == {
        "LLM_CALLS": 1,
        "LLM_FAILURES": 0,
        "LLM_TOKENS": 17,
        "LLM_INPUT_TOKENS": 0,
        "LLM_CACHED_TOKENS": 0,
        "LLM_OUTPUT_TOKENS": 0,
        "LLM_REASONING_TOKENS": 0,
        "LLM_TOTAL_TOKENS": 17,
    }


def test_deepseek_retries_twice_and_failure_is_counted():
    import requests

    session = Session([requests.Timeout(), requests.Timeout(), requests.Timeout()])
    extractor = DeepSeekProductExtractor(api_key="test", session=session)
    result = extractor("Pack desconocido", deal_id="new-2")
    assert result.extraction_source == "deterministic"
    assert session.calls == 3
    assert extractor.metrics["LLM_FAILURES"] == 1


def test_structured_schema_and_timeout():
    session = Session([Response(payload())])
    extractor = DeepSeekProductExtractor(
        api_key="test", model="custom", timeout=7, session=session
    )
    extractor("Pack Mahou")
    args, kwargs = session.requests[0]
    assert args == ("https://api.deepseek.com/responses",)
    assert kwargs["timeout"] == 7
    assert kwargs["json"]["model"] == "custom"
    assert kwargs["json"]["reasoning"] == {"effort": "none"}
    fmt = kwargs["json"]["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == set(payload())
    assert "price" not in fmt["schema"]["properties"]


def test_usage_details_are_optional_and_accumulated():
    session = Session(
        [
            Response(
                payload(),
                usage={
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "total_tokens": 20,
                },
            )
        ]
    )
    extractor = DeepSeekProductExtractor(api_key="test", session=session)
    extractor("Pack Mahou")
    assert extractor.metrics["LLM_INPUT_TOKENS"] == 12
    assert extractor.metrics["LLM_CACHED_TOKENS"] == 0
    assert extractor.metrics["LLM_OUTPUT_TOKENS"] == 8
    assert extractor.metrics["LLM_REASONING_TOKENS"] == 0
    assert extractor.metrics["LLM_TOTAL_TOKENS"] == 20


def test_timeout_then_success():
    import requests

    session = Session([requests.Timeout(), Response(payload())])
    extractor = DeepSeekProductExtractor(api_key="test", session=session)
    assert extractor("Pack Mahou").brand == "Mahou"
    assert session.calls == 2
    assert extractor.metrics["LLM_CALLS"] == 2


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"status": "incomplete"},
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "not json"}],
                }
            ],
        },
    ],
)
def test_malformed_or_incomplete_response_falls_back(body):
    response = Response({})
    response.json = lambda: body
    session = Session([response])
    extractor = DeepSeekProductExtractor(api_key="test", session=session)
    result = extractor("Cerveza")
    assert result.extraction_source == "deterministic"
    assert result.total_volume_l is None
    assert session.calls == 1


@pytest.mark.parametrize(("status", "expected_calls"), [(401, 1), (429, 3), (503, 3)])
def test_http_retry_policy(status, expected_calls):
    response = requests.Response()
    response.status_code = status
    session = Session([response] * 3)
    extractor = DeepSeekProductExtractor(api_key="test", session=session)
    assert extractor("Cerveza").extraction_source == "deterministic"
    assert session.calls == expected_calls
