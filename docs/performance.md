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

## Finish before the deadline

Raw-history model calls share the 50-second foreground assistant scope and
leave ten seconds inside it for durable accounting, response receipts and
owner-lease release. Every model transport timeout honors that reserve. The
planner stops before another call when its model window is spent; retained
partial facts remain available, and unfinished analysis is not reported as a
complete answer. Optional reply composition uses the same remaining model
window and falls back to the checked core reply rather than spending the
persistence reserve. This does not extend deadlines, retry unknown outcomes,
clear reservations or authorize a write.

LINE delivery starts only after the assistant receipt is persisted and has a
separate ten-second RPC scope. Its existing 55-second local token deadline
still applies. Cloud Run must allow the entire request, including cold start;
the offline release gate requires at least 90 seconds. Increasing that ingress
timeout alone cannot prevent a model from exhausting application persistence
time. The [deployment procedure](deployment.md) checks both boundaries.

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

Expense list validation accepts repeated price occurrences and separates row
ordinals from money evidence. This avoids unnecessary planning repairs for
valid lists and combined date/amount corrections. Actual argument mistakes
still return to the planner; repeated failures stop. Offline call-count checks
cover these paths, without establishing a production latency improvement.

New expense proposals can include an interpreted date with its original
quotation in the preparation call. The existing temporal validator produces
the retained proof without another planner request. A genuinely ambiguous
date instead retains the known facts for a focused owner question.

Recoverable preparation feedback can offer a checked reply context and
`finish_preparation_reply`. The repair planner can finish the reply using the
existing document schema and fact renderer, avoiding a subsequent composer
request. An invalid toolless exit uses the checked core fallback directly.
A successful repair clears the earlier reply context and follows the normal
review/composition path. These changes reduce specific round trips; they do
not make every turn a single model call or establish a general speed target.

## Reduce workflow-state round trips

Each Firestore session reads the owner document afresh and acquires its lease
with a conditional commit. Subsequent checkpoints and lease release use the
last acknowledged document's `updateTime` as the write precondition, rather
than reading that same lease before every write. Any intervening document
change rejects the entire checkpoint. The cached version lives only inside
the current session; it cannot authorize another turn. Explicit migration
checks still read fresh. A lost commit acknowledgement still requires exact
nonce/field readback; an unresolved outcome cannot authorize an external effect.
An intervening owner-document write can interrupt the turn with HTTP 503:
the worker cannot adopt that version to save a reply. A redelivery reacquires
fresh state after lease expiry and reconciles effects; missing proof remains
uncertain and does not cause an automatic business-write retry.
The lease is checked again after the pre-effect commit returns, so a slow
acknowledgement cannot authorize work after that renewed lease has expired.

LINE delivery persists a new reply and its executing marker together in the
pre-effect checkpoint. There is no provider I/O between these assignments.
Expiry, delivery outcome and lease release remain durable. Existing prepared
replies remain readable; an executing/uncertain reply is never sent again.
This changes neither the state schema nor confirmation requirements.

Paired offline replays across four tasks, two previously recorded model-output
sets and two rounds per task use identical model inputs before and after the
change. All 16 cases per version pass; average Firestore RPC count falls from
49.125 to 32.5 (33.8%). These are synthetic, already-initialized signed webhooks
with fake Google/LINE services and recorded model outputs, not live model or
production latency measurements. RPC counts are not billed document-operation
counts. Lease takeover, lost acknowledgements, reply crash/replay and atomic
evidence conflicts have separate regression coverage; the official emulator
checks provider preconditions and stale-worker release.

## Waiting and clarification

Successful expense save/edit replies show a text summary without automatic
post-save action menus. Reviews retain their confirmation controls. Operation
documents separate status, date groups and instructions with paragraph spacing.
Routine reviews show the proposed records without an unsolicited same-day
history comparison. Stored amounts remain available for requested reuse and
missing-amount choices; relevant evidence is retained internally for freshness.
Adjacent row blocks may share an unchanged heading only when exact cell group
keys match, with no intervening prose. A different date needs its own heading
or visible cell. Model-generated em dash separators become spaces before fact
substitution, so literal names and notes keep their original punctuation.

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
