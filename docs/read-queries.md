# Read saved expenses and appointments

The signed, owner-only history application can answer stored-data questions using the existing configured workbook and secondary Calendar. These queries use the current read permissions; no new scopes or credentials are needed. They do not append expenses, create events, change a draft or create a confirmation nonce. Conversation receipts and the existing usage ledger continue normally.

Examples:

- `สรุปรายจ่ายเดือนนี้` — total saved expenses for the current Bangkok month.
- `ค่าอาหารวันนี้เท่าไหร่` or `สรุปรายจ่ายเดือนนี้หมวดอาหาร` — resolve an existing catalog category; ask if the label is unknown or ambiguous.
- `ดูรายการรายจ่ายตั้งแต่วันที่ 2026-11-01 ถึงวันที่ 2026-11-07` — total and saved entries for an inclusive range.
- `เฉพาะหมวดย่อยเครื่องดื่ม`, `แยกรายการให้หน่อย`, `แล้วเดือนก่อนล่ะ` — continue the latest delivered query when that catalog label exists.
- `ดูนัดพรุ่งนี้`, `สัปดาห์หน้ามีนัดอะไรบ้าง`, `ดูนัดถัดไป` — list saved timed/all-day occurrences in the configured Calendar.
- `แล้วเมื่อวานล่ะ` — continue the latest delivered query. An unrelated response, failed delivery, different workbook or expired receipt does not supply query context.

A new explicit expense question starts with all items unless it supplies a filter. Date-only follow-ups retain the preceding filter; `ทั้งหมด` clears it. Filters use exact existing item/category/subcategory labels and existing category components. The Thai food/transport aliases resolve only to a unique existing catalog category. Category totals use the current Database mapping, not a reconstructed historical category. Missing or ambiguous stored-item mappings prevent a category total from being presented as complete.

Dates use Asia/Bangkok. Weeks run Monday–Sunday; relative dates are anchored to the current owner message timestamp. Thai month names, Buddhist years, ISO dates and day/month/year dates are accepted. Missing or conflicting periods require clarification. Expense queries retain the existing limit of 367 inclusive dates. Calendar queries cover at most seven dates; “นัดถัดไป” covers the current time through the next six dates and includes events still in progress. Queries do not search other calendars or filter event titles/types.

Expense totals use Decimal arithmetic over a complete bounded input read. Sheets identity, headers and configured grid bounds must pass. Malformed values, incomplete mappings, access failures or an oversized grid yield an unavailable response rather than a zero balance. Income has no verified storage mapping: it is unsupported, not zero, and a remaining balance cannot be computed.

Calendar reads include transparent events and omit cancelled events. Timed events display Bangkok times; all-day exclusive end dates display the correct inclusive last day, including externally saved multi-day events. Pagination is limited to three pages of 20 events; an unfinished or inconsistent result is reported unavailable. Successful replies show at most 20 entries/events and disclose when more exist. Long labels are shortened for display. A complete empty read can report zero saved records; it does not establish availability on other calendars.

The optional existing reply phase can arrange only admitted factual text. A model failure returns the server's factual reply. Query/date/filter resolution is bounded; unfamiliar wording may require clarification. A terse dated title such as `พรุ่งนี้ Workshop` asks whether to add an appointment or view saved events, and creates no draft. This also applies after a previous query.

Run the offline regression suite and `python scripts/check_ci.py` as described in [development](development.md). Synthetic provider tests cover inclusive boundaries, midnight/leap-year dates, totals/listing, filters, all-day events, pagination, incomplete reads, cold restore/replay and confirmation isolation. A source push or passing CI does not deploy this feature.
