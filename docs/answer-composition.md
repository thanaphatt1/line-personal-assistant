# Flexible read answers

Completed read results can use an optional model-composed document. The model
chooses paragraphs, headings, inline answers and lists from verified facts and
record cells. There is no fixed result sentence, automatic row suffix or
vocabulary list on this path.

For example, a single-day question can produce:

```text
เมื่อวาน (15 ต.ค. 2026) ใช้ไป 50 บาท จาก 2 รายการครับ
• ขนม 30 บาท
• ชา 20 บาท
```

Facts are written to slot into natural Thai. A whole month or year is named
(`เดือนพฤศจิกายน 2026`, `ปี 2026`) rather than spelled as a date range. Amounts
use one owner-facing format everywhere (`reply_display.money`): thousands
separators, with satang shown only when present (`1,250 บาท`, `32.50 บาท`).
Stored values keep their original precision. SQL result cells use verified
column units, not names such as `total` or `amount`; computed currency values
retain any extra decimal places rather than being rounded for display.
Latin-script names are spaced from
Thai text; Thai names are not. When placeholders are filled, a word the model
wrote right before a fact that already starts with it is dropped (`เฉพาะ {{f}}`
with `เฉพาะ อาหาร`), and numbers are spaced from adjacent letters; fact values
themselves never change.

For several days, the model can use a shared date heading for adjacent records.
Ranked results retain their actual query order. A scalar SQL result can become
a sentence instead of a list of column names and values. Generic table cells
come from the selected schema and columns; they do not require an expense
amount or date.

Source metadata can mark an optional annotation for omission when empty. For
example, blank expense notes do not force an empty note label into a reply.
This policy follows verified direct columns and aliases, not output names or
computed expressions. Raw SQL values, row counts and nonempty annotations stay
intact. Zero, false, unrelated missing fields and results consisting entirely
of blank cells remain visible. Cell IDs keep their original column positions
when an optional cell is omitted.

## Evidence and layout

[`answer_composition.py`](../src/line_assistant/answer_composition.py) builds a
request-local display view after a successful read. It exposes summary facts,
linked record cells, labels and coverage requirements. It does not fetch more
data, run calculations or grant write authority.

The model returns a document with text and record-group blocks. Templates use
fact/cell references such as `{{f1}}` and `{{c2}}`; the server substitutes the
original values. A group template is evaluated separately for each referenced
record, keeping names, amounts and other cells bound to that record. It can
place a shared cell in a heading only when that cell's original value, label,
type and unit agree for every member. Clipped display text alone cannot prove
a shared value. A heading equal to an entire shared display value can also be
resolved to that verified cell without another model call. Numeric substrings
and a value present in only some members cannot establish that context. A
verified single-day summary can cover redundant row dates.

The renderer requires all summary facts, all displayed records and their
required cells. Duplicate, missing, foreign or reordered records fail
validation. Filters, ordering notes, same-day spending caveats and truncation
notices remain required evidence. Source text resembling template syntax is
inserted literally and is never evaluated again.

When exactly one record is offered, text blocks may also reference that
record's cells directly. They can form one sentence or several paragraphs
without an artificial rows block. Its required cells must still be covered,
and combining an inline record with another rows rendering of the same record
is rejected as a duplicate. Multi-record cell references still need explicit
rows blocks, preserving membership and ranking order.

The composer also receives the executed logical scope and SQL, excluding
physical bindings and raw source rows. It must describe the actual calculation
rather than repeat an unfulfilled requested condition. A legacy expense
inclusion is supplied as the complete required fact `เฉพาะ <label>`, rather
than a bare name whose surrounding prose could silently change its polarity.
This does not prove that every SQL plan expresses the user's intent or that
every free sentence is semantically correct.

Free prose may connect these values and describe the result. The linked
renderer does not ban words such as บันทึก, ยืนยัน, กรุณา or ไหม, English action
words, or question punctuation. References to saved records and negated status
sentences must not fail merely because they contain those words. Literal
business numbers/dates, unknown references, URLs, markup and unsafe control
characters are still rejected; verified values must use placeholders.

**These checks do not prove the semantics of natural-language prose.** A model
can still describe a comparison, source or operation status incorrectly in
words. Instructions require the actual server status and prohibit invented
effects, but a rendered document is not an effect receipt. Operation documents
still require their authoritative status, warnings and confirmation facts;
rendering never changes the core status, review, consent, action or provider
receipt. Semantic evaluation must assess misleading prose independently of
structural acceptance. See [OpenAI's Structured Outputs guidance](https://developers.openai.com/api/docs/guides/structured-outputs).

Explicit `finish_query_analysis` clarification questions likewise use shape,
length and literal-data checks rather than action-word detection. The operation
remains an incomplete, read-only clarification with no write authorization.
The ordinary fact-slot path and diagnosed explanations now also use bounded
free prose with immutable fact IDs/reason codes. Their schemas have no word
patterns or before/after phrase enums. Tool-less planner text is not scanned
for completion words: it stays an answer without any verified operation effect.
See [language policy and typed checks](semantic-boundaries.md#language-policy-and-typed-checks).

## Scope and fallback

The composer supports completed ordinary Expense/Calendar reads, generic
table reads, relational SQL results and completed read-analysis reports.
Cross-read arithmetic/comparison assertions remain atomic verified facts;
their surrounding document can be laid out freely. Calendar semantic
classification keeps its existing separate decision/reply contract, including
its uncertainty handling. A completed multi-read report can subsequently use
the document composer.

Incomplete/error results, activity-selection follow-ups, inventory, drafts, confirmations and write receipts
retain their existing authoritative replies. The new document cannot change a
review, create consent or report a write. Query tools still compute, select and
sort the data; the reply model only presents it.

The existing response-realization flag and owner-bound usage reservation apply.
An optional response call is available after eligible table/SQL/read analysis
results. A fully received document that fails presentation validation can get
**one repair call** with structured validator feedback. There is no critic
call or new provider. Each document attempt reserves up to1,536
tokens; ordinary guarded replies retain768. The foreground deadline remains
eight seconds **shared by both attempts**, including reservation time, inside the overall request deadline;
ordinary guarded replies retain their four-second deadline. The existing24,000-byte body
and4,500-UTF-16-unit reply limits apply.

Feedback contains the rejected document, a fixed validation code/guidance and,
when applicable, missing fact IDs, missing row cells or the required record
order. These are local presentation references. The repaired request retains
the exact original facts, cells, executed query evidence and output schema;
it has no tools and cannot fetch, calculate, save or confirm anything. The
model rewrites the full answer and the same validator checks it again.
Rejected prose remains untrusted data, including any embedded instructions.

Only a received, usage-checked final JSON document rejected by the read
renderer qualifies. Timeouts, transport failures, refusals, malformed provider
envelopes, invalid JSON and usage-bound failures do not start a repair. Calendar
semantic classification, incomplete replies, write reviews and receipts retain
their existing paths. An unchanged or still-invalid repair stops after that
one additional attempt. The grounding checks remain in place; this loop does
not prove arbitrary SQL intent or free-prose semantics.

Every repair has its own owner-bound durable claim, body fingerprint and
budget reservation before model I/O. Reservations are not refunded. Completed
decisions can recover after a crash without retransmission; an unknown reserved
attempt cannot be retried. The existing receipt checkpoint clears consumed
candidate caches together, retaining accounting and unknown-attempt records.
Repeated LINE delivery reuses the delivered answer without new model requests
or business effects. If feedback exceeds the request bound, the budget is
unavailable or time expires, the original authoritative reply is used.

Invalid, unavailable, over-budget, over-size or late composition uses the
authoritative server reply. Its expense fallback also avoids repeated dates:
single-day rows omit dates, chronological spans use day headings, and global
amount/name rankings retain dates beside their original rows. The result and
delivered reply are retained for exact duplicate replay. Static validation codes
such as `answer_cell_coverage` can appear in realization metadata and traces;
they contain no owner text or values.

The scripted contracts in
[`test_answer_composition.py`](../tests/test_answer_composition.py) exercise
layout flexibility, shared context, record binding, malicious documents,
fallback and signed cold replay. Scripted output is not a model-quality
benchmark or a live business end-to-end test.

[`test_answer_repair.py`](../tests/test_answer_repair.py) covers correction from
validator feedback, fixed input facts, durable recovery, unknown attempts,
budget/deadline exhaustion, invalid feedback, replay and isolation from writes.

## Owner evidence and result references

The native `act` respond schema requires only `intent`, `response_ref` and
`document`. The server derives current owner evidence while rebuilding the
result. `response_ref` already binds the owner, active event, actual executed
query, facts and core reply. New proposals do not repeat `source_refs` or
self-review booleans. Legacy source/review fields are read conservatively:
foreign/mixed source refs and explicitly negative/unresolved reviews still
fail, without rereading a provider. Removing the redundant argument does not
make previous-turn, foreign or changed results valid.

For native `act` responses, an invalid presentation document no longer starts
another model turn once owner evidence, the current result proof and
completeness have passed. The server discards the rejected
document and builds a placeholder-only layout from those same facts and cells.
That layout passes through the same renderer, preserving every required value,
unit and record order. If it cannot pass validation, the response still fails
closed. Stale references, incomplete reads and negative/unresolved legacy reviews
never qualify. `reply_fallback` and bounded realization metadata record this
presentation fallback without logging the document or values. The optional
auxiliary composer described above keeps its existing one-repair contract.

A native completed response can retain an optional `read_query.answer_context`
snapshot of the selected result columns/records for follow-up interpretation.
The whole snapshot is omitted if it exceeds 6,000 UTF-8 bytes; records are not
silently clipped. Calendar observations contain only title/start/end/all-day
fields. It has no reusable request-local result reference. Existing scope,
source binding, receipt expiry and verified delivery checks govern admission
to `previous_query`. The model must read sources again for a new calculation;
this context never authorizes a write or confirmation. It helps retain a
selected subject as well as its dates, without a phrase-specific router or
automatic SQL rewriting. Its availability does not prove that a model will
interpret every follow-up correctly.
