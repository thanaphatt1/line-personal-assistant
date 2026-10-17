# Behavior and examples

All examples below are synthetic. Amounts are THB and dates/times use Asia/Bangkok.

## Expenses

`กาแฟ 35 บาท และรถเมล์ 20 บาท เมื่อวาน` prepares two entries. The assistant resolves existing Database names, asks for missing or ambiguous facts and presents the full date and amounts before saving. New names require an explicit existing/new choice and owner-supplied category/subcategory; the model cannot infer a category from the name.

`สรุปเมื่อวาน` is a read request. It does not create a save button. A reply such as a changed amount updates only the selected pending entry. Topic changes do not cancel a draft.

Income, transfers and edit/delete of already recorded expenses are outside the current history workflow. Invalid or ambiguous requests clarify without writing.

## Calendar

`เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า` prepares an incomplete draft and asks:

> นัดนี้ทั้งวัน หรือเริ่มกี่โมงครับ?

`ทั้งวัน` keeps that draft's title/date and switches to a date-only event. `9:00` keeps the same facts and asks only for an end time or duration. `9:00-10:00` completes the time range. Explicit `ทั้งวัน` never asks for clock fields. Google Calendar all-day events use `start.date` and the exclusive next-day `end.date`.

A new request may contain up to eight events, including a mixture of timed and all-day appointments. Clarification targets only the unresolved member. An immediate ordinal edit after a delivered group review is bound to that displayed group; a similarly named older group cannot silently replace it. Ambiguous references ask for selection.

Only confirmation of the delivered, unchanged review creates events. Edits, mode switches, expiry, cancellation or failed delivery invalidate prior consent. Cancellation removes pending drafts and does not delete already-created events. Duplicate webhook delivery must not duplicate an event.

The production path supports creation only in one configured secondary calendar. It has no guests, recurrence, saved-event edit/delete or reminders. Multi-day all-day wording requires a single date or explicitly separated events. Conflict warnings cover that calendar only, not the owner's primary calendar or every account calendar.

## Replies and limits

The optional response phase composes short connective wording around immutable names, dates, amounts, outcomes and confirmation instructions. It does not freely rewrite facts. Its foreground deadline is four seconds; failure returns the deterministic server reply without another model attempt.

The workflow permits at most two model requests per owner turn. External requests have bounded RPC scopes and per-request HTTP timeouts of at most twelve seconds. Budget/count gates are enabled by default. Observational mode requires an explicit single-owner history configuration and still records reservations and usage; it is not a billing-account spending cap.
