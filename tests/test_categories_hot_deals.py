from datetime import UTC, datetime, timedelta
from decimal import Decimal

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.alert_text import (
    extract_category_mentions,
    extract_max_age_minutes,
    merge_rule,
)
from chollometro_alerts.categories import CategoryRef, normalize_category
from chollometro_alerts.config import InterestRule
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.graphql_feed import thread_to_deal
from chollometro_alerts.models import Deal


def deal(**changes):
    values = {
        "deal_id": "1",
        "title": "Portátil",
        "url": "https://example.test/1",
        "price": Decimal(10),
        "merchant": "Amazon",
        "temperature": 250,
        "category": "generic",
        "published_at": datetime.now(UTC) - timedelta(minutes=20),
        "categories": (CategoryRef(id="10", slug="informatica", name="Informática"),),
    }
    values.update(changes)
    return Deal(**values)


def rule(**changes):
    values = {"category": "generic", "category_include": ("informatica",)}
    values.update(changes)
    return InterestRule(**values)


def test_category_normalization_and_include_exclude():
    assert normalize_category("  Informática ") == "informatica"
    assert apply_rule(deal(), rule()).accepted
    assert apply_rule(deal(), rule(category_exclude=("moda",))).accepted
    assert not apply_rule(deal(), rule(category_exclude=("informatica",))).accepted


def test_category_and_hot_deal_constraints_are_and_conditions():
    result = apply_rule(
        deal(), rule(temperature_min=200, include_merchants=("Amazon",))
    )
    assert result.accepted
    assert {check.code for check in result.checks} >= {
        "CATEGORY_INCLUDED",
        "MIN_TEMPERATURE",
        "MERCHANT_INCLUDED",
    }


def test_max_age_rejects_old_or_unknown_publication():
    assert apply_rule(deal(), rule(category_include=(), max_age_minutes=30)).accepted
    assert (
        apply_rule(
            deal(published_at=datetime.now(UTC) - timedelta(hours=2)),
            rule(category_include=(), max_age_minutes=30),
        ).reason
        == "REJECTED_AGE"
    )
    assert (
        apply_rule(
            deal(published_at=None), rule(category_include=(), max_age_minutes=30)
        ).reason
        == "REJECTED_UNKNOWN_AGE"
    )


def test_generic_alert_rule_has_no_query():
    alert = AlertRule(query=None, constraints=AlertConstraints(temperature_min=300))
    assert alert.query is None


def test_deterministic_category_language():
    mentions = extract_category_mentions("electrónica pero no telefonía")
    assert mentions.included == ("electronica",)
    assert mentions.excluded == ("telefonia",)
    merged = merge_rule(AlertRule(query=None), "solo supermercado")
    assert merged.constraints.category_include == ("supermercado",)
    assert extract_max_age_minutes("publicado hace menos de 2 horas") == 120
    assert (
        merge_rule(
            AlertRule(query=None), "alimentación de la última hora"
        ).constraints.max_age_minutes
        == 60
    )


def test_graphql_structured_group_fields_are_carried_without_extra_request():
    row = {
        "threadId": "1",
        "title": "Portátil",
        "url": "https://example.test/1",
        "price": 10,
        "temperature": 301,
        "publishedAt": 1770000000,
        "groups": [
            {
                "threadGroupId": "42",
                "threadGroupName": "Informática",
                "threadGroupUrlName": "informatica",
            }
        ],
    }
    mapped = thread_to_deal(row)
    assert mapped.categories[0].id == "42"
    assert mapped.categories[0].slug == "informatica"
