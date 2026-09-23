"""The verdict of one (rule, deal) observation, written to a real SQLite file.

`record_rule_observation_result` used to build its `UPDATE` from two
concatenated string literals whose first fragment ended in a comma. The
statement the daemon executed was therefore `... rejection_reason=?, WHERE
rule_id=? AND deal_id=?`, and SQLite rejected it with
`sqlite3.OperationalError: near "WHERE": syntax error`.

These tests exercise the method through a real connection instead of a mock:
the concatenation bug is a property of the SQL that reaches the database, so
only a real `execute` catches it.
"""

from chollometro_alerts.repository import DealRepository


def observation(repository, rule_id, deal_id):
    """One observation row as a dict, keyed by column name."""
    columns = [
        row[1]
        for row in repository.db.execute("PRAGMA table_info(rule_deal_observations)")
    ]
    row = repository.get_rule_observation(rule_id, deal_id)
    assert row is not None
    return dict(zip(columns, row))


def test_a_match_is_recorded_and_clears_the_baseline(tmp_path):
    repository = DealRepository(tmp_path / "observations.db")
    # The row a baseline leaves behind: seen, with no verdict yet.
    assert repository.claim_rule_observation(1, "d1", baseline=True) is True
    assert observation(repository, 1, "d1")["baseline"] == 1

    # The write itself is the assertion that the SQL parses: the dangling comma
    # raised `near "WHERE": syntax error` right here.
    repository.record_rule_observation_result(1, "d1", True)

    stored = observation(repository, 1, "d1")
    assert stored["matched"] == 1
    assert stored["baseline"] == 0
    assert stored["rejection_reason"] is None


def test_a_rejection_stores_its_reason(tmp_path):
    repository = DealRepository(tmp_path / "observations.db")
    repository.claim_rule_observation(1, "d1", baseline=True)

    repository.record_rule_observation_result(1, "d1", False, "precio por encima")

    stored = observation(repository, 1, "d1")
    assert stored["matched"] == 0
    assert stored["baseline"] == 0
    assert stored["rejection_reason"] == "precio por encima"


def test_a_later_match_clears_the_previous_rejection_reason(tmp_path):
    repository = DealRepository(tmp_path / "observations.db")
    repository.claim_rule_observation(1, "d1")
    repository.record_rule_observation_result(1, "d1", False, "sin stock")

    repository.record_rule_observation_result(1, "d1", True)

    stored = observation(repository, 1, "d1")
    assert stored["matched"] == 1
    assert stored["rejection_reason"] is None


def test_the_update_targets_the_rule_and_deal_that_were_named(tmp_path):
    repository = DealRepository(tmp_path / "observations.db")
    # Same rule, another deal, and the same deal under another rule.
    repository.claim_rule_observation(1, "d1", baseline=True)
    repository.claim_rule_observation(1, "d2", baseline=True)
    repository.claim_rule_observation(2, "d1", baseline=True)

    repository.record_rule_observation_result(1, "d1", False, "sin stock")

    changed = observation(repository, 1, "d1")
    assert changed["rejection_reason"] == "sin stock"
    assert changed["baseline"] == 0

    # The other pair of the same rule and the same deal of the other rule keep
    # their baseline: neither `matched` nor `rejection_reason` was written.
    for rule_id, deal_id in [(1, "d2"), (2, "d1")]:
        untouched = observation(repository, rule_id, deal_id)
        assert untouched["matched"] is None
        assert untouched["rejection_reason"] is None
        assert untouched["baseline"] == 1
