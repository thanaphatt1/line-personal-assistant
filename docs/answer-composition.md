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

The composer also receives the executed logical scope and SQL, excluding
physical bindings and raw source rows. It must describe the actual calculation
rather than repeat an unfulfilled requested condition. A legacy expense
inclusion is supplied as the complete required fact `เฉพาะ <label>`, rather
than a bare name whose surrounding prose could silently change its polarity.
This does not prove that every SQL plan expresses the user's intent or that
every free sentence is semantically correct.

Free prose may connect these values and describe the result. Generated
business numbers, unknown references, obvious write/consent claims and
instructions are rejected. **These checks do not prove the semantics of all
natural-language prose.** A model can still describe a comparison or a source
incorrectly in words. Prompting, finite evaluations and retained traces are
needed in addition to structural checks; schema adherence alone does not prove
factual accuracy. See [OpenAI's Structured Outputs guidance](https://developers.openai.com/api/docs/guides/structured-outputs).

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
