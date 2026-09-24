"""Opt-in, bounded probes for validating GraphQL ``threadId.in`` recovery.

This is an audit diagnostic, not production discovery code. It performs no
request unless ``--live`` is supplied, has a hard request budget, never
retries, and prints no cookies or tokens. The normal pytest suite never calls
the live path.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import requests

BASE_URL = "https://www.chollometro.com"
SELECTION = "threadId publishedAt createdAt type url"
MAX_REQUESTS = 12
DEFAULT_TIMEOUT = 15.0
KNOWN_OUT_OF_WINDOW_IDS = ("2013628",)
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "User-Agent": "chollometro-alerts-gap-audit/1.0",
}


def query(arguments: str, operation: str) -> str:
    return f"query {operation} {{ threads({arguments}) {{ {SELECTION} }} }}"


def summarise(payload: Any) -> tuple[int, list[str], list[str]]:
    if not isinstance(payload, dict):
        return 0, [], ["payload is not an object"]
    errors = payload.get("errors") or []
    messages = [
        str(error.get("message", error)) if isinstance(error, dict) else str(error)
        for error in errors
    ]
    rows = ((payload.get("data") or {}).get("threads")) if not errors else None
    if not isinstance(rows, list):
        return 0, [], messages or ["data.threads is not a list"]
    ids = [str(row.get("threadId")) for row in rows if isinstance(row, dict)]
    return len(rows), ids, messages


def analyse_ids(ids: list[str]) -> dict[str, Any]:
    numeric = [int(value) for value in ids if value.isdecimal()]
    deltas = [b - a for a, b in pairwise(numeric)]
    id_order_descending = all(
        int(ids[index]) >= int(ids[index + 1])
        for index in range(len(ids) - 1)
        if ids[index].isdecimal() and ids[index + 1].isdecimal()
    )
    return {
        "numeric": len(numeric) == len(ids),
        "unique": len(set(ids)) == len(ids),
        "adjacent_deltas": deltas,
        "numeric_range": (min(numeric), max(numeric)) if numeric else None,
        "id_order_descending": id_order_descending,
    }


def analyse_rows(payload: Any) -> dict[str, Any]:
    rows = (
        ((payload.get("data") or {}).get("threads"))
        if isinstance(payload, dict)
        else []
    )
    if not isinstance(rows, list):
        return {"rows": 0}
    numeric_rows = [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("threadId", "")).isdecimal()
    ]
    timestamps = [str(row.get("publishedAt", "")) for row in numeric_rows]
    ids = [int(row["threadId"]) for row in numeric_rows]
    types = sorted({str(row.get("type")) for row in numeric_rows})
    return {
        "rows": len(rows),
        "types": types,
        "published_descending": all(a >= b for a, b in pairwise(timestamps)),
        "id_descending_with_published": all(a >= b for a, b in pairwise(ids)),
        "id_sample": [str(row["threadId"]) for row in numeric_rows],
    }


class LiveProbe:
    def __init__(self, timeout: float, max_requests: int = MAX_REQUESTS):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.timeout = timeout
        self.max_requests = max_requests
        self.requests = 0
        self.last_payload: Any = None

    def post(self, operation: str, arguments: str) -> tuple[int, list[str], list[str]]:
        if self.requests >= self.max_requests:
            raise RuntimeError(f"hard request limit reached: {self.max_requests}")
        self.requests += 1
        response = self.session.post(
            f"{BASE_URL}/graphql",
            json={
                "operationName": operation,
                "query": query(arguments, operation),
            },
            timeout=self.timeout,
        )
        try:
            payload = response.json()
        except ValueError:
            self.last_payload = None
            return response.status_code, [], ["response is not JSON"]
        self.last_payload = payload
        _count, ids, errors = summarise(payload)
        return response.status_code, ids, errors


def run_live(timeout: float) -> int:
    probe = LiveProbe(timeout)
    home = probe.session.get(f"{BASE_URL}/", timeout=timeout)
    probe.requests += 1
    print(f"GET / -> HTTP {home.status_code}; budget={probe.max_requests}")
    if home.status_code >= 400:
        return 1

    status, ids, errors = probe.post("AuditWindow", "filter: {}")
    print(
        f"baseline -> HTTP {status} count={len(ids)} "
        f"first={ids[:1]} last={ids[-1:]} errors={errors[:2]}"
    )
    if errors or not ids:
        return 1
    print(f"id_analysis -> {analyse_ids(ids)}")
    print(f"row_analysis -> {analyse_rows(probe.last_payload)}")

    known = ids[:3]
    missing = str(max(int(value) for value in ids if value.isdecimal()) + 999999)
    probes = [
        ("known-one", f"filter: {{threadId: {{in: [{known[0]}]}}}}"),
        (
            "known-multi",
            f"filter: {{threadId: {{in: [{known[0]}, {known[1]}, {known[2]}]}}}}",
        ),
        (
            "known-reversed",
            f"filter: {{threadId: {{in: [{known[2]}, {known[0]}, {known[1]}]}}}}",
        ),
        (
            "mixed-existing-missing",
            f"filter: {{threadId: {{in: [{known[0]}, {missing}]}}}}",
        ),
    ]
    for name, arguments in probes:
        status, result, errors = probe.post(f"Audit{name.replace('-', '')}", arguments)
        print(f"{name} -> HTTP {status} ids={result} errors={errors[:2]}")

    out_of_window = KNOWN_OUT_OF_WINDOW_IDS[0]
    status, result, errors = probe.post(
        "AuditOutOfWindow", f"filter: {{threadId: {{in: [{out_of_window}]}}}}"
    )
    print(
        f"out-of-window id={out_of_window} -> HTTP {status} ids={result} "
        f"normal_window_contains={out_of_window in ids} errors={errors[:2]}"
    )

    candidates = [str(value) for value in range(int(ids[-1]), int(ids[0]) + 1)]
    status, result, errors = probe.post(
        "AuditLocalCandidateRange",
        f"filter: {{threadId: {{in: [{', '.join(candidates)}]}}}}",
    )
    print(
        f"local-range candidates={len(candidates)} -> HTTP {status} "
        f"existing={len(result)} density={len(result) / len(candidates):.3f} "
        f"errors={errors[:2]}"
    )

    for size in (1, 5, 10, 20):
        batch = known[:size] + [missing] * max(0, size - len(known))
        status, result, errors = probe.post(
            f"AuditBatch{size}",
            f"filter: {{threadId: {{in: [{', '.join(batch)}]}}}}",
        )
        print(
            f"batch-size={size} -> HTTP {status} count={len(result)} errors={errors[:2]}"
        )
    print(
        f"live_requests_made={probe.requests}; completed_at={datetime.now(UTC).isoformat()}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true", help="perform bounded live probes"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    if not args.live:
        print("No live requests made. Re-run with --live to opt in.")
        return 0
    return run_live(args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
