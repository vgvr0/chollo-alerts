from dataclasses import replace
from decimal import Decimal

import pytest
import requests
from test_deepseek import Response, payload
from test_llm_pipeline import make_service

from chollometro_alerts import cli
from chollometro_alerts.config import InterestRule
from chollometro_alerts.product import ProductExtraction
from chollometro_alerts.telegram import DryRunNotifier, format_message


def test_metrics_are_per_run_and_cached_sources_are_counted(tmp_path):
    service, session = make_service(tmp_path)
    service.run(["cerveza"])
    summary = service.last_summary
    assert (summary.found, summary.new, summary.llm_count, summary.telegram_sent) == (
        1,
        1,
        1,
        1,
    )
    assert (summary.llm_calls, summary.llm_tokens, summary.llm_failures) == (1, 17, 0)
    assert summary.llm_cache_hits == 0

    service.run(["cerveza"])
    summary = service.last_summary
    assert (summary.found, summary.new, summary.llm_count, summary.telegram_sent) == (
        1,
        0,
        1,
        0,
    )
    assert (summary.llm_calls, summary.llm_tokens, summary.llm_failures) == (0, 0, 0)
    assert summary.llm_cache_hits == 1
    assert session.calls == 1


def test_metrics_include_rejected_deterministic_deals(tmp_path):
    service, session = make_service(tmp_path, "Cerveza 6x1L")
    service.run(["cerveza"], rules={"beer": InterestRule("beer", max_price=Decimal(1))})
    summary = service.last_summary
    assert summary.found == summary.deterministic_count == summary.rejected == 1
    assert (
        summary.new
        == summary.llm_count
        == summary.hybrid_count
        == summary.telegram_sent
        == 0
    )
    assert summary.llm_calls == session.calls == 0
    service.notifier.send.assert_not_called()


def test_hybrid_cache_is_counted(tmp_path):
    service, session = make_service(tmp_path)
    extraction = ProductExtraction(**payload(), extraction_source="hybrid")
    service.repository.save_extraction("123", extraction.model_dump(mode="json"))
    service.run(["cerveza"])
    summary = service.last_summary
    assert summary.hybrid_count == summary.llm_cache_hits == 1
    assert summary.deterministic_count == summary.llm_count == summary.llm_calls == 0
    assert session.calls == 0


@pytest.mark.parametrize(
    "responses, calls, tokens",
    [
        ([requests.Timeout(), requests.Timeout(), requests.Timeout()], 3, 0),
        ([Response({"invalid": True})], 1, 17),
    ],
)
def test_fallback_metrics_and_retry_attempts(tmp_path, responses, calls, tokens):
    service, _ = make_service(tmp_path, responses=responses)
    service.run(["cerveza"])
    summary = service.last_summary
    assert summary.deterministic_count == summary.llm_failures == 1
    assert summary.llm_count == summary.hybrid_count == 0
    assert summary.llm_calls == calls
    assert summary.llm_tokens == tokens


def test_check_dry_run_prints_details_then_all_metrics(monkeypatch, tmp_path, capsys):
    service, _ = make_service(tmp_path, "Cerveza 6x1L")
    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli, "ChollometroClient", lambda: service.client)
    monkeypatch.setattr(cli, "DealRepository", lambda path: service.repository)
    monkeypatch.setattr(cli, "AlertService", lambda *args: service)
    monkeypatch.setattr(cli, "load_rules", dict)
    monkeypatch.setattr("sys.argv", ["chollometro-alerts", "check", "--dry-run"])
    cli.main()
    output = capsys.readouterr().out
    assert (
        "EXTRACTION_SOURCE=deterministic\nCONFIDENCE=0.98\nTOTAL_VOLUME_L=6" in output
    )
    assert f"PRICE_PER_LITER={Decimal('12.50') / 6}" in output
    assert output.endswith(
        "FOUND=1\nNEW=1\nDETERMINISTIC_COUNT=1\nLLM_COUNT=0\nHYBRID_COUNT=0\n"
        "LLM_CALLS=0\nLLM_CACHE_HITS=0\nLLM_FAILURES=0\nLLM_TOKENS=0\nTELEGRAM_SENT=0\n"
    )
    assert not service.repository.was_notified("123")


def test_dry_run_preserves_zero_and_reports_missing_values(tmp_path, capsys):
    service, _ = make_service(tmp_path)
    deal = replace(
        service.client.recent()[0],
        product_extraction=ProductExtraction(),
        price_per_liter=Decimal(0),
    )
    DryRunNotifier().send(deal)
    output = capsys.readouterr().out
    assert "CONFIDENCE=0\nTOTAL_VOLUME_L=N/D\nPRICE_PER_LITER=0\n" in output
    assert "CONFIDENCE" not in format_message(deal)


def test_empty_check_prints_zero_metrics(monkeypatch, tmp_path, capsys):
    service, _ = make_service(tmp_path)
    service.client.recent.return_value = []
    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli, "ChollometroClient", lambda: service.client)
    monkeypatch.setattr(cli, "DealRepository", lambda path: service.repository)
    monkeypatch.setattr(cli, "AlertService", lambda *args: service)
    monkeypatch.setattr(cli, "load_rules", dict)
    monkeypatch.setattr("sys.argv", ["chollometro-alerts", "check", "--dry-run"])
    cli.main()
    lines = capsys.readouterr().out.splitlines()
    # An empty-but-successful scan says so explicitly, next to its zero counters.
    assert lines[:2] == ["SCAN_STATUS=SUCCESS", "SCAN_ERROR_TYPE=N/D"]
    assert len(lines) == 12
    assert all(line.endswith("=0") for line in lines[2:])
