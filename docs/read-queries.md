# Query data with declared sources and SQL

This is a source-contract reference. For a short introduction, start with
[read intents](intents.md); use [SQL analysis](relational-queries.md) for query
semantics and precision.

On this page:

- [Current read interface](#current-read-interface)
- [Current interface and compatibility](#current-interface-and-compatibility)
- [Expense and Calendar source reads](#expense-and-calendar-source-reads)
- [Activity dates and spending](#activity-dates-and-spending)
- [Calendar completeness and semantic analysis](#calendar-completeness-and-semantic-analysis)
- [Read-only continuation and finishing](#read-only-continuation-and-finishing)
- [Follow-up activity selections](#follow-up-activity-selections)
- [Reply composition and fallbacks](#reply-composition-and-fallbacks)
- [Tests and evidence](#tests-and-evidence)

## Current read interface

The planner submits `act` with `intent=read` for source reads and [read-only SQL](relational-queries.md).
Its `sources` array declares expense, Calendar or configured table sources with
unique SQL aliases and source-specific parameters. An optional `result` source
selects an actual current-request result. When `sql` is supplied, the server
fetches the snapshots, materializes their aliases and executes the query in the
same call. The model need not obtain a reference between fetching and computing.
Source schemas and catalog choices are available before the initial query.

For example, an expense source aliased `e` can supply
`SELECT SUM(amount) AS total FROM e WHERE canonical_item='Rail'`. The source
parameters select the owner-cited date scope; selection/grouping/ranking belong
to SQL. The server retains complete rows independently of display limits.
Multiple sources can be joined in the same call, including unrelated configured
table schemas. Google Sheets and Firestore remain the business stores; SQLite
is a temporary analysis engine.

Use `sql=null` to inspect source values before planning when necessary, then query the
returned result or redeclare the same source. Source snapshots are retained only within
this read-only owner turn; SQL repair reuses them without repeating provider reads.
Failed reads remain failures, never empty snapshots. Every source descriptor is
validated before any read. Incomplete sources block SQL and preserve unresolved Calendar
choices. No query can prepare or execute a business write. An offered `intent=respond`
checks the actual result and composes the answer in the next planner call, using server
fact/cell placeholders. `finish_query_analysis` remains available for other result
combinations and unresolved conditions. See the [intent contract](intents.md).

## Current interface and compatibility

| Interface | Role in the current history-enabled runtime |
| --- | --- |
| `act` with `intent=read` | Planner entry point for declared sources and optional SQL |
| `act` with `intent=inspect` | Inspect configured capabilities and observed coverage |
| `act` with `intent=respond` | Select an offered complete result and compose a linked answer when enabled |
| `finish_query_analysis` | Still offered for other result combinations and clarification |
| `query`, `query_sources`, `query_relations`, `query_expenses`, `query_calendar` | Internal or compatibility handlers; not additional read actions offered to the current planner |

Typed table selection for saved-change workflows retains its existing
identity/version contract. Below, names such as `query_expenses` identify reader
parameters, diagnostics and compatibility behavior. New read plans use `act`;
they do not need to reproduce the internal handler sequence.

### Filtering in current read plans

For filtering, including one named item, category or subcategory, declare the
complete expense period and put the selection in SQL predicates. Internally,
the executor normalizes expense inputs to `filter=null`, `view=list` and
`continue_analysis=true`; the planner does not set continuation flags on `act`.
Resolve names using catalog meanings and categorical choices in source
metadata. The retained typed filter is positive equality for compatible direct callers.
A total ignores sort flags instead of refusing the read; other view/sort/group conflicts
return to the planner (`expense_view_conflict`). Exclusion, boolean combinations and
nested conditions use the same SQL engine as other queries. The legacy expense source
filter implements positive equality only; it must never stand in for “ไม่รวม” or
“ยกเว้น”. A delivered SQL analysis keeps its source period and logical plan as context
for follow-ups, while each new turn still reads fresh records.

Examples include daily expense totals with cumulative spending,
income joined to people, or shopping records filtered through another table.
These use registered schemas rather than a function for each table/question.
See [metadata-defined tables](tables.md) for setup and operations.

## Expense and Calendar source reads

The planner also receives its actual available tool names and configured source scope. It can inspect verified expense years/counts/date bounds or Calendar discovery/queried-window metadata before planning. Unknown Calendar global extent is never inferred from a bounded window. See [capability and data-scope awareness](data-inventory.md).

The signed, owner-only history application can answer stored-data questions using the existing configured workbook and readable calendars selected by the server. These queries use the current read permissions; no new scopes or credentials are needed. They do not append expenses, create events, change a draft or create a confirmation nonce. Conversation receipts and the existing usage ledger continue normally.

The model selects sources through `act` with `intent=read` without a semantic keyword pre-router. It sees recent dialogue, the authoritative Bangkok timestamp, actual schemas and the latest verified delivered query scope. Code validates arguments and performs configured-source reads. Internally, complete raw results become SQL relations; expense inputs use `filter=null`, `view=list` and `continue_analysis=true`. A source push does not deploy this change.

### Source examples

 - `สรุปรายจ่ายเดือนนี้` — total saved expenses for the current Bangkok month. -
`ค่าอาหารวันนี้เท่าไหร่` or `สรุปรายจ่ายเดือนนี้หมวดอาหาร` — resolve an existing catalog
category; ask if the label is unknown or ambiguous. - `ดูรายการรายจ่ายตั้งแต่วันที่
2026-11-01 ถึงวันที่ 2026-11-07` — total and saved entries for an inclusive range. -
`เฉพาะหมวดย่อยเครื่องดื่ม`, `แยกรายการให้หน่อย`, `แล้วเดือนก่อนล่ะ` — continue the
latest delivered query when that catalog label exists. - `ดูนัดพรุ่งนี้`,
`สัปดาห์หน้ามีนัดอะไรบ้าง`, `ดูนัดถัดไป` — list saved timed/all-day occurrences across
the server-selected readable calendars. - `เดือน พ.ย. มีนัด TA กี่ครั้ง` — read the
whole November frame, then semantically classify appointments and count the matching
event references. - `ใช้ไรเยอะสุด` — follow the verified period, sum by saved item name
and rank group totals highest first. `รายการไหนแพงสุด` ranks individual payments
instead. - `เรียงยอดมากไปน้อย` — sort the complete matching input numerically before
selecting displayed rows. - `เดือนที่แล้วที่ไปคุมสอบผมใช้ตังไปเท่าไหร่` — find matching
Calendar appointments, derive their Bangkok dates, then sum all expenses on those dates.
- `แล้วเมื่อวานล่ะ` — continue the latest verified delivered query. Clarification/chat
replies do not erase that read scope; failed delivery, a different workbook or an
expired receipt cannot supply it. Write consent still requires the latest delivered
review.

### Filters and saved item identity

A new explicit expense question starts with all items unless it supplies a filter. Date-only follow-ups retain the preceding filter; `ทั้งหมด` clears it. Calendar subjects/types/conditions are copied literally from cited owner wording into `filter_text`; a paraphrased filter (`filter_text_not_quoted`) is returned once to the planner as an assistant-argument repair, never read, and a repeated mistake stops with a clarification. The second model request classifies the complete fetched event set semantically; there is no keyword filter, Calendar `q` search, or model-generated provider query.

The model resolves wording and typos to exact existing item/category/subcategory labels
supplied by the catalog; an unknown or ambiguous label requires clarification. No
competing semantic parser chooses filters. Category totals use the current Database
mapping, not a reconstructed historical category. Stored item identity shares the write
path's exact/NFC/case/outer-whitespace, explicit alias and unique internal-spacing
rules. Exact identities take priority over spacing collisions. Fuzzy typo suggestions
never classify a stored expense, and line breaks are not joined to manufacture an
identity. These comparisons do not rename or edit any Sheet cell.

### Missing category mappings

When a complete input read contains an item whose category cannot be established, the
tool returns `mapping_incomplete`, `complete=false`, and `mapping_diagnostics`.
Diagnostics identify the unresolved stored names, occurrence counts, excluded amounts,
missing/ambiguous identity or blank-category reason, and bounded candidate identities
requiring review. They never infer a category from the wording. A separate `partial`
object contains only the subtotal/count of safely identified and mapped rows; there is
no top-level complete `expense` total. The reply explicitly labels it **ยอดบางส่วน**,
names the items that need attention, states that those records may affect the requested
category, and asks the owner to check the expense names and existing Database mappings.
No mapping is changed automatically. A zero known subtotal is still partial and cannot
mean no category expenses.

The structured result includes at most20 unresolved names; the reply shows at most10
names and discloses additional names. Counts and excluded amounts use all unresolved
rows from the complete bounded read. A partial response does not claim a complete
category listing. Optional blank subcategories remain valid catalog data; an unassigned
category is actionable when asking for a category total. Schema/provider/incomplete-read
failures cannot produce a partial subtotal, because the underlying financial input is
not verified complete. Unfiltered expense totals do not require category mapping. Exact
item-filter totals use only verified item identity, including unique spacing, and do not
fuzzy-merge unfamiliar names.

### Dates, periods and source windows

Dates use Asia/Bangkok. Weeks run Monday–Sunday; relative dates are anchored to the current owner message timestamp. The model interprets Thai month names and abbreviations, Buddhist years, ISO dates and day/month/year dates into typed calendar frames or explicit inclusive dates.

Relative day/week/month/year offsets (`calendar_day` for วันนี้/เมื่อวาน/พรุ่งนี้) are selected by the model and expanded using calendar arithmetic on the authoritative Bangkok date. New periods carry a literal owner `period_text` citation; the current clock alone cannot fill an omitted period. A cited span that is not a literal quote (`period_text_not_quoted`) returns to the planner as an assistant-argument repair instead of asking the owner for a period they already gave. The `upcoming` mode needs no period words.

For filter/view-only follow-ups, `date_mode=previous` explicitly selects dates from the latest verified query receipt; it cannot invent a default when that receipt is absent. An unqualified month uses its first through last day; an explicit “จนถึงวันนี้” clips the end to today. Missing or conflicting periods require clarification.

The planner chooses the whole period the owner asks about, across years if needed: Expense reads cover up to 10 years. One Calendar read covers up to 3 years, paged 50 events at a time (at most 1,000 per calendar); a larger result is reported incomplete rather than partially counted. The model-selected `upcoming` mode for “นัดถัดไป” still covers the current time through the next six dates and includes events still in progress.

### Exact totals and input failures

Expense totals use Decimal arithmetic over a complete bounded input read. Sheets identity, headers and configured grid bounds must pass. Malformed values, incomplete mappings, access failures or an oversized grid yield an unavailable response rather than a zero balance. The legacy expense ledger has no income mapping. Income/balance questions require explicitly configured income data and the necessary sources; an absent source is not a known zero.

### Compatibility: typed expense grouping

`query_expenses` composes exact filters with `group_by` (`none`, `item`, `date`,
`category`, `subcategory`), `order_by` (`date`, `amount`, `count`, `label`),
`descending` and a displayed `limit` of1–20. Group amounts use Decimal sums and group
counts include every matching payment; sorting happens before display truncation.
Ranking requires `view=list`. `count` requires groups; date ordering requires individual
rows or date groups. Invalid combinations fail explicitly. Category/subcategory grouping
uses verified current catalog mappings; unresolved/blank mappings make the ranking
unavailable without a guessed zero or silently omitted group. This grouping failure does
not create a provisional category proposal. Item/date grouping does not require catalog
mappings. The result discloses grouping/order and full-input totals separately from
displayed top rows. Grounded row facts cannot be reordered by the wording model.

## Activity dates and spending

`count_unit=days` answers how many days (distinct Bangkok dates inside the window, exclusive ends) and also states the appointment count when it differs; other questions count appointments. A follow-up confirmation and `calendar_count` keep that unit.

In the compatibility reader sequence for activity-conditioned spending, first call `query_calendar` with the literal activity in `filter_text`, `purpose=expense_dates` and `continue_analysis=true`, then call `query_expenses` with its current-request `calendar_query_refs`. `date_mode=previous` can derive the interval directly from these Calendar reads.

Code requires complete Calendar coverage of the expense interval, complete classifications and occurrence references. It unions matching Bangkok dates, includes each payment once even with several same-day events, clips spanning events to verified windows and treats event end times/dates as exclusive. All-day/timed events ending at midnight do not add the next day. Unknown, duplicate, stale, incomplete, uncertain or insufficient-coverage refs fail without an unconditional total or false zero. A complete empty Calendar result can establish zero matching dates. This join can compose with exact expense filters, grouping and ranking.

It reads all spending on matching dates; it does not prove that each expense was caused by that appointment. Replies disclose that distinction, the selected dates, criteria and event count, and any remembered activity label behind those dates. A confirmed-dates subtotal still names the period the owner asked about, not the span of the confirmed dates.

`purpose=expense_dates` keeps a pending activity question tied to the spending goal even when the planner stops after an uncertain Calendar read, so a later confirmation answers the subtotal rather than the appointments. Aggregate `finish_query_analysis` operations keep each read's remembered-label lines.

### Fresh references on later turns

Calendar refs expire with the request. A follow-up retains the verified period,
requested presentation and activity condition as context, but must re-read Calendar
before applying that condition on a new turn. Reusing `previous` without fresh Calendar
refs cannot silently broaden the answer to every date. Planner instructions and
`query_capabilities` describe supported operations, distinguish activity conditions from
expense category filters, require executing supported requests instead of promising a
future sort, and require honest statements for unsupported operations. An output-claim
guard also rejects recognized narration of query execution when no read ran; it is not a
universal language classifier or proof of planner accuracy.

## Calendar completeness and semantic analysis

### Pagination, coverage and display

Calendar reads include transparent events and omit cancelled events. Timed events display Bangkok times; all-day exclusive end dates display the correct inclusive last day, including externally saved multi-day events. Pagination is limited to twenty pages of50 events per calendar, within the existing overall RPC/deadline bounds; an unfinished or inconsistent result is reported unavailable.

Paging stops while 20 provider calls and 15 seconds of the owner turn remain, so classification, state and the reply can still finish; that read answers `read_capacity` (ask for a shorter period) rather than ending the webhook with a retryable failure. A spent turn budget during any read is reported the same way, not as malformed data.

A shared invitation is counted once across calendars, while recurring occurrences remain separate. Successful replies show at most20 entries/events and disclose when more exist. Counts use all verified matches before display truncation. Long labels are shortened for display. A complete empty read can report zero saved records.

### Semantic membership and uncertainty

Semantic Calendar analysis uses the existing response-realization opt-in and second-call reservation. It groups occurrences with identical title/details into one reference (repeated appointments get one decision) and sends at most120 groups with original titles, times, up to1000 description characters and500 location characters; truncated details are explicitly flagged. With activity memory, at most20 owner-stated labels for the same titles under other activity wording are added as `owner_labels`. Attendees and other provider fields are not sent.

Its strict output must label every supplied event reference `match`, `exclude`, or `uncertain` and nothing else. Code validates membership and calculates counts, then renders the reply with the same wording as other Calendar answers. It does not admit an invented event or model-supplied number.

Uncertain events are listed separately (up to twenty), `complete=false` prevents a complete-total claim, and only confirmed matches contribute to the displayed count. Ambiguous abbreviations must remain uncertain without supporting context. Provider text and conversation are untrusted data; they cannot grant tool or write authority. This grounding does not prove that the model's semantic classifications are correct.

The schema uses [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses); refusal, incomplete-output and application-level membership checks remain mandatory.

### Inference limits and failure behavior

The semantic request is capped at48,000 encoded bytes,2048 output tokens and an eight-second foreground deadline, further bounded by the owner request's remaining time. Oversized candidate sets/context, missing read access, unfinished pagination, unavailable inference, exhausted budget, refusals and malformed outputs return an honest unavailable/clarification response with no guessed zero or keyword fallback. A repeated signed webhook reuses its persisted reply without new reads or model requests. Ordinary unfiltered queries retain their existing fact-slot realization.

## Read-only continuation and finishing

For multi-part questions, `continue_analysis=true` retains the complete read as a
current-request result reference and lets the model continue. There is no fixed
model-call count for a raw-history owner turn. Each subsequent request includes every
preceding call/result pair, while actual workbook/calendar identifiers stay out of tool
feedback. In this phase only saved-data read tools and `finish_query_analysis` are
available; draft preparation, edits and confirmation tools are absent. The model can
read Calendar, infer matching appointments, then query expenses for those dates.
Individual query windows and inference batches remain bounded, so larger questions can
be decomposed into multiple reads rather than enlarging a single model context.

`finish_query_analysis` accepts current-request references and an operation: `report`,
`expense_sum`, `calendar_count`, `compare`, or `clarify`. Code performs the arithmetic
for aggregates; `clarify` asks about an unresolved condition without reporting
supporting reads as a completed answer. Expense addition rejects overlapping inclusive
date intervals; Calendar union uses stable occurrence references across overlapping
reads and calendars. Comparisons require complete reads of the same domain. Incomplete
reads or uncertain Calendar membership cannot produce an aggregate total. A report
preserves each original incomplete-result qualifier. Duplicate, invented or
previous-turn references are rejected. Final replies disclose the actual source periods;
combining a few windows does not establish coverage of a larger unqueried period. Plain
model prose cannot replace these grounded facts.

## Follow-up activity selections

### Delivered selection context

An uncertain activity read also attaches a bounded clarification to its existing
receipt: the original question and period, activity, confirmed occurrences and up to
twenty displayed candidates. Possible matches are grouped by title; a zero confirmed
count leads with them, and one question per title (with reply buttons that only send
owner text) can settle every occurrence of that title. `pending_query` exposes this
context on later turns after verified delivery, within the same owner, expense dataset,
selected Calendar configuration and 24-hour history window. No separate pending-write
workflow, collection or migration is used. A completed new read or a business review
replaces this read focus; plain chat and failed reads can preserve it.

### Confirming or excluding candidates

For a spending follow-up such as `มีวันที่ 14`, the planner selects candidate references
with `resolve_activity_dates`, citing a literal selection from the current owner
message. The model interprets the language; the server checks reference ownership and
delivery, re-reads Calendar, rejects changed/deleted occurrences and calculates the
expense subtotal on the confirmed Bangkok dates. The selected event must also agree with
any unambiguous explicit date in that literal evidence. `decision=exclude` records a
rejected candidate without confirming another candidate or creating an expense total.
For example, `วันที่ 14 ไม่ใช่คุมสอบ` excludes day 14 and leaves day 29 unconfirmed; it
cannot authorize a day-29 subtotal. Exclusions survive restart alongside the remaining
candidates, and do not contribute to later confirmed-date sums. The original month
supplies the month/year for the displayed candidate, even when the current clock is in
another month. Known matches and subsequently confirmed candidates are unioned; two
events on one date count expenses once. Unconfirmed appointments remain visible for
later confirmation. The reply calls the amount a confirmed-date subtotal and discloses
any unresolved candidates; it never presents those dates as complete coverage of the
original activity or as proof of costs caused by the event.

### Remembered activity labels

With activity memory enabled, a whole-title answer is also remembered for that activity
(see [behavior](behavior.md)). A later read settles remembered titles before
classification, sends only the remaining groups and states each label it applied (`นับ
DSDE Exam เป็นคุมสอบตามที่เคยยืนยันไว้`). Labels stated under other wording for the same
titles (`คุมสอบ` when asked about `งานคุมสอบ`) are only `owner_labels` hints to the
classifier; an answer discloses a hint only where the classifier's label agrees with it
(`นับ DSDE Exam โดยอิงที่เคยยืนยันว่าเป็นคุมสอบ`). Activity answers list matches grouped
by title with Thai weekday dates, the same layout as possible matches and confirmations.

### Selection repair and stopping

While such a clarification is pending, an unconditional expense read with
`date_mode=previous` cannot silently broaden the selection to the entire month. That
error, missing tool dates and stale request-local Calendar refs return the pending
context to the ordinary read-only model loop for repair. Repeated calls/reads, deadlines
and accounting gates still stop the loop. If the planner cannot resolve the selection,
the fallback lists the actual candidates and asks which appointment, preserving the
known period. An unrelated new question can use its own explicit period normally.
Offline tests exercise these contracts with scripted choices; real model interpretation
remains a separate evaluation.

The loop ends on a final answer/tool result, failure, repeated identical read or invalid duplicate call ID. Removing the turn-call ceiling does not remove provider/context/output limits, the existing owner RPC/deadline scope, configured daily/monthly accounting gates or unknown-attempt/replay protections. Completed proposal caches for every step are cleared together with the production receipt; fingerprints, reservations, usage and unknown attempts remain. Account settings and production activation are separate from a source change.

## Reply composition and fallbacks

The current linked-document path is described in [answer composition](answer-composition.md).
It validates fact/cell references, coverage and literal-data boundaries; it does
not use a vocabulary whitelist or prove arbitrary prose semantically correct.
Native `act` responses use a verified fallback directly after a rejected document,
without another model call.

The retained ordinary fact-slot reply path composes natural Thai by selecting phrases around separate immutable period, filter, total and count slots. Verified row details are appended by the server; the model need not repeat or enumerate them. Partial-result qualifiers and resolution instructions are also required immutable facts, and unresolved names are appended as locked rows. Amounts/counts include their units, preventing unit swaps; required schema fields retain every header fact exactly once, and per-slot phrase enums cannot invent dates, quantities, names, success or change units. A model failure returns a natural server fallback.

Distinct non-sensitive reason codes distinguish invalid arguments, unknown filters, incomplete mappings, schema errors, provider failures and unfinished reads. They do not establish the cause of any earlier live incident. A terse dated title such as `พรุ่งนี้ Workshop` asks whether to add an appointment or view saved events, and creates no draft. This also applies after a previous query.

## Tests and evidence

Run the offline regression suite and `python scripts/check_ci.py` as described in
[development](development.md). Offline tests inject explicit model tool choices; they do
not demonstrate real language understanding. The synthetic inference corpus in
`tests/fixtures/query_inference.json` defines paraphrase, follow-up, fault, injection
and write-regression scenarios for a separately authorized paid runner; private
credentials, responses, cost receipts and approvals stay outside the checkout. Provider
tests cover inclusive boundaries, midnight/leap-year dates, totals/listing, filters,
all-day events, pagination, incomplete reads, cold restore/replay and confirmation
isolation. A source push or passing CI does not deploy this feature.
