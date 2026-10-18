# Historical category review

This development feature is disabled by default. It scans already saved expense
names that have no confirmed category and proposes existing canonical
category/subcategory pairs for owner review. It never edits the original expense
date, item, amount, Remark, formula or transaction ID.

With no period in the owner's request, the default is **all unmapped names in the
configured expense sheet**. An explicit month or inclusive date range is optional;
date ranges retain the existing367-date bound. The all-history default does not
impose that date limit. Every scan reads the complete configured grid. An oversized
grid, incomplete read, malformed input, formula in an input cell or duplicate
existing transaction ID fails closed rather than returning a complete zero.

The review displays at most **ten grouped names per page**, with total name count,
remaining names, affected transaction count, amount and first/last dates for each
group in the selected scope. Ten is a display limit, not a source-data cap. Names
are grouped by the existing NFC/case/outer-space normalization, not fuzzy matches
or invented aliases. Up to three equivalent spelling examples are shown with the
full variant count. Existing IDs participate in the server-owned snapshot; legacy
rows without IDs can be classified without assigning IDs or enabling row edits.

The model sees the current page's aggregates and canonical category choices.
Transaction rows, notes and IDs remain server-side. This feature does not expand
access to another workbook or Calendar. Stored names and provider content are data,
never instructions.

## Review and corrections

The owner can request the next/previous page, refresh, select only some names, defer
unclear names or cancel. Navigation does not save the previous page's proposals;
returning to it produces a fresh review. Every correction or selection change
creates a new review version and nonce. Examples include:

- “ถูกหมด ยกเว้นชื่อ X ให้เป็นหมวด Y”
- “เอาเฉพาะ X และ Y นอกนั้นพักไว้”
- “ขอหน้าถัดไป” or “ตรวจใหม่”
- “ยกเลิกจัดหมวดย้อนหลัง”

“All correct” concerns only clear entries on the **currently displayed page**.
Ambiguous names remain open. Possible aliases, spacing collisions and an existing
Database item with a blank category require separate owner resolution; the append
workflow cannot overwrite an existing mapping or create aliases/new categories.
If a review's labels are too long to display completely within the LINE text
budget, confirmation is disabled and the owner must select a smaller subset for
a complete review. A truncated review never authorizes a write.

## Two distinct consent scopes

**“ใช้ข้อเสนอย้อนหลังครั้งนี้”** gives a provisional summary for the selected clear
names on this displayed page only. The summary is explicitly partial/provisional;
it does not change Database or future query classification. This consent is
consumed. A later permanent save needs a fresh delivered review.

**“บันทึกหมวดย้อนหลัง”** separately confirms appending only the displayed selected
clear names to Database using existing category/subcategory pairs. This permanent
mapping affects the same stored identity across **all historical and future
queries**, including outside an optional scan date range. Counts/date ranges in
the review describe the selected scan scope; they are not a limit on permanent
mapping effects. No transaction or existing Database row is rewritten. Bare
“ยืนยัน”/“ใช่” asks which scope; a correction or mixed confirmation/correction never
commits. Explicit reference/nonce forms are also supported.

After a completed page, the owner can request the next batch. Previously displayed
but unselected/ambiguous names remain unmapped and are disclosed as skipped; a new
scan includes them again. The workflow does not declare all historical names
classified after a partial or paginated approval.

## Staleness, effects and recovery

Before confirmation, the server re-reads the complete source and catalog and
compares their fingerprints to the reviewed snapshot. Date/item/amount/Remark/ID
changes, removed records or changed catalog entries invalidate the review. Physical
row relocation alone preserves the content/ID multiset and does not select a
different record. The mapping adapter revalidates the catalog, canonical pairs,
name collisions, capacity and protections immediately before its one bounded
append. Old, expired, foreign, modified or undelivered reviews cannot authorize it.
If the process restarts after durable confirmation but before the first attempt,
the prepared Action revalidates the original source snapshot before writing.
Executing or uncertain Actions take only the read-only reconciliation path.

The owner must not edit, sort or insert rows while the bot processes an action.
Other integrations must observe the same serialization. The application owner
lease does not lock outside writers, and Sheets has no conditional compare-and-swap
between its final read and append. Wait for a terminal result and reconcile an
uncertain outcome before other writes. This remains an operating constraint,
not an enforced collaborator lock.

Confirmed writes use a durable owner/workbook/review/payload-bound Action. Replay
returns its receipt. An acknowledgement timeout triggers read-only positive
reconciliation, never a blind append retry; old-looking state stays uncertain.
Cancel affects only an uncommitted review and cannot undo an external effect.

`PILOT_HISTORICAL_CATEGORIES_ENABLED=false` is the additional opt-in. It requires
the existing history and provisional-category feature. Persistent mappings also
require the separate mapping flag and reviewed writer policy. Publishing source
does not enable these flags or deploy it.

Any release must stage a compatible same-image feature-OFF bridge, preserve raw
history/state and ordinary Calendar access, and prove pending/uncertain recovery.
Do not assume generic old-image persistence is executable recovery. Traffic
recovery cannot undo a mapping; never restore an old snapshot over newer data.
Development and inference tests use synthetic providers/names only. They do not
scan actual owner history, prove general classification accuracy or exercise a
real business write or traffic rollback.
