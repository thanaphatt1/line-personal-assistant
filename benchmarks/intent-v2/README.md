# Current intent evaluation v2

This suite separates query execution from semantic outcome. A valid SQL result
and the model's `selection_matches=true` can still omit “ไม่รวมเครื่องดื่ม”,
count distinct dates instead of payment occurrences, multiply payments by
same-day appointments, or sort decimal totals as text. Negative controls must
fail the independent oracle while retaining successful execution and grounded
rendering. These are synthetic mechanism checks, not sampled model failures.

The 25 development scenarios contain 30 turns. Eight additional scenarios are
**candidate holdout labels**, awaiting independent human adjudication. They are
seeded from separate `candidate-fixtures.json` amounts, records and event data.
They are
not an independently authored, confidential or previously measured holdout.
Do not tune against a candidate's fresh outputs and continue calling it held
out: move it to development and replace/version the split. Existing v1 evidence
and the 68-case query-inference corpus are unchanged.

Coverage includes Thai exclusions, payment maximum versus frequency/grouped
sums, exact decimals, repeated payments/events, whole calendar frames, Buddhist
years, follow-up totals, topic changes, activity-date expense joins, empty
results, missing category mappings, provider failures, ambiguous requests,
Calendar previews, explicit confirmation, cancellation and event replay.
Generic table mutations, saved-record edits, enrollment, time-zone/all-day
Calendar edge cases and raw-history retention are not covered by this corpus;
they retain their existing regression suites.

## Four observations

| Dimension | Evidence | Limits |
|---|---|---|
| Intent outcome | Independent Python/Decimal oracle over fixture records and gold predicates; exact preview fields | Observable result equivalence on these data, not a proof of arbitrary SQL predicates or complete language understanding |
| Execution contract | Reported current result refs are successful and complete; expected operation status or unavailable read | An unrelated earlier successful query cannot satisfy this check |
| Structural grounding | Accepted native response document is rendered again from the actual offered facts, equals the result message, and equals the fake LINE delivery | Human correctness, wording and style review stays pending; trusted fallback/other paths are NA here and their realization mode is reported |
| Authorized effects | Before/after expense/catalog/Calendar snapshots, exact new Calendar fields, provider write counts; replay uses no model or business reads | Synthetic protocols; internal state/usage/receipts and LINE delivery are intentionally excluded from business-write counts |

No expected tool sequence, exact SQL string or column alias is required. Rows
are compared as multisets unless order was requested; duplicates and row
associations remain significant. Numeric strings use exact Decimal equivalence;
this corpus does not test numeric-looking item names or infer arbitrary units.
Model self-review is never an oracle. Free clarification prose receives a
contract check but its intent-outcome score is NA until human review. Missing
or inapplicable scores are not counted as failures or perfect answers.

## Offline use

Run from the public checkout with its existing Python environment:

```sh
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python scripts/evaluate_intents.py --validate
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python scripts/evaluate_intents.py --reference \
  --output ../local-data/intent-v2/reference-development.json
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest tests/test_intent_evaluation.py -p no:cacheprovider
```

`--reference` executes scripted native plans from `reference-plans.json`. It
calibrates the harness and oracle and is **not model accuracy**. The CLI has no
HTTP model client, credential discovery or live-provider mode. Results must be
written outside the public checkout. Exit codes are 0 for observed checks
passing, 1 for semantic/contract/effect failures or failed attempts, 2 for invalid
input/configuration. Human review remains pending even on exit 0. Reports retain
failed attempts, repetitions, scenarios, actual request hashes, received usage,
model settings and installed package versions. Control usage is synthetic;
received usage is not an invoice. Local fake-provider elapsed time is not
production/model latency.

`--replay path.json` consumes exact recorded responses, without adapting old
names or guessing stale references. The tape must bind `manifest_sha256` and
have `attempts["0:scenario-id"]` arrays of `request_sha256` plus a Responses
`response` envelope (zero-based repetition). Unused, missing or changed requests
fail. A replay is not fresh inference. Do not store private tapes in public
source, and do not use old v1 outputs as evidence for current v2 accuracy.

## Fresh evaluation through an authorized caller

`scripts/semantic_harness.py::run_case(case, fixtures, model_callback)` constructs
the current production factory, signed owner webhooks and wholly synthetic
Google/LINE/Firestore protocols, including external state records. It uses the
current configured model, reasoning setting, request budgets and timeouts.
`model_callback(body)` receives only the exact native runtime Responses body
and returns a Responses envelope. Gold labels, reference plans and scorer data
are never sent to it. All planner/auxiliary requests take this same callback;
there is no tool-name translation, extra critic or hidden retry. A callback
failure raises `AttemptFailure`; its `.partial` retains completed turns,
request hashes, settings and unknown usage. The CLI preserves this failed
attempt before continuing with the next independently scheduled scenario.
`--repetitions` always means
fresh scenario state, not regeneration until a pass.

This declared synthetic profile enables Calendar all-calendar reads, response
realization and state records, uses Sheets expenses and disables unrelated
optional workflows. It is not a claim to reproduce every production flag or
backend. A real model callback is separately authorized by its caller, which
must preserve all failed requests/unknown usage and keep credentials and raw
artifacts outside this checkout. No such callback is supplied or invoked by
this suite. Before fresh evaluation, independently review labels, declare the
profile/thresholds and freeze a new manifest; do not change labels after seeing
model answers. Human answer/style review and fresh-model repetitions remain
necessary before drawing model-quality conclusions.

`manifest.json` binds runtime source, tests/protocol fixtures, corpus, runner and
pinned dependency files. After an intentional source/corpus change, review it
and run `--write-manifest` **before** new evaluation; never refresh a manifest
to relabel an old failed run. Manifest validation also runs in ordinary CI tests.
Exact archive packaging is a separate release check.

The staged, task-specific checks and separation of automatic versus human
judgment follow [OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices).

Native Calendar results are normalized directly from their complete event count
and Bangkok start dates; they do not need a SQL projection with a `date` alias.
Timed offsets and date-only starts preserve duplicate occurrences. If a scorer
adapter bug is found after a run, retain the original scores and source binding,
then write an explicitly versioned offline rescore of the received observations
with unchanged gold values. That rescore is not another fresh-model attempt.
Strict extra-column/title/presentation differences still require independent
review of the labels; do not silently promote them to correct model answers.
