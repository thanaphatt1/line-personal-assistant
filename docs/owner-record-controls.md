# Reviewed removal, remembered names and saved summaries

These optional owner features extend the existing expense/catalog workflow.
All three flags default to `false`; publishing source does not activate them.
They support both configured expense backends. They do not extend Calendar
deletion or introduce a global memory shared across unrelated tables.

| Feature | Flag | Requirements |
| --- | --- | --- |
| Remove one saved expense | `PILOT_SAVED_EXPENSE_DELETE_ENABLED` | History/state/raw journal, saved expense edits and the backend's reviewed concurrency policy |
| Remember catalog aliases | `PILOT_CATALOG_ALIASES_ENABLED` | History/state/raw journal and an existing catalog target |
| Buttons after saving/editing expenses | `PILOT_RECORD_ACTIONS_ENABLED` | History/state/raw journal, saved expense edits, stable transaction IDs |

## Remove a saved expense

An owner can ask to remove a recently saved expense or identify one by name
and date. The planner uses `find_saved_records` and
`prepare_saved_expense_delete`. Several candidates require a delivered
selection and an owner choice; a multi-entry batch is never silently removed.
The server shows the exact item, amount and date. A separate fresh confirmation
of that delivered review authorizes one removal. Canceling leaves the record.

Sheets clears only A:D in a single `updateCells` request, retaining E's stable
`transaction_id` as a tombstone. Other rows, columns and formatting remain.
Reads and historical category scans skip that tombstone. A row's absence alone
does not prove a timed-out removal succeeded. A positive tombstone observation
can establish the desired removed state, without claiming who performed it.
The same [Sheets single-writer limitation](saved-record-edits.md) applies.

Firestore conditionally deletes the exact expense document at its reviewed
`updateTime`, updates the dataset count/revision and records a durable operation
receipt in one transaction. A missing document without that receipt is
insufficient proof. Unknown outcomes on either backend reconcile read-only;
they never issue another removal attempt, including with the feature disabled.

“Undo” here means reviewed removal of one saved expense. It does not restore an
earlier value of an edited record or recreate a deleted record automatically.

## Remember owner vocabulary

For example: `จำว่า เพลง หมายถึง Spotify`. The planner prepares an alias to
an exact existing catalog name, supported by the owner's original wording.
Only confirming the delivered review changes memory. A one-time name choice
or an inferred synonym does not create a persistent alias.

The owner can inspect aliases (ten per page), replace a mapping, or ask to forget one, with
a fresh review for changes. If conversation interrupts confirmation,
`review_catalog_alias` redisplays the change with a new confirmation token.
The review explicitly says the name will be remembered for future use.
Actual catalog names take precedence. Renamed/missing targets deactivate their
aliases rather than silently pointing to another name. Aliases let original
wording resolve to a canonical identity while preserving source text and spans.

Aliases belong to one owner and storage scope. They survive the rolling
24-hour dialogue window and feature-OFF recovery. No new business Sheet
columns or Firestore collection is created. Up to 100 aliases are retained in
the existing durable owner state; this is vocabulary memory, not arbitrary
conversation memory.

## Saved summaries and legacy buttons

New save/edit confirmations contain only the result summary. They do not append
record-action or remember-name menus. Owners can request a correction, removal,
source lookup or remembered alias in conversation. Confirmation buttons remain
on pending reviews, where a decision is needed.

Previously delivered `แก้ยอด`, `เปลี่ยนวันที่`, `ดูต้นทาง` and
`นำรายการออก` controls retain their existing expiry and validation. Their
receipt content hashes are not rewritten. A batch first asks which saved
record to use. Amount/date buttons select a
fresh record and ask for the new value; ordinary tool validation and a separate
review/confirmation still apply. Opening a source never changes data.

The server binds buttons to owner, storage scope, stable record IDs, delivered
receipt, expiry and nonce. Typed button tokens, forged/expired controls and
undelivered messages cannot invoke the model or authorize a write. Sheets
source links resolve the ID's current row, including after reordering.
Firestore replies include the dataset/record ID and a Console link.

## State and release compatibility

Unused/default-OFF state remains `local-history-state-v1`. Using these features
persists `local-history-state-v2` with an owner/scope-bound
`owner_features_v1` extension. Aliases are durable; pending reviews and delivered
buttons retain their ordinary expiry. Turning flags OFF preserves this state.

Older runtimes that only accept v1 deliberately reject v2. Any future release
must retain a compatible runtime for recovery (for example the same new image
with these flags OFF). Do not roll back to v35 after new feature state exists,
restore an old snapshot over it, or treat changing flags as undoing a business
effect. No production migration or activation runs at startup.

Signed synthetic workflow tests cover cold restoration, replay, interrupted
confirmation, wrong/expired buttons, multi-record selection and field focus.
Provider tests cover row relocation, tombstones, conflicts and unknown writes.
The optional official Firestore emulator verifies conditional deletion and
receipt-based recovery independently of the fake REST server.
