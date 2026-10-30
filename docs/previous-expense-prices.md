# Choosing a previous expense amount

The optional `PILOT_PREVIOUS_EXPENSE_PRICES_ENABLED` flag defaults to `false`.
With history/state enabled, a missing amount can produce a question grounded in
the configured expense history. It creates no recurring schedule or automatic
transaction and needs no new Sheet columns or owner-maintained subscription list.

For example, with a synthetic saved payment of 1,200 THB for `ค่าส่วนกลาง` on
September 3, an owner message `บันทึกค่าส่วนกลาง` can show that saved amount and
date. `ใช้เดิม` selects it for a new draft dated Bangkok today. The assistant
then shows the ordinary final review; a separate `ยืนยัน` saves the expense.
`เดือนนี้1250` supplies a new amount instead. An explicit transaction date is
retained independently of the historical payment date.
The existing expense policy still rejects a transaction date in the future.

## What the suggestion means

The displayed number is a **previously saved transaction total**, not a public
subscription price, guaranteed fixed price or inferred price per trip/month.
The item must resolve exactly in the current catalog; history uses the same
normalized item name. Similar labels and separate plans do not share prices.
Unknown, ambiguous or typo-like labels require resolution and an amount.

For an ordinary missing-amount offer, the server reads the complete configured
bounded grid and selects the most recent
calendar day strictly before both today and the new transaction date, and shows
its amount and source date. If earlier saved amounts differ, the question says
that history contains several amounts. Future and same-day records do not supply
the suggestion. Conflicting totals on the latest historical day or a nonempty
remark on that day produce no suggestion; the server does not fall back to an
older convenient price. Invalid, zero, negative/refund or formula amounts cause
the bounded read to fail closed.

The Sheet has no per-row currency, quantity or billing-plan fields. Requests with
foreign currency or quantity/count evidence do not get inferred prices. Stored
annotations can signal bundles or plan differences, so the conservative rule
above excludes them. Unannotated totals cannot establish unit quantity or a
fixed billing contract; the owner must decide whether to use the shown total.

For multiple missing amounts, name the item or explicitly choose all displayed
amounts. A bare `ใช้เดิม` cannot choose one of several unresolved entries. Once
one remains, it can answer the latest delivered question. A Calendar or other
interruption requires a fresh price question before using its reference.

## Evidence and confirmation

### Repeating a payment

Current expense proposals can set `reuse_previous_amount=true` on selected
new entries when the owner asks to repeat saved payments. `amount` and
`amount_source` remain null; the server supplies the amount from verified
history rather than accepting an invented owner quotation. The current
request supplies intent and the item quotation, and ordinary date references
retain a conversationally selected day.

This lookup includes the proposed expense day and selects the latest
unambiguous, unannotated saved amount on or before it. Two different amounts
on that latest day require clarification; the lookup does not choose an older
convenient amount. A future payment cannot supply the value. The exact
catalog identity, configured storage scope and existing price feature flag
still apply. Each repeated payment remains a new occurrence for full review;
the original record is not edited and a matching saved payment is not silently
treated as already completed.

Repeat proposals use `repeated-expense-amount-v2` proof metadata, including
source date/hash, storage binding and the current request. The older
`previous-expense-price-v1` offer/selection workflow above keeps its strictly
earlier-day semantics. Both proofs are reread before confirmation and the first
write attempt. Relevant source changes stop the write; final delivered-review
confirmation and idempotency remain required. Older runtimes do not support
the v2 proof: use a compatible new image for recovery, without restoring state.

### Selecting an offered historical amount

The model receives an owned opaque price reference, amount, source date and
whether history varies. It can select that reference after a matching owner
reply; it cannot fabricate money evidence or treat the reply as final save
consent. Server checks bind the delivered offer to the owner, workbook, draft,
version, item, source fingerprint and current choice. Raw owner text stays intact.

History is shown when the owner needs to select an amount. Once selected, the
ready review displays the proposed payment amount and transaction date, without
repeating the old payment history. Source dates and fingerprints remain in
internal proof metadata for freshness checks. An ordinary additional payment
does not trigger a same-name/same-day warning or an unsolicited history read.
Explicit requests to check whether a payment exists retain their checks.

The server rereads source evidence before selecting the price, before final
confirmation and before a prepared action's first attempt. Whole-row relocation
does not invalidate the fingerprint, but relevant historical changes do. If a
selected source changes, the assistant stops and asks for an explicit new amount
or cancellation; it does not silently substitute a new price. An explicit owner
amount removes the historical-price dependency, including when it equals the
previous amount. It still requires a new delivered review and confirmation.

This uses the existing Sheets reader and normal idempotent append path. Reads
and writes are not a transaction across external Sheet writers: source freshness
checks cannot provide Sheets CAS or undo an already completed external write.
The existing no-overlap writer policy remains necessary.

## State and recovery

Optional `previous_price` metadata lives inside existing draft entries. Accepted
bases are copied into the ordinary Action metadata for first-attempt validation.
See the [field guide](database-schema.md). No collection or Sheet migration is
required; the original snapshot envelope and raw history are preserved.

Recovery must use the same new image with the price flag OFF, retaining the
existing history/state, Calendar and other reviewed features. An old image is
not assumed safe for actions carrying this new proof. OFF blocks offered or
selected prices and unattempted price-derived actions. Executing/uncertain
actions perform positive reconciliation only: a verified committed result can
complete, while an unresolved result stays uncertain without another append.
An owner can still supply an explicit amount for a fresh ordinary review.

This is feature-OFF recovery, not full-code rollback. It cannot undo saved
transactions. Never restore old snapshots over newer work or reset raw history,
usage or ledgers. Local tests do not establish a live traffic rollback or live
LINE/Google end-to-end result. Publishing source does not deploy this feature.

## Validation

[Signed offline regressions](../tests/test_previous_expense_prices.py) cover
grounded/varied/missing history, plans, count/currency exclusions, explicit
correction, stale evidence, independent dates, multiple items, cancellation,
delivery/expiry/reference guards, replay, prepared/uncertain recovery and mixed
expense/Calendar/historical reviews. The
[synthetic development corpus](../tests/fixtures/previous_expense_prices_development.json)
defines fresh model scenarios separately from scripted tests. Model responses,
usage, approvals and release receipts remain private. Development examples are
not held-out accuracy or human style scores.
