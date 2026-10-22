# Query data with SQL and source read tools

The primary composable analysis layer is [read-only SQL](relational-queries.md)
over observed table, expense and Calendar records. The planner selects sources
from metadata, reads complete matched rows, then uses `query_relations` for
joins, conditional/nested calculations and window functions. Google Sheets and
Firestore remain the business stores; SQLite is a temporary analysis engine.
Simple typed reads and legacy combination tools remain available.

Examples include daily expense totals with cumulative spending,
income joined to people, or shopping records filtered through another table.
These use registered schemas rather than a function for each table/question.
See [metadata-defined tables](tables.md) for setup and operations.

## Expense and Calendar source reads

The planner also receives its actual available tool names and configured source scope. It can inspect verified expense years/counts/date bounds or Calendar discovery/queried-window metadata before planning. Unknown Calendar global extent is never inferred from a bounded window. See [capability and data-scope awareness](data-inventory.md).

The signed, owner-only history application can answer stored-data questions using the existing configured workbook and readable calendars selected by the server. These queries use the current read permissions; no new scopes or credentials are needed. They do not append expenses, create events, change a draft or create a confirmation nonce. Conversation receipts and the existing usage ledger continue normally.

The model selects `query_expenses`, `query_calendar` or `query_table` as needed, without a semantic keyword pre-router. It sees recent dialogue, the authoritative Bangkok timestamp, actual schemas and the latest verified delivered query scope. Code validates arguments and performs configured-source reads. With `continue_analysis=true`, complete raw results become SQL relations; expense SQL inputs use `view=list`. A source push does not deploy this change.

Examples:

- `สรุปรายจ่ายเดือนนี้` — total saved expenses for the current Bangkok month.
- `ค่าอาหารวันนี้เท่าไหร่` or `สรุปรายจ่ายเดือนนี้หมวดอาหาร` — resolve an existing catalog category; ask if the label is unknown or ambiguous.
- `ดูรายการรายจ่ายตั้งแต่วันที่ 2026-11-01 ถึงวันที่ 2026-11-07` — total and saved entries for an inclusive range.
- `เฉพาะหมวดย่อยเครื่องดื่ม`, `แยกรายการให้หน่อย`, `แล้วเดือนก่อนล่ะ` — continue the latest delivered query when that catalog label exists.
- `ดูนัดพรุ่งนี้`, `สัปดาห์หน้ามีนัดอะไรบ้าง`, `ดูนัดถัดไป` — list saved timed/all-day occurrences across the server-selected readable calendars.
- `เดือน พ.ย. มีนัด TA กี่ครั้ง` — read the whole November frame, then semantically classify appointments and count the matching event references.
- `ใช้ไรเยอะสุด` — follow the verified period, sum by saved item name and rank group totals highest first. `รายการไหนแพงสุด` ranks individual payments instead.
- `เรียงยอดมากไปน้อย` — sort the complete matching input numerically before selecting displayed rows.
- `เดือนที่แล้วที่ไปคุมสอบผมใช้ตังไปเท่าไหร่` — find matching Calendar appointments, derive their Bangkok dates, then sum all expenses on those dates.
- `แล้วเมื่อวานล่ะ` — continue the latest verified delivered query. Clarification/chat replies do not erase that read scope; failed delivery, a different workbook or an expired receipt cannot supply it. Write consent still requires the latest delivered review.

A new explicit expense question starts with all items unless it supplies a filter. Date-only follow-ups retain the preceding filter; `ทั้งหมด` clears it. Calendar subjects/types/conditions are copied literally from cited owner wording into `filter_text`. The second model request classifies the complete fetched event set semantically; there is no keyword filter, Calendar `q` search, or model-generated provider query.

The model resolves wording and typos to exact existing item/category/subcategory labels supplied by the catalog; an unknown or ambiguous label requires clarification. No competing semantic parser chooses filters. Category totals use the current Database mapping, not a reconstructed historical category. Stored item identity shares the write path's exact/NFC/case/outer-whitespace, explicit alias and unique internal-spacing rules. Exact identities take priority over spacing collisions. Fuzzy typo suggestions never classify a stored expense, and line breaks are not joined to manufacture an identity. These comparisons do not rename or edit any Sheet cell.

When a complete input read contains an item whose category cannot be established, the tool returns `mapping_incomplete`, `complete=false`, and `mapping_diagnostics`. Diagnostics identify the unresolved stored names, occurrence counts, excluded amounts, missing/ambiguous identity or blank-category reason, and bounded candidate identities requiring review. They never infer a category from the wording. A separate `partial` object contains only the subtotal/count of safely identified and mapped rows; there is no top-level complete `expense` total. The reply explicitly labels it **ยอดบางส่วน**, names the items that need attention, states that those records may affect the requested category, and asks the owner to check the expense names and existing Database mappings. No mapping is changed automatically. A zero known subtotal is still partial and cannot mean no category expenses.

The structured result includes at most20 unresolved names; the reply shows at most10 names and discloses additional names. Counts and excluded amounts use all unresolved rows from the complete bounded read. A partial response does not claim a complete category listing. Optional blank subcategories remain valid catalog data; an unassigned category is actionable when asking for a category total. Schema/provider/incomplete-read failures cannot produce a partial subtotal, because the underlying financial input is not verified complete. Unfiltered expense totals do not require category mapping. Exact item-filter totals use only verified item identity, including unique spacing, and do not fuzzy-merge unfamiliar names.

Dates use Asia/Bangkok. Weeks run Monday–Sunday; relative dates are anchored to the current owner message timestamp. The model interprets Thai month names and abbreviations, Buddhist years, ISO dates and day/month/year dates into typed calendar frames or explicit inclusive dates. Relative week/month/year offsets are selected by the model and expanded using calendar arithmetic on the authoritative Bangkok date. New periods carry a literal owner `period_text` citation; the current clock alone cannot fill an omitted period. For filter/view-only follow-ups, `date_mode=previous` explicitly selects dates from the latest verified query receipt; it cannot invent a default when that receipt is absent. An unqualified month uses its first through last day; an explicit “จนถึงวันนี้” clips the end to today. Missing or conflicting periods require clarification. Expense queries retain the existing limit of 367 inclusive dates. Calendar queries cover at most31 inclusive dates, including a whole named month. The model-selected `upcoming` mode for “นัดถัดไป” still covers the current time through the next six dates and includes events still in progress.

Expense totals use Decimal arithmetic over a complete bounded input read. Sheets identity, headers and configured grid bounds must pass. Malformed values, incomplete mappings, access failures or an oversized grid yield an unavailable response rather than a zero balance. The legacy expense ledger has no income mapping. Income/balance questions require explicitly configured income data and the necessary sources; an absent source is not a known zero.

`query_expenses` composes exact filters with `group_by` (`none`, `item`, `date`, `category`, `subcategory`), `order_by` (`date`, `amount`, `count`, `label`), `descending` and a displayed `limit` of1–20. Group amounts use Decimal sums and group counts include every matching payment; sorting happens before display truncation. Ranking requires `view=list`. `count` requires groups; date ordering requires individual rows or date groups. Invalid combinations fail explicitly. Category/subcategory grouping uses verified current catalog mappings; unresolved/blank mappings make the ranking unavailable without a guessed zero or silently omitted group. This grouping failure does not create a provisional category proposal. Item/date grouping does not require catalog mappings. The result discloses grouping/order and full-input totals separately from displayed top rows. Grounded row facts cannot be reordered by the wording model.

For activity-conditioned spending, first call `query_calendar` with the literal activity in `filter_text` and `continue_analysis=true`, then call `query_expenses` with its current-request `calendar_query_refs`. `date_mode=previous` can derive the interval directly from these Calendar reads. Code requires complete Calendar coverage of the expense interval, complete classifications and occurrence references. It unions matching Bangkok dates, includes each payment once even with several same-day events, clips spanning events to verified windows and treats event end times/dates as exclusive. All-day/timed events ending at midnight do not add the next day. Unknown, duplicate, stale, incomplete, uncertain or insufficient-coverage refs fail without an unconditional total or false zero. A complete empty Calendar result can establish zero matching dates. This join can compose with exact expense filters, grouping and ranking. It reads all spending on matching dates; it does not prove that each expense was caused by that appointment. Replies disclose that distinction, the selected dates, criteria and event count.

Calendar refs expire with the request. A follow-up retains the verified period, requested presentation and activity condition as context, but must re-read Calendar before applying that condition on a new turn. Reusing `previous` without fresh Calendar refs cannot silently broaden the answer to every date. Planner instructions and `query_capabilities` describe supported operations, distinguish activity conditions from expense category filters, require executing supported requests instead of promising a future sort, and require honest statements for unsupported operations. An output-claim guard also rejects recognized narration of query execution when no read ran; it is not a universal language classifier or proof of planner accuracy.

Calendar reads include transparent events and omit cancelled events. Timed events display Bangkok times; all-day exclusive end dates display the correct inclusive last day, including externally saved multi-day events. Pagination is limited to eight pages of20 events per calendar, within the existing overall RPC/deadline bounds; an unfinished or inconsistent result is reported unavailable. A shared invitation is counted once across calendars, while recurring occurrences remain separate. Successful replies show at most20 entries/events and disclose when more exist. Counts use all verified matches before display truncation. Long labels are shortened for display. A complete empty read can report zero saved records.

Semantic Calendar analysis uses the existing response-realization opt-in and second-call reservation. It sends at most120 deduplicated events with original titles, times, up to1000 description characters and500 location characters; truncated details are explicitly flagged. Attendees and other provider fields are not sent. Its strict output must label every supplied event reference `match`, `exclude`, or `uncertain` and choose a bounded Thai phrase. Code validates membership and calculates counts, then renders original event facts. It does not admit an invented event or model-supplied number. Uncertain events are listed separately (up to five), `complete=false` prevents a complete-total claim, and only confirmed matches contribute to the displayed count. Ambiguous abbreviations must remain uncertain without supporting context. Provider text and conversation are untrusted data; they cannot grant tool or write authority. This grounding does not prove that the model's semantic classifications are correct.

The schema uses [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses); refusal, incomplete-output and application-level membership checks remain mandatory.

The semantic request is capped at48,000 encoded bytes,2048 output tokens and an eight-second foreground deadline, further bounded by the owner request's remaining time. Oversized candidate sets/context, missing read access, unfinished pagination, unavailable inference, exhausted budget, refusals and malformed outputs return an honest unavailable/clarification response with no guessed zero or keyword fallback. A repeated signed webhook reuses its persisted reply without new reads or model requests. Ordinary unfiltered queries retain their existing fact-slot realization.

For multi-part questions, `continue_analysis=true` retains the complete read as a current-request result reference and lets the model continue. There is no fixed model-call count for a raw-history owner turn. Each subsequent request includes every preceding call/result pair, while actual workbook/calendar identifiers stay out of tool feedback. In this phase only saved-data read tools and `finish_query_analysis` are available; draft preparation, edits and confirmation tools are absent. The model can read Calendar, infer matching appointments, then query expenses for those dates. Individual query windows and inference batches remain bounded, so larger questions can be decomposed into multiple reads rather than enlarging a single model context.

`finish_query_analysis` accepts current-request references and an operation: `report`, `expense_sum`, `calendar_count`, or `compare`. Code performs the arithmetic. Expense addition rejects overlapping inclusive date intervals; Calendar union uses stable occurrence references across overlapping reads and calendars. Comparisons require complete reads of the same domain. Incomplete reads or uncertain Calendar membership cannot produce an aggregate total. A report preserves each original incomplete-result qualifier. Duplicate, invented or previous-turn references are rejected. Final replies disclose the actual source periods; combining a few windows does not establish coverage of a larger unqueried period. Plain model prose cannot replace these grounded facts.

An uncertain activity read also attaches a bounded clarification to its existing
receipt: the original question and period, activity, confirmed occurrences and
up to five displayed candidates. `pending_query` exposes this context on later
turns after verified delivery, within the same owner, expense dataset, selected
Calendar configuration and 24-hour history window. No separate pending-write
workflow, collection or migration is used. A completed new read or a business
review replaces this read focus; plain chat and failed reads can preserve it.

For a spending follow-up such as `มีวันที่ 14`, the planner selects candidate
references with `resolve_activity_dates`, citing a literal selection from the
current owner message. The model interprets the language; the server checks
reference ownership and delivery, re-reads Calendar, rejects changed/deleted
occurrences and calculates the expense subtotal on the confirmed Bangkok dates.
The selected event must also agree with any unambiguous explicit date in that
literal evidence. `decision=exclude` records a rejected candidate without
confirming another candidate or creating an expense total. For example,
`วันที่ 14 ไม่ใช่คุมสอบ` excludes day 14 and leaves day 29 unconfirmed; it
cannot authorize a day-29 subtotal. Exclusions survive restart alongside the
remaining candidates, and do not contribute to later confirmed-date sums.
The original month supplies the month/year for the displayed candidate, even
when the current clock is in another month. Known matches and subsequently
confirmed candidates are unioned; two events on one date count expenses once.
Unconfirmed appointments remain visible for later confirmation. The reply calls
the amount a confirmed-date subtotal and discloses any unresolved candidates;
it never presents those dates as complete coverage of the original activity or
as proof of costs caused by the event.

While such a clarification is pending, an unconditional expense read with
`date_mode=previous` cannot silently broaden the selection to the entire month.
That error, missing tool dates and stale request-local Calendar refs return
the pending context to the ordinary read-only model loop for repair. Repeated
calls/reads, deadlines and accounting gates still stop the loop. If the planner
cannot resolve the selection, the fallback lists the actual candidates and asks
which appointment, preserving the known period. An unrelated new question can
use its own explicit period normally. Offline tests exercise these contracts
with scripted choices; real model interpretation remains a separate evaluation.

The loop ends on a final answer/tool result, failure, repeated identical read or invalid duplicate call ID. Removing the turn-call ceiling does not remove provider/context/output limits, the existing owner RPC/deadline scope, configured daily/monthly accounting gates or unknown-attempt/replay protections. Completed proposal caches for every step are cleared together with the production receipt; fingerprints, reservations, usage and unknown attempts remain. Account settings and production activation are separate from a source change.

The optional reply phase composes natural Thai by selecting phrases around separate immutable period, filter, total and count slots. Verified row details are appended by the server; the model need not repeat or enumerate them. Partial-result qualifiers and resolution instructions are also required immutable facts, and unresolved names are appended as locked rows. Amounts/counts include their units, preventing unit swaps; required schema fields retain every header fact exactly once, and per-slot phrase enums cannot invent dates, quantities, names, success or change units. A model failure returns a natural server fallback. Distinct non-sensitive reason codes distinguish invalid arguments, unknown filters, incomplete mappings, schema errors, provider failures and unfinished reads. They do not establish the cause of any earlier live incident. A terse dated title such as `พรุ่งนี้ Workshop` asks whether to add an appointment or view saved events, and creates no draft. This also applies after a previous query.

Run the offline regression suite and `python scripts/check_ci.py` as described in [development](development.md). Offline tests inject explicit model tool choices; they do not demonstrate real language understanding. The synthetic inference corpus in `tests/fixtures/query_inference.json` defines paraphrase, follow-up, fault, injection and write-regression scenarios for a separately authorized paid runner; private credentials, responses, cost receipts and approvals stay outside the checkout. Provider tests cover inclusive boundaries, midnight/leap-year dates, totals/listing, filters, all-day events, pagination, incomplete reads, cold restore/replay and confirmation isolation. A source push or passing CI does not deploy this feature.
