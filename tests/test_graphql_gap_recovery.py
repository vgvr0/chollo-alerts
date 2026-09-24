from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_graphql_discovery import (
    AFTER,
    FakeFeed,
    add_rule,
    make_deal,
    make_service,
)
from test_provider_resilience import Response, Transport, page

from chollometro_alerts.client import ChollometroClient
from chollometro_alerts.config import ChollometroSettings
from chollometro_alerts.errors import ChollometroHTTPError
from chollometro_alerts.service import (
    GAP_RECOVERED,
    GAP_RECOVERY_FAILED,
    GAP_RECOVERY_INCOMPLETE,
    AlertService,
)


class RecoveryFeed(FakeFeed):
    def __init__(self, *batches, max_pages=5):
        super().__init__(*batches)
        self.feed_settings = SimpleNamespace(
            gap_recovery_enabled=True,
            gap_recovery_max_pages=max_pages,
        )


class PaginatedHtml:
    def __init__(self, pages):
        self.pages = pages
        self.feed_calls = []
        self.last_scan = None
        self.last_search = {}

    def recent(self, queries, pages=1):
        raise AssertionError("legacy HTML fallback must not run")

    def feed_page(self, page):
        self.feed_calls.append(page)
        value = self.pages[page - 1]
        if isinstance(value, Exception):
            raise value
        return list(value)


def seed_history(tmp_path):
    first = [make_deal("known-1", AFTER - timedelta(hours=1))]
    feed = RecoveryFeed(first)
    html = PaginatedHtml([])
    service, repository, notifier, extractor = make_service(tmp_path, feed, html=html)
    add_rule(repository)
    assert service.run_active_rules() == 0
    return service, repository, notifier, extractor


def gap_batch(prefix, count, start=0):
    return [
        make_deal(
            f"{prefix}-{index}",
            AFTER + timedelta(minutes=start + index),
        )
        for index in range(count)
    ]


def test_no_gap_does_not_request_new_pages(tmp_path):
    first = [make_deal("known-1", AFTER - timedelta(hours=1))]
    current = [make_deal("fresh", AFTER), *first]
    feed = RecoveryFeed(first, current)
    html = PaginatedHtml([])
    service, repository, _notifier, _extractor = make_service(tmp_path, feed, html=html)
    add_rule(repository)

    service.run_active_rules()
    service.run_active_rules()

    assert html.feed_calls == []
    assert service.last_gap_recovery["status"] == "NO_GAP"


def test_gap_of_ten_recovers_page_two_and_stops_at_boundary(tmp_path):
    service, repository, _notifier, _extractor = seed_history(tmp_path)
    graph = gap_batch("graph", 30)
    page_one = gap_batch("lost", 30, start=100)
    page_two = [
        *gap_batch("lost-page-two", 10, start=130),
        make_deal("known-1", AFTER - timedelta(hours=1)),
    ]
    service.feed.batches.append(graph)
    service.client.pages = [page_one, page_two]

    service.run_active_rules()

    assert service.last_gap_recovery["status"] == GAP_RECOVERED
    assert service.client.feed_calls == [1, 2]
    assert service.last_gap_recovery["deals"] == 40
    assert repository.seen_feed_thread_ids() >= {
        deal.deal_id for deal in graph + page_one
    }


def test_budget_exhaustion_is_not_recovered(tmp_path):
    service, _repository, _notifier, _extractor = seed_history(tmp_path)
    service.feed.feed_settings.gap_recovery_max_pages = 2
    service.feed.batches.append(gap_batch("graph", 30))
    service.client.pages = [gap_batch("lost", 30), gap_batch("lost", 30, 30)]

    service.run_active_rules()

    assert service.last_gap_recovery["status"] == GAP_RECOVERY_INCOMPLETE
    assert service.last_gap_recovery["boundary_found"] is False
    assert service.gap_recovery_metrics["gap_recovery_incomplete_total"] == 1


def test_intermediate_page_failure_is_failed_closed(tmp_path):
    service, _repository, _notifier, _extractor = seed_history(tmp_path)
    service.feed.batches.append(gap_batch("graph", 30))
    service.client.pages = [gap_batch("lost", 30), ChollometroHTTPError("boom")]

    service.run_active_rules()

    assert service.last_gap_recovery["status"] == GAP_RECOVERY_FAILED
    assert service.last_gap_recovery["boundary_found"] is False
    assert service.gap_recovery_metrics["gap_recovery_failures_total"] == 1


def test_first_page_failure_is_failed_closed(tmp_path):
    service, _repository, _notifier, _extractor = seed_history(tmp_path)
    service.feed.batches.append(gap_batch("graph", 30))
    service.client.pages = [ChollometroHTTPError("boom")]

    service.run_active_rules()

    assert service.last_gap_recovery["status"] == GAP_RECOVERY_FAILED
    assert service.last_gap_recovery["pages"] == 0


def test_baseline_never_starts_gap_recovery(tmp_path):
    feed = RecoveryFeed([make_deal("first", AFTER)], max_pages=5)
    html = PaginatedHtml([])
    service, repository, _notifier, _extractor = make_service(tmp_path, feed, html=html)
    add_rule(repository)

    service.run_active_rules()

    assert html.feed_calls == []
    assert service.last_gap_recovery["status"] == "NO_GAP"


def test_feed_page_reuses_parser_and_requests_nuevos_route():
    transport = Transport(Response(200, page("older-1", "older-2")))
    client = ChollometroClient(transport, settings=ChollometroSettings())

    deals = client.feed_page(2)

    assert [deal.deal_id for deal in deals] == ["older-1", "older-2"]
    assert transport.requests == [
        {"url": "https://www.chollometro.com/nuevos?page=2", "timeout": 20}
    ]


def test_disabled_recovery_preserves_legacy_no_html_requests(tmp_path):
    first = [make_deal("known-1", AFTER - timedelta(hours=1))]
    feed = FakeFeed(first, gap_batch("graph", 30))
    html = PaginatedHtml([gap_batch("lost", 30)])
    service, repository, _notifier, _extractor = make_service(tmp_path, feed, html=html)
    add_rule(repository)

    service.run_active_rules()
    service.run_active_rules()

    assert html.feed_calls == []
    assert service.last_gap_recovery["status"] == "GAP_DETECTED"


def test_restart_after_partial_recovery_is_idempotent(tmp_path):
    service, repository, notifier, extractor = seed_history(tmp_path)
    service.feed.feed_settings.gap_recovery_max_pages = 1
    graph = gap_batch("graph", 30)
    page_one = gap_batch("lost", 30)
    service.feed.batches.append(graph)
    service.client.pages = [page_one]
    service.run_active_rules()
    sent_before_restart = list(notifier.sent_ids)

    restarted_feed = RecoveryFeed(gap_batch("graph-restart", 30), max_pages=2)
    restarted_html = PaginatedHtml(
        [page_one, [make_deal("known-1", AFTER - timedelta(hours=1))]]
    )
    restarted_service = AlertService(
        restarted_html,
        repository,
        notifier,
        extractor,
        feed=restarted_feed,
    )
    restarted_service.run_active_rules()

    assert restarted_service.last_gap_recovery["status"] == GAP_RECOVERED
    assert notifier.sent_ids.count("lost-0") == sent_before_restart.count("lost-0")
    assert len(notifier.sent_ids) >= len(sent_before_restart)


@pytest.mark.parametrize("count", [40, 100])
def test_recovered_deals_are_newest_first_and_unique(tmp_path, count):
    service, repository, _notifier, _extractor = seed_history(tmp_path)
    service.feed.feed_settings.gap_recovery_max_pages = 5
    service.feed.batches.append(gap_batch("graph", 30))
    pages = [
        gap_batch(f"lost-page-{page}", 30, start=page * 30)
        for page in range((count + 29) // 30)
    ]
    pages[-1].append(make_deal("known-1", AFTER - timedelta(hours=1)))
    service.client.pages = pages

    service.run_active_rules()

    assert service.last_gap_recovery["status"] == GAP_RECOVERED
    assert service.last_gap_recovery["deals"] >= count
    assert len(repository.seen_feed_thread_ids()) == (
        1 + 30 + service.last_gap_recovery["deals"]
    )
