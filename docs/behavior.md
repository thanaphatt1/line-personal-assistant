# Behavior and examples

All examples below are synthetic. Amounts are THB and dates/times use Asia/Bangkok.

## Structured data and SQL

The assistant works with metadata-defined tables in Google Sheets/Firestore
and Calendar events. Expense logging is one workflow, rather than the only
meaning of a table. For example:

- `เดือน ก.ย. ขอรายจ่ายรวมรายวันพร้อมยอดสะสม` reads the period and calculates grouped/window results with SQL.
- `รายรับของคนที่โทรแล้วเป็นเท่าไร` can join configured income/person tables using their relationship and status metadata.
- `หนังสือเล่มไหนหนากว่าค่าเฉลี่ย` can query a configured books table with title/pages columns; no date or money is required.
- `วันนี้รายได้ 1000` prepares an insertion into a uniquely selected configured income table, followed by review and confirmation.

The planner sees actual schemas and source scope, retrieves complete matched
records, then proposes read-only SQL through `query_relations`. Simple typed
reads remain available. Derived results can be queried again within the turn.
Incomplete reads cannot prove a complete total; an unresolved SQL error cannot
be replaced by a different earlier summary. See [SQL details](relational-queries.md)
and [table setup](tables.md).

## Expenses

`กาแฟ 35 บาท และรถเมล์ 20 บาท เมื่อวาน` prepares two entries. The assistant resolves existing Database names, asks for missing or ambiguous facts and presents the full date and amounts before saving. New names require an explicit existing/new choice and owner-supplied category/subcategory; the model cannot infer a category from the name.

`สรุปเมื่อวาน` is a read request. It does not create a save button. A reply such as a changed amount updates only the selected pending entry. Topic changes do not cancel a draft.

The established expense ledger does not handle income/transfers. Those require
an appropriate configured table. Saved expense corrections use the separate
optional [saved-record workflow](saved-record-edits.md); generic table
insert/update/delete proposals follow their declared operations. Invalid or
ambiguous requests clarify without writing.

## Calendar

`เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า` prepares an incomplete draft and asks:

> นัดนี้ทั้งวัน หรือเริ่มกี่โมงครับ?

`ทั้งวัน` keeps that draft's title/date and switches to a date-only event. `9:00` keeps the same facts and asks only for an end time or duration. `9:00-10:00` completes the time range. Explicit `ทั้งวัน` never asks for clock fields. Google Calendar all-day events use `start.date` and the exclusive next-day `end.date`.

A new request may contain up to eight events, including a mixture of timed and all-day appointments. Clarification targets only the unresolved member. An immediate ordinal edit after a delivered group review is bound to that displayed group; a similarly named older group cannot silently replace it. Ambiguous references ask for selection.

Only confirmation of the delivered, unchanged review creates events. Edits, mode switches, expiry, cancellation or failed delivery invalidate prior consent. Cancellation removes pending drafts and does not delete already-created events. Duplicate webhook delivery must not duplicate an event.

Creation targets one configured calendar and does not include guests, recurrence
or reminders. Saved-event rescheduling uses a separate optional reviewed flow;
saved-event deletion is not supported. Multi-day all-day creation requires a
single date or separated events. Conflict warnings cover the write calendar;
reads can combine calendars accessible through the configured account.

`เดือน พ.ย. มีนัด TA กี่ครั้ง` reads the entire named month across the
configured/readable calendars. A second model request uses titles, descriptions,
locations and relevant owner wording to label which saved appointments match.
Code counts the selected event references and renders a natural Thai reply with
the original appointments. Ambiguous matches are shown separately and do not
become a definitive total. A failed or incomplete read/analysis cannot mean zero
matches. Calendar query ranges support up to31 dates; analysis supports at
most120 events before the existing20-row display limit. This analysis requires
response realization enabled. A simple question usually uses two model
requests; multi-step analysis can continue without a fixed turn-call limit.

## Replies and limits

Expense reads can sort all matching payments numerically before limiting the displayed rows, or sum/count by item, date, category or subcategory before ranking. “ใช้ไรเยอะสุด” defaults to item totals; a request for the largest individual payment uses ungrouped rows. Follow-ups retain the last verified delivered read period through clarification/chat turns. This read focus grants no write consent.

For spending on dates of an activity, the model first reads Calendar, then supplies the complete current-request Calendar result refs to the expense tool. Code derives and unions Bangkok dates, counts each matching payment once, and discloses the selected dates. Ambiguous/incomplete Calendar classification cannot produce a complete total. “คุมสอบ” can use EXAM/proctoring/invigilation context; an EXAM-only title with an unclear owner role remains uncertain. Same-day spending is not proof that the appointment caused that expense. A later turn must re-read Calendar; old refs cannot silently broaden the query to every day.

Saved appointment lists are grouped by Bangkok calendar day, with a Thai
weekday/date heading and numbering that restarts at one for each day. All-day
appointments appear before timed appointments; timed appointments are ordered
by start time. An appointment spanning multiple days is shown once with its
date range or end date. If it began before the requested period, it appears
under the first requested day with its original start date retained. Counts
still refer to appointments, and the existing 20-appointment display limit
applies. Day headings and rows stay fixed during optional reply realization.

The optional response phase composes short connective wording around immutable names, dates, amounts, outcomes and confirmation instructions. It does not freely rewrite facts. Its foreground deadline is four seconds for ordinary replies and eight seconds for Calendar semantic selection, within the overall request deadline. Failure returns the deterministic server reply without another model attempt; a semantic-selection failure leaves the requested count unavailable.

The raw-history workflow has no fixed model-request count per owner turn. Reads
with `continue_analysis=true` can feed `query_relations`, further source reads or
legacy `finish_query_analysis` combinations. Subsequent tools remain read-only.
Python calculates the proposed query; plain model prose cannot replace its
results. Repeated reads/plans and duplicate call IDs stop. Provider deadlines,
context/output bounds and configured accounting gates remain. Observational
mode still records usage and is not a billing-account spending cap.
