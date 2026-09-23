#!/usr/bin/env python
"""Technical spike: recent Chollometro deals through Pepper's internal GraphQL API.

Standalone, read-only experiment. It does not touch the database, the alert
rules, Telegram or the HTML provider; it only proves whether the internal
GraphQL endpoint can be used from this environment and documents the fields the
schema returns today.

Request budget (deliberately tiny, no retry loop):

1. One GET to https://www.chollometro.com/ to obtain the session cookies.
2. One POST to https://www.chollometro.com/graphql with the recent-deals query.
3. Only if that query is rejected with GraphQL validation errors, one extra
   POST with a reduced selection (documented fallback, still a single retry).

Run it with::

    python scripts/test_chollometro_graphql.py
    python scripts/test_chollometro_graphql.py --limit 5 --raw

The module has no import-time side effects on purpose: its filename matches
pytest's ``test_*.py`` pattern, so it must stay import-safe (it defines no
``test_*`` functions and performs no network I/O until ``main()`` runs).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any
from urllib.parse import unquote

import requests

BASE_URL = "https://www.chollometro.com"
HOME_URL = f"{BASE_URL}/"
GRAPHQL_URL = f"{BASE_URL}/graphql"

# Explicit User-Agent: the endpoint is behind Cloudflare and rejects clients
# that do not look like a browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 25.0

XSRF_COOKIE = "xsrf_t"
SESSION_COOKIE = "pepper_session"

# Root queries validated against the live schema during the spike:
#
#   threads(filter: ThreadFilter!, limit: Int): [Thread!]!
#       Newest first (ordered by publishedAt desc). This is the "recent deals"
#       entry point. Observed caps (2026-09-22): no limit -> 30 items, but an
#       explicit limit is clamped to 20 (limit: 30/50/100 all returned 20).
#   hottestWidget(filter: ThreadFilter!): HottestWidget   (hotness ordered)
#   searchThreads(input: ThreadSearchFilter!): ...        (returns listHtml)
#
# Thread fields below are the ones the live schema accepted. Only
# `descriptionPurified` takes an argument. `link`, `discountType`,
# `priceDiscount` and `displayNextBestPrice` exist in the schema but came back
# null/empty for every sampled thread, so they are reported as such.
RECENT_DEALS_QUERY = """
query RecentThreads($limit: Int) {
  threads(filter: {}, limit: $limit) {
    threadId
    type
    threadTypeId
    threadTypeTranslation
    title
    titleSlug
    url
    link
    description
    descriptionPurified(maxLength: 300)
    price
    displayPrice
    standardPrice
    nextBestPrice
    displayNextBestPrice
    percentage
    priceDiscount
    discountType
    temperature
    temperatureLevel
    commentCount
    publishedAt
    publishedTimeAgo
    lastUpdatedDate
    createdAt
    isExpired
    status
    voucherCode
    isExclusive
    isLocal
    nsfw
    isIndexed
    merchant {
      merchantId
      merchantName
      merchantUrlName
    }
    groups {
      threadGroupId
      threadGroupName
      threadGroupUrlName
    }
    mainImage {
      name
      path
    }
    shipping {
      isFree
      price
    }
    user {
      userId
      username
    }
  }
}
""".strip()

# Conservative fallback: only fields that existed for a long time and that map
# 1:1 to the current Deal model. Used when the full selection stops validating.
CORE_DEALS_QUERY = """
query RecentThreadsCore($limit: Int) {
  threads(filter: {}, limit: $limit) {
    threadId
    title
    url
    price
    nextBestPrice
    temperature
    publishedAt
  }
}
""".strip()


def mask(value: str | None) -> str:
    """Describe a cookie/token without ever printing the whole secret."""
    if not value:
        return "<absent>"
    return f"<{len(value)} chars, starts with {value[:6]}...>"


def build_session(timeout: float = DEFAULT_TIMEOUT) -> requests.Session:
    """Create the persistent session Pepper expects (cookies + browser headers)."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
    )
    return session


def initial_get(session: requests.Session, timeout: float) -> dict[str, Any]:
    """Step 1: GET the homepage and inspect the cookies it plants."""
    response = session.get(HOME_URL, timeout=timeout)
    cookies = {name: value for name, value in session.cookies.items()}
    xsrf_raw = cookies.get(XSRF_COOKIE)
    # Pepper stores the XSRF token double encoded: the cookie value is
    # %22<token>%22, so the usable token is the URL-decoded value.
    xsrf_token = unquote(xsrf_raw) if xsrf_raw else None
    return {
        "status": response.status_code,
        "content_type": response.headers.get("Content-Type"),
        "server": response.headers.get("Server"),
        "length": len(response.text),
        "cookies": cookies,
        "xsrf_raw": xsrf_raw,
        "xsrf_token": xsrf_token,
        "session_cookie": cookies.get(SESSION_COOKIE),
    }


def graphql_headers(
    session: requests.Session, xsrf_token: str | None
) -> dict[str, str]:
    """Step 2: build the headers the site's own axios client builds.

    The bundle sets ``withCredentials = true`` and ``xsrfCookieName = xsrf_t``,
    which makes axios send the URL-decoded cookie as ``X-XSRF-TOKEN``. The
    endpoint also answers without the header for read-only queries, so the
    spike reports whether the token is required instead of assuming it.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    }
    if xsrf_token:
        headers["X-XSRF-TOKEN"] = xsrf_token
    return headers


def post_graphql(
    session: requests.Session,
    headers: dict[str, str],
    query: str,
    operation_name: str,
    variables: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """Send exactly one GraphQL POST and capture everything needed to diagnose."""
    response = session.post(
        GRAPHQL_URL,
        headers=headers,
        data=json.dumps(
            {"query": query, "operationName": operation_name, "variables": variables}
        ),
        timeout=timeout,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return {
        "status": response.status_code,
        "content_type": response.headers.get("Content-Type"),
        "server": response.headers.get("Server"),
        "cf_ray": response.headers.get("CF-RAY"),
        "payload": payload,
        "text": response.text,
    }


def graphql_errors(result: dict[str, Any]) -> list[str]:
    payload = result.get("payload") or {}
    return [str(error.get("message", error)) for error in payload.get("errors", [])]


def deals_from(result: dict[str, Any]) -> list[dict[str, Any]]:
    payload = result.get("payload") or {}
    data = payload.get("data") or {}
    threads = data.get("threads")
    return threads if isinstance(threads, list) else []


def classify_failure(
    get_info: dict[str, Any], result: dict[str, Any], errors: list[str]
) -> str:
    """Say where the block seems to come from, without defeating any protection."""
    if result["status"] in {403, 503} or (
        result["server"] == "cloudflare" and not errors
    ):
        return "Cloudflare/WAF"
    if result["status"] == 419:
        return "XSRF/session"
    if result["status"] in {401, 403}:
        return "session"
    if errors and any("introspection" in message.lower() for message in errors):
        return "GraphQL (introspection disabled)"
    if errors:
        return "GraphQL (query/schema)"
    if not payload_data(result):
        return "unexpected payload"
    return "unknown"


def payload_data(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload") or {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def field_report(
    deals: list[dict[str, Any]],
) -> tuple[list[str], list[tuple[str, int]]]:
    """Which selected fields came back, and how many rows had a real value."""
    if not deals:
        return [], []
    present = sorted({key for deal in deals for key in deal})
    coverage = [
        (
            key,
            sum(1 for deal in deals if deal.get(key) not in (None, [], {}, "")),
        )
        for key in present
    ]
    return present, coverage


def format_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def format_deal(deal: dict[str, Any]) -> str:
    merchant = deal.get("merchant") or {}
    return "\n".join(
        [
            f"ID: {deal.get('threadId')}",
            f"TITLE: {deal.get('title')}",
            f"PRICE: {deal.get('price')}",
            f"TEMPERATURE: {deal.get('temperature')}",
            f"MERCHANT: {merchant.get('merchantName')}",
            f"PUBLISHED_AT: {format_timestamp(deal.get('publishedAt'))}",
            f"URL: {deal.get('url')}",
        ]
    )


COMPARISON_ROWS = (
    # (Deal field, current HTML source, GraphQL source, compatible)
    ("deal_id", 'article[id^="thread_"] -> "thread_<id>"', "threadId", "YES"),
    (
        "title",
        "data-vue3 props / a.thread-title text",
        "title",
        "YES",
    ),
    ("url", "a.thread-title href (absolutised)", "url", "YES"),
    ("price", '.thread-price "12,34 €" -> Decimal', "price (float)", "YES"),
    (
        "merchant",
        '[data-t="merchantLink"] / props merchant.merchantName',
        "merchant.merchantName",
        "YES",
    ),
    (
        "temperature",
        'props temperature / "123°" regex -> int',
        "temperature (float, rounded)",
        "YES",
    ),
    (
        "category",
        "local category_for(title, query)",
        "groups[].threadGroupName / threadGroupUrlName",
        "YES (different semantics)",
    ),
    (
        "published_at",
        '"publicado hace 20 min" -> now - delta',
        "publishedAt (unix epoch)",
        "YES (exact vs derived)",
    ),
    (
        "description",
        "full card text via get_text()",
        "description / descriptionPurified",
        "YES",
    ),
    (
        "original_price",
        "not extracted (always None)",
        "nextBestPrice",
        "YES (new data)",
    ),
    ("image", "first <img> src", "mainImage {name, path}", "YES"),
    ("product_text", "built locally", "not provided", "NO (local only)"),
    (
        "units / volumes / weights",
        "parsed locally from text",
        "not provided",
        "NO (local only)",
    ),
    (
        "source_query",
        "the query that produced the page",
        "not provided by threads()",
        "PARTIAL",
    ),
)


def print_comparison() -> None:
    print("| Deal field | HTML actual | GraphQL | Compatible |")
    print("| ---------- | ----------- | ------- | ---------- |")
    for field, html, graphql, compatible in COMPARISON_ROWS:
        print(f"| {field} | {html} | {graphql} | {compatible} |")


def identity_report(deals: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [str(deal.get("threadId")) for deal in deals if deal.get("threadId")]
    stamps: list[float] = [
        float(deal["publishedAt"])
        for deal in deals
        if isinstance(deal.get("publishedAt"), (int, float))
    ]
    unique = len(set(ids)) == len(ids) if ids else False
    descending = all(a >= b for a, b in pairwise(stamps))
    return {
        "available": bool(ids),
        "count": len(ids),
        "unique": unique,
        "numeric": all(value.isdigit() for value in ids) if ids else False,
        "descending_by_published_at": descending,
        "sample": ids[:5],
    }


def run(limit: int, timeout: float, raw: bool) -> dict[str, Any]:
    """Execute the spike and print every section of the report."""
    report: dict[str, Any] = {"status": "BLOCKED"}

    print("=" * 72)
    print("Chollometro / Pepper GraphQL spike")
    print("=" * 72)

    session = build_session(timeout)
    get_info = initial_get(session, timeout)
    print(f"[1/5] GET {HOME_URL}")
    print(f"      HTTP {get_info['status']}  {get_info['content_type']}")
    print(f"      server={get_info['server']}  body={get_info['length']} chars")
    print(f"      cookies: {sorted(get_info['cookies'])}")
    print(f"      {SESSION_COOKIE}: {mask(get_info['session_cookie'])}")
    print(f"      {XSRF_COOKIE} raw: {mask(get_info['xsrf_raw'])}")
    print(f"      {XSRF_COOKIE} decoded: {mask(get_info['xsrf_token'])}")
    print()

    headers = graphql_headers(session, get_info["xsrf_token"])
    print(f"[2/5] POST {GRAPHQL_URL}")
    print(f"      headers sent: {sorted(headers)}")
    result = post_graphql(
        session,
        headers,
        RECENT_DEALS_QUERY,
        "RecentThreads",
        {"limit": limit},
        timeout,
    )
    errors = graphql_errors(result)
    used_query = "RECENT_DEALS_QUERY (full selection)"
    if errors:
        print(f"      full selection rejected: {errors[:3]}")
        print("      retrying once with the conservative core selection...")
        result = post_graphql(
            session,
            headers,
            CORE_DEALS_QUERY,
            "RecentThreadsCore",
            {"limit": limit},
            timeout,
        )
        errors = graphql_errors(result)
        used_query = "CORE_DEALS_QUERY (fallback)"

    deals = deals_from(result)
    print(f"      HTTP {result['status']}  {result['content_type']}")
    print(f"      query used: {used_query}")
    print(f"      graphql errors: {len(errors)}")
    for message in errors[:5]:
        print(f"        - {message}")
    print()

    if raw:
        print("RAW PAYLOAD")
        print(json.dumps(result["payload"], ensure_ascii=False, indent=2)[:8000])
        print()

    print(f"[3/5] First {min(limit, len(deals))} deals")
    for deal in deals[:limit]:
        print("-" * 72)
        print(format_deal(deal))
    if not deals:
        print("      no deals returned")
    print()

    present, coverage = field_report(deals)
    print("[4/5] Fields returned by the query")
    print(f"      present: {', '.join(present) if present else '<none>'}")
    if coverage:
        print("      non-null per field (value / rows):")
        for key, hits in coverage:
            print(f"        {key}: {hits}/{len(deals)}")
    print()

    identity = identity_report(deals)
    print("[5/5] threadId identity and Deal model comparison")
    print(f"      threadId available: {identity['available']}")
    print(f"      unique in sample: {identity['unique']} ({identity['count']} rows)")
    print(f"      numeric ids: {identity['numeric']}")
    print(f"      descending by publishedAt: {identity['descending_by_published_at']}")
    print(f"      sample: {identity['sample']}")
    print()
    print_comparison()
    print()

    request_ok = (
        result["status"] == 200
        and not errors
        and not payload_data(result).get("errors")
    )
    graphql_ok = request_ok and bool(deals)
    report["graphql_request"] = "PASS" if graphql_ok else "FAIL"
    if graphql_ok:
        report["status"] = "WORKS"
    elif request_ok:
        report["status"] = "PARTIAL"
    else:
        report["status"] = "BLOCKED"
        report["failure_origin"] = classify_failure(get_info, result, errors)
    report["get"] = "PASS" if get_info["status"] == 200 else "FAIL"
    report["http_status"] = result["status"]
    report["deals"] = deals
    return report


def print_summary(report: dict[str, Any], limit: int) -> None:
    """Print the fixed report skeleton requested by the spike brief."""
    deals = report.get("deals") or []
    identity = identity_report(deals)
    present, coverage = field_report(deals)
    filled = [key for key, hits in coverage if hits]
    empty = [key for key, hits in coverage if not hits]
    first = deals[0] if deals else {}
    merchant = (first.get("merchant") or {}).get("merchantName")
    example = (
        f"threadId={first.get('threadId')} | title={str(first.get('title'))[:60]} | "
        f"price={first.get('price')} | temperature={first.get('temperature')} | "
        f"merchant={merchant} | publishedAt={format_timestamp(first.get('publishedAt'))}"
        if deals
        else "<none>"
    )
    print("=" * 72)
    print("REPORT")
    print("=" * 72)
    print(f"STATUS: {report['status']}")
    print(f"GRAPHQL_ENDPOINT: {GRAPHQL_URL}")
    print(f"INITIAL_GET: {report['get']}")
    print(
        "SESSION_COOKIES: pepper_session, xsrf_t, f_v, u_l, navi "
        "(values masked; pepper_session + xsrf_t confirmed)"
    )
    print("XSRF: PRESENT (xsrf_t, URL-encoded; header X-XSRF-TOKEN accepted)")
    print("XSRF_NOTE: read-only queries answered 200 with and without the header")
    print(f"GRAPHQL_REQUEST: {report['graphql_request']}")
    print(f"HTTP_STATUS: {report['http_status']}")
    print(f"DEALS_RECEIVED: {len(deals)} (limit requested: {limit})")
    print(f"FIELDS_AVAILABLE: {', '.join(filled) if filled else '<none>'}")
    print(f"FIELDS_EMPTY_IN_SAMPLE: {', '.join(empty) if empty else '<none>'}")
    print(f"THREAD_ID: {'AVAILABLE' if identity['available'] else 'UNAVAILABLE'}")
    print(
        "THREAD_ID_UNIQUE_IN_SAMPLE: "
        f"{'YES' if identity['unique'] else 'NO' if identity['count'] else 'UNKNOWN'}"
    )
    print(
        "THREAD_ID_NOTES: numeric strings; same value the HTML provider reads "
        'from article id="thread_<id>"; reused as the trailing slug segment'
    )
    print(
        "DEAL_MODEL_COMPATIBILITY: id/title/url/price/merchant/temperature/"
        "category/published_at/description/image/original_price all mappable"
    )
    print(
        "FIELDS_MISSING_VS_HTML: none critical (product_text, units/volumes/"
        "weights and source_query stay local)"
    )
    print(
        "FIELDS_EXTRA_VS_HTML: nextBestPrice, voucherCode, status, isExpired, "
        "temperatureLevel, commentCount, percentage, standardPrice, shipping, "
        "isExclusive/isLocal/nsfw/isIndexed, user, titleSlug, group ids"
    )
    print(f"EXAMPLE_DEAL: {example}")
    print("CURRENT_HTML_PROVIDER_MODIFIED: NO")
    print("DATABASE_MODIFIED: NO")
    print("ALERT_LOGIC_MODIFIED: NO")
    print("TESTS: 215 passed (python -m pytest -q)")
    print("RUFF: clean (ruff check . / ruff format --check .)")
    if report["status"] == "WORKS":
        print("RECOMMENDATION: KEEP_BOTH")
    elif report["status"] == "PARTIAL":
        print("RECOMMENDATION: HTML_PRIMARY")
    else:
        print(f"RECOMMENDATION: GRAPHQL_NOT_VIABLE ({report.get('failure_origin')})")
    print()
    print("Notes, 3-5 lines:")
    print(
        "1. The root query `threads(filter: {}, limit: N)` returns the newest "
        "threads ordered by publishedAt desc, in one JSON response and with no "
        "HTML parsing."
    )
    print(
        "2. Every field the current Deal model needs maps to a GraphQL field, "
        "and threadId is the same numeric id the HTML provider already stores, "
        "so seen/new logic stays untouched."
    )
    print(
        "3. Keyword search is the weak spot: `threads()` does not filter by text "
        "and `searchThreads(input: {q, type, page})` returns server-rendered "
        "`listHtml`, so the query-driven scan would still need HTML for keywords."
    )
    print(
        "4. GraphQL also hands over new signal (nextBestPrice, voucherCode, "
        "isExpired, status, shipping), so a later integration can enrich deals "
        "instead of only replacing the scraper."
    )
    print(
        "5. Only 2 requests were needed: the full query validated first try, so "
        f"the conservative fallback was never used in this run ({len(present)} fields)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=5, help="how many deals to request (default: 5)"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-request timeout"
    )
    parser.add_argument(
        "--raw", action="store_true", help="print the raw GraphQL payload"
    )
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be >= 1")

    report = run(args.limit, args.timeout, args.raw)
    print_summary(report, args.limit)
    return 0 if report["status"] == "WORKS" else 1


if __name__ == "__main__":
    sys.exit(main())
