"""Optional HTTP providers kept behind the provider-neutral LLM boundary."""

import json
import logging
from time import perf_counter
from typing import Any

import requests

from ..config import ConfigurationError
from ..product import ProductExtraction
from .base import LLMRequest, NonRetryableLLMError, RetryableLLMError

logger = logging.getLogger(__name__)


class _HTTPProvider:
    name = "unknown"
    endpoint: str

    def __init__(self, api_key, model, timeout=20, session=None):
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = float(timeout)
        self.session = session or requests.Session()
        self._metrics = {
            "LLM_CALLS": 0,
            "LLM_SUCCESSES": 0,
            "LLM_FAILURES": 0,
            "LLM_DURATION_SECONDS": 0.0,
        }
        if not self.api_key:
            raise ConfigurationError(f"{self.name.upper()}_API_KEY es obligatoria")
        if not self.model:
            raise ConfigurationError(f"{self.name.upper()}_MODEL no puede estar vacío")

    def generate(self, request: LLMRequest) -> ProductExtraction:
        started = perf_counter()
        self._metrics["LLM_CALLS"] += 1
        try:
            response = self.session.post(
                self.endpoint,
                headers=self.headers,
                json=self.payload(request.text),
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = ProductExtraction.model_validate(
                {**self.parse_text(response), "extraction_source": "llm"}
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            self._fail(started)
            raise RetryableLLMError(type(exc).__name__.lower(), self.name) from exc
        except requests.HTTPError as exc:
            self._fail(started)
            status = getattr(exc.response, "status_code", 0)
            if status in {408, 429} or 500 <= status <= 599:
                raise RetryableLLMError(f"http_{status}", self.name) from exc
            raise NonRetryableLLMError(f"http_{status}", self.name) from exc
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            self._fail(started)
            raise RetryableLLMError("invalid_response", self.name) from exc
        self._metrics["LLM_SUCCESSES"] += 1
        self._metrics["LLM_DURATION_SECONDS"] += perf_counter() - started
        return result

    def _fail(self, started):
        self._metrics["LLM_FAILURES"] += 1
        self._metrics["LLM_DURATION_SECONDS"] += perf_counter() - started

    @property
    def metrics(self):
        return dict(self._metrics)

    @staticmethod
    def schema():
        schema = ProductExtraction.model_json_schema()
        schema["properties"].pop("extraction_source")
        schema["properties"].pop("unit_weight_kg", None)
        schema["properties"].pop("total_weight_kg", None)
        schema["required"] = list(schema["properties"])
        for field in schema["properties"].values():
            field.pop("default", None)
        return schema

    @property
    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def payload(self, text: str) -> dict[str, Any]:
        raise NotImplementedError

    def parse_text(self, response: Any) -> dict[str, Any]:
        raise NotImplementedError


class OpenAIProvider(_HTTPProvider):
    name = "openai"
    endpoint = "https://api.openai.com/v1/chat/completions"

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def payload(self, text):
        return {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": "Extrae hechos explícitos del producto en JSON. Usa null para desconocidos.",
                },
                {"role": "user", "content": text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "product_extraction",
                    "strict": True,
                    "schema": self.schema(),
                },
            },
        }

    def parse_text(self, response):
        return json.loads(response.json()["choices"][0]["message"]["content"])


class GeminiProvider(_HTTPProvider):
    name = "gemini"

    @property
    def endpoint(self):
        return f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

    @property
    def headers(self):
        return {"Content-Type": "application/json"}

    def payload(self, text):
        return {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": self.schema(),
            },
        }

    def parse_text(self, response):
        return json.loads(
            response.json()["candidates"][0]["content"]["parts"][0]["text"]
        )
