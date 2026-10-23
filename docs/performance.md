# Performance and conversation quality

## Measure a complete turn

Action traces now attach `performance` to terminal records: `total_ms`, cumulative
`phase_ms`, call counts, reply-fallback count and an explicit replay marker.
Paired model/tool records also have `duration_ms`. Provider timings are aggregated
without filling the bounded action log with successful journal/lease RPCs.
Labels contain no provider paths, owner text, record values or credentials.

`turn_finished` measures the assistant. For signed hosted webhooks,
`webhook_finished` includes reply delivery and has the same attempt reference.
Abrupt process termination can leave an attempt without a terminal record; it is
not a successful timing sample. These are application timings, not measurements
of the user's network, Cloud Run scheduling or cold-start time before ingress.

Export existing logs filtered by `jsonPayload.event="assistant_action_trace"`
and a selected revision/time window, then summarize the local JSON/JSONL file:

```sh
python scripts/summarize_performance.py /path/outside-checkout/traces.json
```

The report contains nearest-rank p50/p95, sample counts, result statuses, model
and provider call counts and the fraction of attempts using a reply fallback.
It deduplicates nested assistant/webhook summaries and excludes explicit receipt
replays by default (`--include-replays` includes them). Older traces without
metrics are skipped; older traces without replay markers cannot be separated.
Choose one deployment and boundary when comparing results: assistant-only and
webhook samples measure different things. Phase timings can overlap, so adding
all phases does not produce total latency. Small samples are diagnostic, not a
reliable production p95. This script does not fetch logs or call any provider.

## Fewer planning round trips

`query_sources` batches up to eight independent typed expense, Calendar or table
reads in one planner tool call. More reads can follow in later calls. Every
member keeps its own scope, completeness and request-local reference. The entire
argument list is checked before reading; mutation tools and dependent query refs
are unavailable inside a batch. A failed member never becomes an empty result
or evidence of a complete total. The planner inspects the results, then computes
with `finish_query_analysis` or SQL.

For example, comparing two months can use one batched read and one finish call,
instead of two read calls and one finish call. Optional answer composition is
unchanged. Batch schemas increase the prompt size; fewer planner calls do not
guarantee a particular wall-clock improvement. Provider reads remain ordered
under the existing owner lease; this does not introduce concurrent writes or
speculative classification. Activity-based spending remains sequential when
expenses depend on Calendar classification or an owner selection.

Successful read snapshots can be reused within a turn. The cache has at most
eight entries, copies returned records, is discarded at the next turn and is
invalidated before fenced effects (including model requests). Failed reads are
not cached. Full scans for different local table filters can share a source
snapshot; multiple expense windows in one batch share one validated Sheet read.
Configured table schemas already live in the in-process registry. This adds no
cross-turn data/metadata TTL and never bypasses fresh mutation/version checks.

## Waiting and clarification

After 0.8 seconds, the raw-history webhook can start one best-effort, 20-second
LINE loading animation for the verified active owner's private chat. Fast
answers cancel the timer. Groups, foreign owners, standby events, reported
redeliveries and locally known receipts are excluded. Only this fixed animation
endpoint is added; it cannot send a push or consume the reply token. The loading
request has a one-second budget, no retry and no business-state effect. Failures
do not replace or block the answer. Canary and reply-check profiles exclude it.
LINE displays this animation only while the user is viewing the private chat;
it is not a delivery acknowledgement or a notification.

Ordinary incomplete expense/Calendar/table drafts ask one missing field at a
time and retain the full draft for the final review. A version-bound question
focus tells the planner which pending record a short reply addresses. Validation
failures, invalid dates, uncertain results, historical-price offers and final
confirmation retain their detailed evidence contracts. The model still selects
tools and interprets language; the server does not classify owner intent with
new phrase rules. The final review displays all proposed effects before saving.

## Read only the matching Firestore scope

Generic Firestore tables can push one supported predicate to `runQuery` using
the existing bound collection and a read-only transaction. Equality supports
text, enum, reference, boolean, date and integer fields. Range predicates support
date and integer fields. Pushing one predicate avoids requiring a new composite
index. An excluded single-field index or provider error is reported as an
unavailable read; it does not silently trigger a different scan.

All remaining predicates, exact decimal comparisons/calculations, grouping,
sorting and display limits remain local. Decimal values are stored as strings;
lexicographic backend comparisons would be incorrect. Null equality, missing
fields, substring search and other unsupported predicates retain complete-scan
semantics. `describe_table` exposes the adapter's strategy and capabilities.

Every matching page is read in the same transaction with an explicit ordered
cursor; display limits do not limit aggregation. Record/byte limits apply to the
complete matching snapshot. Unknown, repeated or oversized pages fail instead
of reporting partial totals. A filtered query validates matched documents,
not unrelated documents outside its scope; it is not a whole-collection schema
audit. Mutation preparation and execution still use fresh complete/versioned
reads. SQL remains local over observed snapshots, not SQL sent to Firestore.

Sheets still reads and validates the configured grid; its values API does not
provide general row predicates. Request-local reuse reduces repeated transfers,
but this is not an indexed Sheets query or a persistent SQL mirror. The existing
specialized Firestore expense/catalog adapter retains its complete-scan path.

## Validate changes

Offline multi-turn acceptance scenarios cover clarification, rename/category
continuations, interruption, final review and duplicate delivery. Batch tests
compare the same computed result and call counts against sequential planning.
REST tests cover filtered scopes, exact residual predicates, pagination, unknown
responses and cache freshness. The optional official Firestore emulator checks
the actual indexed date-range/cursor contract over more than one page.

These scripted cases do not measure general model accuracy. Fresh inference,
when separately run with synthetic providers/data, is recorded outside the
public checkout. No production speed claim follows from offline tests.

References: [OpenAI latency optimization](https://developers.openai.com/api/docs/guides/latency-optimization),
[LINE loading animation](https://developers.line.biz/en/docs/messaging-api/use-loading-indicator/),
[Firestore runQuery](https://firebase.google.com/docs/firestore/reference/rest/v1/projects.databases.documents/runQuery).
