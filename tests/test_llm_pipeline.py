from decimal import Decimal
from unittest.mock import Mock

import pytest
from test_deepseek import Response, Session, payload

from chollometro_alerts.config import ConfigurationError, InterestRule
from chollometro_alerts.llm import create_extractor
from chollometro_alerts.llm.deepseek import DeepSeekProductExtractor
from chollometro_alerts.parser import parse_search
from chollometro_alerts.product import extract_product
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.service import AlertService


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("LLM_ENABLED", "false")
    for key in (
        "LLM_PROVIDER",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_TIMEOUT_SECONDS",
        "DEEPSEEK_MAX_RETRIES",
    ):
        if key == "DEEPSEEK_API_KEY":
            monkeypatch.setenv(key, "")
        elif key == "LLM_PROVIDER":
            monkeypatch.setenv(key, "deepseek")
        else:
            monkeypatch.delenv(key, raising=False)


def make_service(tmp_path, title="Pack cerveza Mahou", responses=None):
    html = f'<article id="thread_123"><a class="thread-title">{title}</a><span class="thread-price">12,50€</span></article>'
    deals = parse_search(html, "cerveza")
    client = Mock()
    client.recent.return_value = deals
    notifier = Mock(dry_run=False)
    repository = DealRepository(tmp_path / "deals.db")
    session = Session(responses if responses is not None else [Response(payload())])
    extractor = DeepSeekProductExtractor(api_key="test", session=session)
    return AlertService(client, repository, notifier, extractor), session


def test_known_deal_without_cache_never_calls_deepseek(tmp_path):
    service, session = make_service(tmp_path)
    service.repository.upsert(service.client.recent()[0])
    service.run(["cerveza"])
    assert session.calls == 0
    assert service.last_summary.already_known == 1


def test_complete_deterministic_extraction_never_calls_deepseek(tmp_path):
    service, session = make_service(tmp_path, "Cerveza Mahou 6x1L")
    service.run(["cerveza"])
    assert session.calls == 0
    deal = service.notifier.send.call_args.args[0]
    assert deal.price_per_liter == Decimal("12.50") / 6
    assert (
        service.repository.get_extraction("123")["extraction_source"] == "deterministic"
    )


def test_new_ambiguous_deal_calls_once_and_checks_identity_first(tmp_path):
    service, session = make_service(tmp_path)
    original_exists = service.repository.exists
    service.repository.exists = Mock(wraps=original_exists)
    original_post = session.post

    def post(*args, **kwargs):
        service.repository.exists.assert_called_once_with("123")
        return original_post(*args, **kwargs)

    session.post = post
    original_pricing = service.pricing.evaluate

    def pricing(deal, extraction):
        assert service.repository.get_extraction(deal.deal_id) is not None
        return original_pricing(deal, extraction)

    service.pricing.evaluate = pricing
    service.run(["cerveza"])
    assert session.calls == 1
    deal = service.notifier.send.call_args.args[0]
    assert deal.product_extraction.brand == "Mahou"
    assert deal.price == Decimal("12.50")


@pytest.mark.parametrize("rejected", [False, True])
def test_second_execution_uses_persistent_cache_even_for_rejected_deals(
    tmp_path, rejected
):
    service, first = make_service(tmp_path)
    rules = {"beer": InterestRule("beer", max_price=Decimal(1))} if rejected else None
    service.run(["cerveza"], rules=rules)
    assert first.calls == 1
    service.repository.db.close()
    second, session = make_service(tmp_path, responses=[])
    second.run(["cerveza"], rules=rules)
    assert session.calls == 0
    assert second.extraction_cache_hits == 1
    second.notifier.send.assert_not_called()


@pytest.mark.parametrize(
    "invalid",
    [{"price": 1}, [], {**payload(), "units": -1}, {**payload(), "confidence": 3}],
)
def test_invalid_deepseek_falls_back_and_is_cached(tmp_path, invalid):
    service, session = make_service(tmp_path, responses=[Response(invalid)])
    service.run(["cerveza"])
    expected = extract_product(service.client.recent()[0].product_text)
    assert service.repository.get_extraction("123") == expected.model_dump(mode="json")
    assert service.extractor.llm_failures == 1
    service.run(["cerveza"])
    assert session.calls == 1


def test_disabled_without_key_does_not_fail(tmp_path):
    assert create_extractor() is None
    service, session = make_service(tmp_path)
    service.extractor = None
    service.run(["cerveza"])
    assert session.calls == 0


def test_enabled_without_key_is_clear_configuration_error(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ENABLED", "true")
    service, _ = make_service(tmp_path)
    service.extractor = None
    service.client.reset_mock()
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY.*LLM_ENABLED=true"):
        service.run(["cerveza"])
    service.client.recent.assert_not_called()


def test_environment_configures_provider(monkeypatch):
    for key, value in {
        "LLM_ENABLED": "true",
        "DEEPSEEK_API_KEY": "test",
        "LLM_PROVIDER": "deepseek",
        "DEEPSEEK_MODEL": "custom",
        "DEEPSEEK_TIMEOUT_SECONDS": "3.5",
        "DEEPSEEK_MAX_RETRIES": "4",
    }.items():
        monkeypatch.setenv(key, value)
    provider = create_extractor()
    assert (provider.model, provider.timeout, provider.retries) == ("custom", 3.5, 4)


def test_dotenv_loads_without_overriding_environment(monkeypatch, tmp_path):
    from dotenv import load_dotenv

    from chollometro_alerts import config

    path = tmp_path / ".env"
    path.write_text(
        "LLM_ENABLED=true\nLLM_PROVIDER=deepseek\nDEEPSEEK_API_KEY=dotenv-key\nDEEPSEEK_MODEL=dotenv-model\n"
    )
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED")
    monkeypatch.delenv("LLM_ENABLED")
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    monkeypatch.delenv("LLM_PROVIDER")
    monkeypatch.setenv("DEEPSEEK_MODEL", "environment-model")
    monkeypatch.setattr(config, "load_dotenv", lambda *args: load_dotenv(path))
    provider = create_extractor()
    assert provider.api_key == "dotenv-key"
    assert provider.model == "environment-model"
    # dotenv writes directly to os.environ; register these for fixture cleanup.
    for key in ("LLM_ENABLED", "LLM_PROVIDER", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key)
