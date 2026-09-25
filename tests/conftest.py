import gc

import pytest

from chollometro_alerts.repository import DealRepository


@pytest.fixture(autouse=True)
def close_live_repository_connections():
    """Close repositories created by a test before pytest collects them."""
    yield
    for instance in gc.get_objects():
        if isinstance(instance, DealRepository):
            instance.close_current_thread()
