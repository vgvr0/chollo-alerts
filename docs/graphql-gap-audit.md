# GraphQL gap recovery audit

Audit date: 2026-09-24. Base: `80c137bc50f900a19bc33d0092c7278ffdcace15`.

## Conclusion

`GAP_POSSIBLE=YES`. The current scanner can permanently miss deals when more
threads are published between two cycles than the GraphQL window returns. The
scanner detects evidence of this condition, but detection is not recovery:
there is no recovery pass, no persisted `GAP_DETECTED` state, and no
`GAP_RECOVERED` state.

## Current flow

`GraphQLFeedClient.latest()` performs one feed request per cycle. The response
is mapped to the shared `Deal` model using `threadId` as identity. The service
deduplicates the response, checks `feed_threads`, evaluates only unseen deals,
persists each thread after its rule outcome is durable, and sends through the
existing extraction/rules/Telegram pipeline. A GraphQL failure uses the
existing per-rule HTML scans and does not update GraphQL discovery state.

The feed watermark stores the previous cycle's newest `publishedAt`. The
service logs `feed_window_gap` when the current oldest timestamp is newer than
that watermark, and logs `feed_window_risk reason=no_overlap` when a non-empty
window has zero overlap with an initialized non-empty `feed_threads` store.
These are logs, not recovery actions or durable recovery metrics.

## Reproduction

With a 30-thread window:

1. Cycle N records `known-0` through `known-29`.
2. Forty new threads appear: `burst-0` through `burst-39`.
3. Cycle N+1 returns only `burst-10` through `burst-39`.
4. The service evaluates and persists those 30 visible threads, while
   `burst-0` through `burst-9` are absent from the response.
5. A later newer window can push those ten threads out permanently.

The offline test `test_more_than_one_window_can_be_lost_without_recovery`
demonstrates exactly this behavior. It also verifies that the risk warning is
emitted, while the missing IDs are neither persisted nor notified.

## Live endpoint evidence

The bounded diagnostic was run against `https://www.chollometro.com/graphql`
using ordinary homepage GET plus GraphQL POST requests, with no retries or
authentication bypass:

| Probe | Observed result |
| --- | --- |
| no `limit` | 30 rows |
| `limit` 1, 10, 20 | exactly 1, 10, 20 rows |
| `limit` 21, 30 | 20 rows; silently capped |
| repeated no-limit request | 30 rows, identical order and IDs during the run |
| default order | `publishedAt` descending; no alternate sort demonstrated |
| `threadId.in` | works and returns requested existing IDs |
| `threadId.gt/ge/lt/le` | accepted but returned the unchanged default window |
| `after/before/offset/page/cursor` | rejected as unknown root arguments |
| `publishedAt` filter | rejected as undefined `ThreadFilter` field |

The current client had a separate defect: its explicit-limit query did not
declare `$limit`. That minimal defect was fixed and covered by a test; it was
not a gap-recovery feature.

## threadId.in recovery validation

The live probe is explicitly opt-in (`python scripts/audit_graphql_gap.py
--live`), has a hard budget of 12 HTTP requests including the homepage
handshake, uses a timeout, does not retry, and performs no remote writes. The
following results are from the focused run on 2026-09-24.

### OBSERVED

- The 30-row normal window contained numeric, unique IDs and was ordered by
  `publishedAt` descending.
- The sample contained both `Deal` and `Voucher` values in the `type` field.
- ID order was not monotonic with publication order. A large local jump from
  `2013641` to `2000248` appeared inside the sample.
- In the local interval `2013638..2013679`, 29 existing IDs were returned out
  of 42 numeric candidates (approximately 69% density). This is only a local
  sample, not a global density estimate.
- A single known ID, several known IDs, reversed known IDs, and a mixed batch
  of one existing plus one nonexistent ID were queried. Existing IDs were
  returned; nonexistent IDs were ignored.
- The reversed batch returned the server's normal order, not the request
  order.
- `2013628`, previously observed but absent from the current normal window,
  was returned by `threadId.in`. `OUT_OF_WINDOW_LOOKUP=PASS`.
- Batch payloads containing 1, 5, 10 and 20 candidate values were accepted.
  This demonstrates support for at least 20 values, not the server maximum.

### INFERRED

- `threadId.in` can reread a known thread after it has left the normal window.
- A numeric interval can generate candidates, but it will include holes and
  may include IDs for other content types. For the observed sample, candidate
  enumeration is a heuristic, not a time-range query.
- With a conservative simulated batch size of 20, candidate counts of 30,
  100, 500 and 1000 imply approximately 2, 5, 25 and 50 requests
  respectively.
- A restartable design would need to persist the gap identity, reference
  watermarks, candidate-set fingerprint, next batch index, request budget and
  explicit status. Existing `feed_threads`/deduplication could make processed
  IDs idempotent, but this state is not currently implemented.

### NOT PROVEN

- IDs are not proven to be monotonic by publication time or dense.
- A candidate interval between two IDs is not proven to contain every thread
  published between two watermarks.
- The maximum safe `threadId.in` batch size is unknown; `BATCH_LIMIT=UNKNOWN`.
- A successful `in` response does not prove that a gap was completely
  recovered. `GAP_RECOVERED` would require reaching a previously known
  boundary *and* a justified, fully covered candidate domain. Without that
  proof the state must remain `GAP_DETECTED` or
  `GAP_RECOVERY_INCOMPLETE`.

## Recovery options

| Option | Assessment |
| --- | --- |
| Real cursor/offset pagination | Not found; best option if the API later exposes it |
| `threadId` range filters | Not viable; live probes show them ignored |
| Time-range filters | Not available in `ThreadFilter` |
| `threadId.in` batches | Demonstrated for known IDs; unknown historical IDs still require bounded candidate enumeration, so end-to-end recovery is not yet proven |
| Larger window | Not reliable; explicit values cap at 20 and omitted limit is 30 |
| Faster adaptive polling | Mitigates probability/cost, cannot guarantee recovery |
| HTML recovery | Useful fallback/secondary source, but coverage and ordering are not equivalent to the GraphQL feed |
| Hybrid GraphQL + HTML | Best mitigation while recovery remains unproven; must retain source/coverage status and never claim a gap recovered without overlap evidence |

## Recommendation

Do not implement automatic recovery yet. First add opt-in, bounded live
instrumentation for candidate-ID `in` batches and measure whether a candidate
sequence can reliably reach a known `feed_threads` overlap across restarts and
non-dense IDs. If that experiment proves reliable, integrate it as:

`normal scan -> GAP_DETECTED -> bounded recovery batches -> shared dedupe and
normal pipeline -> overlap stop -> GAP_RECOVERED`.

Until then, use the current warnings as `GAP_DETECTED` only, combine them with
adaptive polling during high activity and a clearly labelled HTML recovery
scan, and keep `GAP_RECOVERED` unset.
