"""Historical thread identity and explicit reactivation behavior."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import test_graphql_discovery as discovery

from chollometro_alerts.alert_rule import AlertConstraints
from chollometro_alerts.config import RetentionSettings
from chollometro_alerts.models import Deal
from chollometro_alerts.retention import RetentionService


def _rule(repo):
    return discovery.add_rule(
        repo,
        query="leche",
        created_at=discovery.T0,
        constraints=AlertConstraints(max_price=15),
    )


def _old_deal(deal_id="R", **kwargs):
    return discovery.make_deal(deal_id, discovery.AFTER, title="Leche R", **kwargs)


def _prune(repo):
    now = discovery.T0 + timedelta(days=40)
    return RetentionService(
        repo,
        RetentionSettings(batch_size=10),
        clock=lambda: now,
    ).run(now=now)


def test_unchanged_historical_thread_survives_prune_without_notification(tmp_path):
    deal = _old_deal()
    service, repo, notifier, _ = discovery.make_service(
        tmp_path, discovery.FakeFeed([deal], [deal])
    )
    _rule(repo)

    assert service.run_active_rules() == 1
    assert _prune(repo).deleted_deals == 1
    assert (
        repo.db.execute(
            "SELECT COUNT(*) FROM feed_threads WHERE site=? AND thread_id=?",
            ("chollometro", "R"),
        ).fetchone()[0]
        == 1
    )
    assert service.run_active_rules() == 0
    assert notifier.sent_ids == ["R"]


def test_explicit_inactive_to_active_reactivation_re_evaluates_and_notifies(tmp_path):
    active = _old_deal(status="Activated", is_expired=False)
    inactive = _old_deal(status="Expired", is_expired=True)
    service, repo, notifier, _extractor = discovery.make_service(
        tmp_path, discovery.FakeFeed([active], [inactive], [active])
    )
    _rule(repo)

    assert service.run_active_rules() == 1
    assert service.run_active_rules() == 0
    assert _prune(repo).deleted_deals == 1
    assert service.run_active_rules() == 1
    assert notifier.sent_ids == ["R", "R"]
    assert service.last_summary.reactivations_detected == 1
    assert service.last_summary.reactivations_notified == 1
    assert service.last_summary.classified >= 1
    assert (
        repo.db.execute(
            "SELECT reactivation_count FROM feed_threads WHERE site=? AND thread_id=?",
            ("chollometro", "R"),
        ).fetchone()[0]
        == 1
    )


def test_reactivation_not_matching_current_rule_is_not_notified(tmp_path):
    active = _old_deal(price="10", status="Activated", is_expired=False)
    inactive = _old_deal(price="10", status="Expired", is_expired=True)
    expensive = _old_deal(price="99", status="Activated", is_expired=False)
    service, repo, notifier, _ = discovery.make_service(
        tmp_path, discovery.FakeFeed([active], [inactive], [expensive])
    )
    _rule(repo)

    assert service.run_active_rules() == 1
    service.run_active_rules()
    _prune(repo)
    assert service.run_active_rules() == 0
    assert notifier.sent_ids == ["R"]


def test_same_thread_id_is_site_aware_for_history(tmp_path):
    repo = discovery.DealRepository(tmp_path / "sites.db")
    for site in ("chollometro", "promodescuentos"):
        repo.record_feed_threads(
            [
                Deal(
                    "same",
                    "same",
                    "https://example.test/same",
                    Decimal(1),
                    "shop",
                    1,
                    "other",
                    datetime.now(UTC),
                    site=site,
                    is_expired=True,
                    status="Expired",
                )
            ]
        )
    assert repo.feed_thread_count("chollometro") == 1
    assert repo.feed_thread_count("promodescuentos") == 1
    assert (
        repo.classify_feed_threads(
            [
                _old_deal(
                    "same", site="chollometro", status="Activated", is_expired=False
                )
            ]
        )["same"]
        == "REACTIVATED"
    )
    assert (
        repo.classify_feed_threads(
            [
                _old_deal(
                    "same", site="promodescuentos", status="Expired", is_expired=True
                )
            ]
        )["same"]
        == "KNOWN_UNCHANGED"
    )


def test_prune_dry_run_preserves_deal_and_thread_identity(tmp_path):
    deal = _old_deal()
    _service, repo, _notifier, _ = discovery.make_service(
        tmp_path, discovery.FakeFeed([deal])
    )
    repo.record_feed_threads([deal])
    repo.upsert(deal)
    now = discovery.T0 + timedelta(days=40)
    service = RetentionService(
        repo, RetentionSettings(batch_size=10), clock=lambda: now
    )
    preview = service.run(dry_run=True, now=now)
    assert preview.deleted_deals == 1
    assert repo.db.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 1
    result = service.run(now=now)  # real cleanup proves the identity remains
    assert result.deleted_deals == 1
    assert repo.db.execute("SELECT COUNT(*) FROM feed_threads").fetchone()[0] == 1
