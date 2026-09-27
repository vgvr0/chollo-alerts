import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import urlopen

from chollometro_alerts.observability import ObservabilityServer, ObservabilityState


@dataclass
class Summary:
    found: int = 12
    interesting: int = 3
    telegram_sent: int = 2
    errors: int = 0
    llm_calls: int = 2
    llm_failures: int = 0
    llm_input_tokens: int = 10
    llm_output_tokens: int = 4
    llm_duration_seconds: float = 0.75


class Repository:
    def runtime_status(self):
        return {}

    def list_alert_rules(self, enabled_only=False):
        return [(1,), (2,)]


def get(server, path):
    with urlopen(f"http://127.0.0.1:{server.httpd.server_port}{path}") as response:
        return response.status, json.load(response)


def test_http_health_endpoints_and_metrics():
    state = ObservabilityState()
    server = ObservabilityServer(state, Repository(), host="127.0.0.1", port=0)
    server.start()
    try:
        assert get(server, "/health/live") == (200, {"status": "ok"})
        assert get(server, "/health/ready") == (
            200,
            {"status": "ready", "database": "ok"},
        )
        state.mark_scan_started()
        state.record_run(
            Summary(),
            "SUCCESS",
            1.25,
            2,
            site="example-site",
            provider="example-provider",
        )
        status, health = get(server, "/health")
        assert status == 200
        assert health["last_run"] == {
            "status": "completed",
            "finished_at": health["last_run"]["finished_at"],
            "deals_seen": 12,
            "alerts_matched": 3,
            "notifications_sent": 2,
        }
        assert health["last_scan_at"] is not None
        assert health["last_success_at"] is not None
        with urlopen(
            f"http://127.0.0.1:{server.httpd.server_port}/metrics"
        ) as response:
            metrics = response.read().decode()
        assert 'chollometro_runs_total{status="completed"} 1.0' in metrics
        assert "chollometro_deals_processed_total 12.0" in metrics
        assert "chollometro_run_duration_seconds_count 1.0" in metrics
        assert (
            'chollometro_runs_by_site_total{provider="example-provider",site="example-site",status="completed"} 1.0'
            in metrics
        )
        assert "chollometro_llm_requests_total 2.0" in metrics
        assert "chollometro_llm_input_tokens_total 10.0" in metrics
        assert "chollometro_llm_output_tokens_total 4.0" in metrics
        assert "chollometro_llm_request_duration_seconds_count 1.0" in metrics
        assert "alert_id" not in metrics
        assert "chat_id" not in metrics
    finally:
        server.close()


def test_readiness_failure_returns_503_and_success_timestamp_is_stable():
    class BrokenRepository:
        def runtime_status(self):
            raise OSError("db unavailable")

    state = ObservabilityState()
    server = ObservabilityServer(state, BrokenRepository(), host="127.0.0.1", port=0)
    server.start()
    try:
        try:
            get(server, "/health/ready")
        except HTTPError as error:
            assert error.code == 503
            assert json.load(error) == {"status": "not_ready", "database": "error"}
        else:
            raise AssertionError("readiness unexpectedly succeeded")
        assert state.last_success_timestamp == 0
        state.mark_scan_started()
        assert state.last_scan_timestamp > 0
        state.record_run(Summary(), "FAILED", 0.5, 0)
        assert state.last_success_timestamp == 0
    finally:
        server.close()
