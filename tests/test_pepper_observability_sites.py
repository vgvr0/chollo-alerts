from dataclasses import dataclass

import pytest
from prometheus_client import exposition

from chollometro_alerts.observability import ObservabilityState


@dataclass
class Summary:
    found: int = 1
    interesting: int = 0
    telegram_sent: int = 0
    errors: int = 0
    llm_calls: int = 0
    llm_failures: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_duration_seconds: float = 0.0


@pytest.mark.parametrize("site", ["promodescuentos", "pepper_nl", "pepperdeals_se"])
def test_metrics_accept_new_site_labels(site):
    state = ObservabilityState()
    state.record_run(Summary(), "SUCCESS", 0.1, 1, site=site, provider="pepper")

    output = exposition.generate_latest(state.registry).decode()

    assert f'site="{site}"' in output
