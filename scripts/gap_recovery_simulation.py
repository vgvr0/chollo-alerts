"""Offline-only model for candidate enumeration and recovery stopping rules."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil


class CandidateLimitError(ValueError):
    """The inferred numeric range is too large for a bounded experiment."""


def candidate_ids_between(
    before_id: str, after_id: str, *, max_candidates: int
) -> list[str]:
    if not before_id.isdecimal() or not after_id.isdecimal():
        raise ValueError("candidate range requires numeric IDs")
    start, end = int(before_id), int(after_id)
    if end <= start:
        raise ValueError("after_id must be greater than before_id")
    count = end - start - 1
    if count > max_candidates:
        raise CandidateLimitError(count)
    return [str(value) for value in range(start + 1, end)]


def estimated_requests(candidate_count: int, batch_size: int) -> int:
    if candidate_count < 0 or batch_size < 1:
        raise ValueError("counts must be non-negative and batch_size must be positive")
    return ceil(candidate_count / batch_size)


@dataclass(frozen=True)
class RecoverySimulation:
    requests: int
    next_batch: int
    recovered_ids: tuple[str, ...]
    overlap_ids: tuple[str, ...]
    status: str


def simulate_recovery(
    candidate_ids: list[str],
    existing_ids: set[str],
    known_ids: set[str],
    *,
    batch_size: int,
    start_batch: int = 0,
    max_requests: int = 10,
) -> RecoverySimulation:
    unique = list(dict.fromkeys(candidate_ids))
    if batch_size < 1 or max_requests < 1:
        raise ValueError("batch_size and max_requests must be positive")
    recovered: list[str] = []
    overlap: list[str] = []
    batches = [
        unique[index : index + batch_size]
        for index in range(0, len(unique), batch_size)
    ]
    for batch_index in range(
        start_batch, min(len(batches), start_batch + max_requests)
    ):
        returned = [value for value in batches[batch_index] if value in existing_ids]
        overlap.extend(value for value in returned if value in known_ids)
        recovered.extend(value for value in returned if value not in known_ids)
        if overlap:
            return RecoverySimulation(
                batch_index - start_batch + 1,
                batch_index + 1,
                tuple(dict.fromkeys(recovered)),
                tuple(dict.fromkeys(overlap)),
                "GAP_RECOVERED",
            )
    next_batch = min(start_batch + max_requests, len(batches))
    status = "GAP_RECOVERY_INCOMPLETE" if next_batch < len(batches) else "GAP_DETECTED"
    return RecoverySimulation(
        next_batch - start_batch,
        next_batch,
        tuple(dict.fromkeys(recovered)),
        tuple(dict.fromkeys(overlap)),
        status,
    )
