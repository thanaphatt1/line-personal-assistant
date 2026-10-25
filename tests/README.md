# Test guide

[SQL result units](test_relational_result_units.py) check counts with misleading
aliases, source units through projections/CTEs/windows/aggregates, ratios,
conflicting units, repeated analysis and exact decimal display. These contracts
cover generic structured records as well as expense data.

[Create-then-reschedule regressions](test_calendar_created_reschedule.py) use a
single synthetic Calendar through signed creation, cold restoration and ETag
updates. They cover a day-only follow-up after 28 minutes, intervening replies,
one-event batches, all-day events, repeated moves, unknown writes and foreign
or altered creation proofs at the exact read boundary.

[Owner record controls](test_owner_features.py) cover reviewed expense removal,
delivered buttons and confirmed vocabulary across cold signed webhooks.
[Provider/state boundaries](test_owner_feature_boundaries.py) exercise stable
IDs, tombstones, atomic Firestore receipts, uncertain writes, scope/version
validation, interrupted alias actions and paginated Unicode labels. These use
synthetic providers; the optional official emulator separately verifies deletion.

The [planner prompt contracts](test_planner_instructions.py) check guidance
selection from offered tools, preserved dialogue and parameter documentation.
The [planner semantic corpus](fixtures/planner_semantics.json) supplies synthetic
cross-domain scenarios for separate real-model development comparisons; it is
not executed as paid inference by pytest. See [prompt design](../docs/planning-prompts.md).

The [additional-payment regressions](test_additional_expenses.py) cover
recorded-fact reuse on another date, unrelated pending drafts, catalog item
case variants, creation-only repair, cold signed restore/confirmation/replay,
and safe planner failure diagnostics. Business providers are synthetic.

The [natural reply contracts](test_natural_replies.py) check that approximate
Calendar times are prepared and disclosed while alternatives at the date/time
still ask, that unrelated wording does not block preparation, that Calendar
reviews and receipts use readable dates through bound operation documents and
fallbacks, that evidence mismatches return to the planner, and that failure copy
states no effect without internal terms.

The [entry checks](test_entry_checks.py) cover same-day duplicate notices,
conditional `only_if_absent` preparation (present, absent and unreadable records),
the previous-question date and its immediacy limit, visible default dates and
the receipt's alias offer through the separate alias review.

The [name matching contracts](test_name_matching.py) check enum-only candidate
requests, strict parsing, fallback on low confidence/outage/invalid output, no
request for known names or a disabled flag, replay without a second request, the
signed production reservation gate, typed save confirmation and the false
success-claim regression.

The [calendar activity answers](test_calendar_activity_answers.py) check that a
long activity question is one read with one decision per repeated title, that a
zero-confirmed answer leads with grouped possible matches and one per-title
question, that answering for a title settles every occurrence without a write,
the multi-year window bound, month-split reads answered as one question, and
that a paraphrased filter returns to the planner instead of ending the turn, and
that a read stops before the turn's provider-call budget so the turn still saves
and replies (`read_capacity`) instead of failing the webhook.
The [answer units and periods](test_answer_units_and_periods.py) check the shared
amount format, Latin-name spacing, named months/years, day counting (distinct
dates, not appointments) in reads, confirmations and subtotals, the asked period
on confirmed-date subtotals and the single polite zero-confirmed lead.

The [read planning contracts](test_read_planning_contracts.py) cover server-computed
relative days, next appointments without period words, a misquoted period returned
to the planner, the typed single-label filter and balance guidance, SQL fallback
amounts and Thai table-review dates.

The [activity memory contracts](test_activity_memory.py) cover remembering a
whole-title answer with a one-tap undo, applying and disclosing labels on the
next read, skipping the classifier when every title is known, forgetting,
owner-wording grounding, date-subset answers, the disabled flag, the optional
snapshot key and its tamper check, the configuration dependency, labels outside
the restore bounds, no memory after a failed expense read, disclosed hints for
other activity wording, expense totals and count summaries that disclose the
label, and `purpose=expense_dates` keeping the spending goal.

The [conversation acceptance scenarios](test_experience_scenarios.py) exercise
missing-field questions, interruption, compound rename/category corrections,
short continuations and one final save. [Batch reads](test_read_batch.py) compare
identical computed answers against sequential planner/provider call counts.
[Performance](test_performance.py) and [LINE loading](test_loading.py) check
aggregate-only reporting, replay separation, signed owner scopes and failure
isolation. [Filtered table reads](test_filtered_table_storage.py) check indexed
scope completeness, decimal/null residual semantics, cursor failures and fresh
requests; the official emulator also exercises a multi-page date-range query.
These are scripted synthetic contracts; see the
[performance guide](../docs/performance.md) for measurement boundaries.

The [read-answer composer](test_answer_composition.py) checks flexible prose,
shared date headings, complete linked cells, preserved ranking, invalid-document
fallback and exact signed replay. Its model responses are scripted; fresh
synthetic model evaluations are retained separately outside this checkout.

The [SQL executor](test_relational_sql.py) and
[relational workflow](test_relational_queries.py) suites cover joins, CTEs,
CASE/window calculations, exact decimal helpers, complete inputs beyond display
limits, schema-based repair and read isolation. They include people/income and
unrelated nonfinancial schemas, not only expense records. These are scripted
offline contracts, separate from any fresh model evaluation.

Shared [pending patches](test_pending_records.py),
[temporal plans](test_temporal_plans.py) and [adaptive recovery](test_tool_recovery.py)
check current schema/evidence, retained values, restart and no-progress behavior.

Metadata table suites use synthetic people, income and shopping schemas:
[engine](test_tables.py), [dialogue](test_table_workflow.py),
[REST adapters](test_table_storage.py), and [signed composition](test_table_factory.py).
They cover nonfinancial records, complete-input calculations, generic CRUD
reviews, restart/replay and positive-proof recovery. The
[registration fixture](fixtures/table_registry.json) contains no real targets.

The default pytest suite checks the assistant's code with synthetic data and
scripted provider/model responses. It needs no LINE, Google or OpenAI credentials.
Passing it shows that the tested contracts hold; it does not measure general
language-model accuracy or prove a live owner workflow.
Use the [database and field guide](../docs/database-schema.md) when constructing
synthetic Sheet rows, persisted state or source/transaction references.

The [activity-spending follow-up regression](test_activity_expense_followup.py)
uses a [synthetic two-turn scenario](fixtures/activity_expense_followup.json): an
uncertain exam-duty Calendar result followed by `มีวันที่ 14`. It checks both
speakers and the verified month after a factory restart, reproduces the
`date_required` repair, blocks an unconditional previous-month total, rejects
stale Calendar refs and calculates the confirmed-date subtotal. Short selections
use the delivered `pending_query` context and `resolve_activity_dates` tool;
remaining candidates survive restart and can be confirmed later. Cases also
cover changed/deleted events, access failures, expired/undelivered context,
unrelated new queries, same-day deduplication and exclusive midnight ends. The
former expected failures now pass. The malformed model calls are constructed
reproductions, not captured production decisions; the scripted controls do not
establish that the model understands the dialogue.
Run with `pytest tests/test_activity_expense_followup.py -p no:cacheprovider`.

## Run from the repository root

Use Python 3.11+ and a virtual environment. Dependency installation needs access
to the package index; the test and package checks afterward are offline.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-ci.txt
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -p no:cacheprovider
python scripts/check_ci.py
```

pytest reads the source path from [pyproject.toml](../pyproject.toml), so an
editable installation is not required for these commands. The last command
verifies a deterministic source archive, builds/installs a local wheel without
dependency downloads and checks isolated mock and signed startup paths. It does
not build a cloud container. These are also the checks in [CI](../.github/workflows/ci.yml).

For a focused change, select a suite or an exact test:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_historical_categories_adversarial.py -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_calendar_all_day_v15.py::test_lost_all_day_ack_reconciles_positive_receipt_without_second_post -p no:cacheprovider
```

Inspect what pytest will run without executing test bodies:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests --collect-only -q -p no:cacheprovider
```

Do not supply production credentials or replace fixtures with real owner records.
[conftest.py](conftest.py) blocks external socket connections. Tests inject
`httpx.MockTransport`, fake owners, clocks, messages and provider results. Worker
subprocesses use synthetic adapters and temporary storage as well.

## What is tested

The following count is a **dated snapshot**, not an expected count for every
future revision: on **2026-10-18**, source
[`4ef9481`](https://github.com/thanaphatt1/line-personal-assistant/tree/4ef9481c314229726ae2d950b03d53548b311c60/tests)
passed **3,150 collected test items** in [CI](https://github.com/thanaphatt1/line-personal-assistant/actions/runs/39090878702).
The table groups each item once by its primary test file; a workflow can exercise
several areas. Use collection output for the checkout's actual count.

The later previous-amount feature adds **56 signed offline cases** in
[test_previous_expense_prices.py](test_previous_expense_prices.py). They check
saved-price provenance, dates independent of the source payment, explicit amount
precedence, stale/unknown outcomes and mixed workflows. Its
[synthetic model corpus](fixtures/previous_expense_prices_development.json)
is development-only and is not executed by the default offline suite. The dated
3,150-item inventory below remains the earlier source snapshot.

| Family | Snapshot items | Representative behavior and suites |
| --- | ---: | --- |
| Expenses, drafts and catalog | 734 | Keep separate price occurrences, reject invented amounts, clarify missing facts and invalidate old consent after an edit. [Financial evidence](test_financial_guard_v13.py), [shared expenses](test_shared_expense_evidence_v10.py), [draft changes](test_draft_patch.py), [catalog names](test_catalog_names_v09.py) |
| Conversation, history and tools | 663 | Follow up on owned messages, preserve pending drafts after restart and reject foreign or fabricated references. [History workflow](test_history_first.py), [reference contracts](test_history_reference_contract.py), [raw history](test_raw_history.py), [tool loop](test_domain_tool_loop.py) |
| Calendar | 628 | Use date-only all-day payloads with exclusive ends, handle month/leap-year boundaries, correct one batch member and reconcile a lost acknowledgement without creating it again. [All-day events](test_calendar_all_day_v15.py), [batches](test_calendar_batches_v13.py), [text confirmation](test_calendar_text_confirmation_v13.py) |
| Ingress, auth, configuration and startup | 353 | Reject invalid signatures and nonowners, constrain Google requests and stop when required configuration is missing. [Webhook](test_webhook.py), [Google requests](test_google_requests.py), [runtime configuration](test_runtime_config.py), [signed startup](test_signed_startup.py) |
| Language, model contract and replies | 285 | Validate scripted tool arguments, prevent invented amounts/dates/units in wording and use a factual fallback on failure. [Model contract](test_model_api.py), [language cases](test_language_fixes.py), [reply checks](test_reply_check.py), [reply realization](test_response_realization.py) |
| Recovery, reliability and synthetic integration | 172 | Kill/restart a local process at effect boundaries, redeliver the same event and keep uncertain effects closed until positive reconciliation. [Durable recovery](test_durable_recovery.py), [lease model](test_firestore_leases.py), [synthetic integration](test_synthetic_integration.py) |
| Historical and provisional categories | 170 | Review ten names at a time, preserve a partial selection, exclude ambiguous names and separate one-time summary consent from permanent mapping. [Historical workflow](test_historical_categories.py), [adversarial cases](test_historical_categories_adversarial.py), [provisional categories](test_provisional_categories.py) |
| Reads and saved-record changes | 145 | Calculate bounded totals, preserve query filters, reject model-invented facts and select an existing expense/event before showing a change review. [Reads](test_read_queries.py), [query grounding](test_query_grounding.py), [saved-record workflow](test_saved_record_workflow.py), [provider contracts](test_saved_record_providers.py) |
| **Total** | **3,150** | |

A test item is not a separate feature or a real API call. `pytest.mark.parametrize`
expands combinations such as feature ON/OFF, stale/current timestamps, lost
acknowledgement phases and malformed values. For example, an all-day date test
can cover the end of a month, year and leap February as separate items. Counts
are neither independent user conversations nor a measure of coverage percentage.

## Test layers and organization

- **Unit and contract tests** check dates, Decimal amounts, schemas, reference
  ownership, configuration and exact request/response formats. See
  [Sheet dates](test_sheet_dates.py) and [history tool schemas](test_history_tool_schema.py).
- **Workflow integration tests** send events through the assistant or a FastAPI
  test client, often with a real HMAC signature. The app runs actual validation,
  state and consent code, while LINE, Google and model responses are simulated.
  They check both results and effects: no write on preview, exactly one write
  after valid consent and no second write on replay.
- **Crash/restart tests** use temporary SQLite stores and separate local Python
  processes. They exercise real process exits with synthetic providers. A local
  concurrency or modeled Firestore-lease test does not prove every live
  Cloud Run/Firestore or outside-writer race is controlled.
- **Packaging/startup tests** verify allowed source inputs, checksums, tampering,
  isolated imports and signed startup with zero provider calls. See
  [source packaging](test_build_source.py), [packaged history startup](test_history_packaged_startup.py)
  and [the CI checker](../scripts/check_ci.py).

Many files span more than one layer, so there is no unit-versus-integration
percentage implied by the family table.

Test files are `test_*.py`. Shared [behavior](behavior_support.py) and
[language](language_support.py) helpers construct synthetic evidence and tool
responses. [Persistent API mocks](persistent_api_mock.py) and worker scripts
support restart tests; run them through their pytest callers. [`fixtures/`](fixtures/)
holds synthetic corpora, including examples intended for separate inference
validation. A corpus JSON file does not call a model by itself.

Version suffixes in filenames preserve the history of regression stages and
compatibility contracts. Older entry points and test-only adapters can have cases
for behavior that the current owner-facing runtime does not expose. Use
[supported behavior](../docs/behavior.md), not a helper name, as the feature list.

## Offline, replay and fresh model evidence

| Evidence | What runs | What it establishes |
| --- | --- | --- |
| Default offline suite | Real application code with synthetic inputs and scripted model/provider responses; external sockets denied | The asserted validation, consent, state, API and recovery contracts for those cases |
| Replay | Application code consumes previously recorded model decisions; no new inference | Compatibility with those recorded decisions; it is not a fresh model result |
| Fresh synthetic inference | A separately authorized runner calls the real selected model on an exact runtime archive/corpus, with Google/LINE/state mocked | How that model behaved on those synthetic turns and repetitions; not live business delivery or general accuracy |
| Deployment checks | Separately approved live health/signature, Ready/config/digest and targeted operational checks | The inspected service/release conditions; not a real expense, mapping or Calendar write |

Keep these denominators separate. In the evidence reported with the snapshot:

- The **70 Astra adversarial cases are included in 3,150**. They cover stale
  controls, competing expense/Calendar confirmations, partial selection,
  oversized Unicode reviews, redisplay/delivery and exact page references/counts.
- The **42 release-operator tests** quoted during preparation were separate
  from the public suite. Three later operator-only binding regressions made that
  check **45**; they did not change the 3,150 public items. Release executors and
  their private settings are not checked into this repository.
- The **13 cross-process OFF-bridge cases** were a separate synthetic recovery
  proof, not 13 more public test items or an actual traffic rollback.
- The **29/29 fresh Luna turns in five synthetic conversations used 30 received
  calls**, with one repetition and no decision replay. Turns and API calls differ
  because a turn can need two model phases or a deterministic confirmation can
  use none. These are separate from the offline count. The public
  [historical feature guide](../docs/historical-categories.md#adversarial-review-regressions)
  identifies that exact runtime archive and its synthetic corpora.

Private responses, approvals, usage records, deployment settings and live logs
stay outside this repository. There is no automatic paid-inference or deployment
command in CI. Follow the [inference validation protocol](../docs/inference-validation.md)
for a separately authorized run. Development cases must not be presented as
unseen holdout results; the [benchmark protocol](../benchmarks/PROTOCOL.md) and
[frozen scorecard](../benchmarks/v1/results/README.md) describe that distinction.

Passing these checks does not establish general Thai understanding, every real
LINE delivery outcome, a live Sheets/Calendar business write, actual traffic
rollback or atomic protection against outside Sheet writers. The latter remains
an operating constraint: the app lease does not provide Sheets compare-and-swap
or lock external editors. See [saved-record limits](../docs/saved-record-edits.md)
and [deployment/recovery](../docs/deployment.md).

## Add a regression

1. Choose the suite closest to the observable behavior. Use a synthetic owner,
   workbook, event and clock; keep private records and credentials out of fixtures.
2. Reproduce the failure with a small case before changing code. Use malformed
   or wrong scripted model output when checking that the server rejects it;
   a correctly scripted answer is not evidence that Luna understood the request.
3. Assert the user-visible result **and** relevant effects/state. For example,
   edit a reviewed amount, submit the old confirmation and assert zero writes;
   deliver the latest review, confirm it and assert one effect despite replay.
4. Add boundary variants that exercise the same rule: stale/foreign references,
   failed delivery, feature OFF or an uncertain acknowledgement. Parameterize
   meaningful variants rather than copying an implementation into assertions.
5. Run the focused file/test, then the appropriate related regressions. Before
   publishing a runtime change, run the full offline suite and `scripts/check_ci.py`
   and review persistence/bridge compatibility if state contracts changed.

See [development](../docs/development.md) for source packaging and change boundaries.
A source push and passing tests do not authorize paid model calls or deployment.
