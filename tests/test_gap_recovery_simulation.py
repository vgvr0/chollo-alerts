from __future__ import annotations

import pytest

from scripts.gap_recovery_simulation import (
    CandidateLimitError,
    candidate_ids_between,
    estimated_requests,
    simulate_recovery,
)


def test_candidate_ranges_are_numeric_but_not_assumed_dense():
    assert candidate_ids_between("100", "105", max_candidates=10) == [
        "101",
        "102",
        "103",
        "104",
    ]


def test_candidate_range_has_a_hard_limit():
    with pytest.raises(CandidateLimitError):
        candidate_ids_between("100", "201", max_candidates=10)


def test_estimated_requests_are_local_only():
    assert estimated_requests(0, 20) == 0
    assert estimated_requests(30, 20) == 2
    assert estimated_requests(500, 20) == 25
    assert estimated_requests(1000, 20) == 50


def test_recovery_deduplicates_existing_and_missing_ids():
    result = simulate_recovery(
        ["101", "101", "102", "999"],
        {"101", "999"},
        {"999"},
        batch_size=2,
    )
    assert result.recovered_ids == ("101",)
    assert result.overlap_ids == ("999",)
    assert result.status == "GAP_RECOVERED"


def test_partial_recovery_can_resume_after_restart():
    candidates = [str(value) for value in range(100, 140)]
    first = simulate_recovery(
        candidates,
        {"110", "135"},
        {"135"},
        batch_size=10,
        max_requests=1,
    )
    assert first.status == "GAP_RECOVERY_INCOMPLETE"
    resumed = simulate_recovery(
        candidates,
        {"110", "135"},
        {"135"},
        batch_size=10,
        start_batch=first.next_batch,
    )
    assert resumed.status == "GAP_RECOVERED"
    assert resumed.overlap_ids == ("135",)


def test_exhausting_candidates_without_overlap_is_not_recovered():
    result = simulate_recovery(
        ["101", "102", "103"],
        {"102"},
        {"999"},
        batch_size=2,
    )
    assert result.recovered_ids == ("102",)
    assert result.overlap_ids == ()
    assert result.status == "GAP_DETECTED"
