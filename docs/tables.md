# Metadata-defined tables and Calendar

The history runtime has two source abstractions: **tables** for structured
records and **Calendar** for appointments/time intervals. People, income, tasks
and shopping lists share the same table tools. Date and amount are optional
columns. Adding another supported schema requires metadata and an existing
accessible target, without a new domain function. Publishing and production
activation remain separate; startup never provisions or migrates business data.

## Registration

Set `PILOT_TABLE_REGISTRY_JSON` to an explicit JSON array in the history
production factory. It is empty by default and requires history/durable history
state. Keep filled settings outside this repository. Each entry contains:

- `table`: logical `id`, label, purpose, aliases, columns, allowed operations,
  optional `display_field` and up to five examples.
- `storage`: backend and exact private physical target, record capacity and
  explicit write policy.

The runnable [synthetic registration](../tests/fixtures/table_registry.json)
defines people in Sheets, family income in Firestore, and a shopping list, with
an income-to-person reference. For example, the people schema is:

```json
{
  "id": "people",
  "label": "ผู้ติดต่อ",
  "description": "คนที่รู้จักและลูกค้า พร้อมเบอร์ติดต่อ",
  "aliases": ["คน", "ลูกค้า"],
  "operations": ["query", "insert", "update", "delete"],
  "display_field": "name",
  "columns": [
    {"name": "name", "label": "ชื่อ", "type": "text", "required": true},
    {"name": "phone", "label": "เบอร์โทร", "type": "text"}
  ]
}
```

Logical IDs and column names are lowercase ASCII identifiers. `id` is a
read-only virtual record identity; `_id` is the Sheets identity header. Use
`storage_name` to map a logical column to a physical header/Firestore field.
Supported types are `text`, `integer`, `decimal`, `boolean`, ISO `date`, `enum`
with explicit `choices`, and `reference` with another registered table ID in
`references`. Phone numbers are text, preserving zeroes. Decimal values use
strings/integers and exact decimal arithmetic; NaN/infinity are rejected.
Unique declared column labels and optional column `aliases` resolve to logical
names. Ambiguous or invented columns are rejected; the tool returns the actual
schema for a corrected plan instead of silently choosing a different column.

Columns may specify `required`, a literal `default`, `default_today` for a
Bangkok date, `unit` and a description. Defaults apply only on insertion. A
partial draft retains its original default date across turns. The model receives
a compact table catalog, then loads the relevant schema with `describe_table`.
Spreadsheet IDs, project paths and provider permits remain server-side.

Purpose metadata selects the table. “วันนี้รายได้ 1000” can select family income
when its description explicitly establishes that meaning. If several tables fit,
the assistant must clarify. A people table adds no implicit date or amount.
Use `routing_group` for alternative destinations such as two income ledgers and
`default_for_group=true` for at most one explicit default. Without a unique owner
choice or that default, insertions ask which destination to use. Identical
schemas are also treated as competing destinations when no group is declared;
different schemas can share an explicit group. This check does not depend on
the model noticing ambiguity. A unique owner table name/alias overrides the
default; conflicting explicit table names require clarification.

## Shared operations

| Tool | Behavior |
| --- | --- |
| `list_tables` | Discover configured purposes and allowed operations |
| `describe_table` | Load schema, types, defaults, relationships and examples |
| `query_table` | Filter, project, group, aggregate, order and display records |
| `prepare_table_change` | Prepare insert/update/delete, retain partial facts and show a review |
| `check_table_change` | Read proof of an uncertain mutation without a write retry |

Filters support equality, inequality, ranges, text containment, membership and
null checks, combined with AND. Aggregates support count, sum, average, minimum
and maximum. Calculation and ordering use the complete matched input before
the display limit of 1–100 rows. Invalid columns/nonnumeric sums fail explicitly.
Missing access, bad schema and incomplete scans cannot become zero results.

Current-request result references support semi-joins through
`matching_query_ref`, `matching_field` and `source_field`. For example, filter
income to customers whose people records have status `pending`. The executor
uses all matching source records, including those beyond the display limit.
Select the source field when using a projection; `id` is always available.
Historical result references cannot be reused in a new request.
An empty literal membership list cannot stand in for an unread relationship.
The assistant must read the source first; a verified empty source result can
still establish zero through its query reference. Invalid aggregate/projection
combinations return planning feedback so the model can correct the request.

Calendar retains event/time tools. Its complete query references can constrain
a date column through `calendar_query_refs` and `date_field`. The executor
unions Bangkok event dates and respects exclusive ends without duplicate rows.
This establishes records on those dates, not causal costs of the event.
Uncertain/incomplete Calendar matches cannot establish a full table total.

When additional tables are configured, the existing ledger is exposed as a
read-only `expenses` table. Existing catalog/expense creation and edit tools
retain responsibility for its writes; no legacy data/category migration occurs.
New ordinary tables share the generic tools above.

## Changes and recovery

Changed values cite literal owner messages. Text stays literal, numbers must
match their evidence, and explicit dates are checked against the source's
Bangkok timestamp. Reference fields can select actually offered records by
display name; ambiguous names require another selection. Enum/boolean
interpretation follows metadata and is displayed for review.

Insertions apply declared defaults and ask only missing required fields. An
update/delete first queries with `for_change=true`, then selects an offered
ID/version. A fresh read supplies the before/after snapshot. A new owner
“ยืนยัน” applies the exact latest delivered unchanged review; “ยกเลิก” cancels
that draft. Chat/read interruptions, failed delivery, expired drafts or changed
configuration cannot authorize an old review. No model tool executes consent.
Read-only analysis exposes no preparation tool.

Proposals/outcomes use existing receipts, mutation attempts use existing durable
Actions. Keys bind owner, workspace, metadata, target and review. Replayed
webhooks return stored results. Uncertain effects block new writes and require
positive proof; the runtime never automatically retries them.

## Backend contracts and limits

**Sheets:** a dedicated tab has `_id` followed by physical columns in metadata
order. Existing records need unique stable IDs. Configure spreadsheet ID, tab
title, numeric sheet ID, `max_records` and `writes_enabled`. Writes additionally
require `single_writer_reviewed=true`; the app lease cannot fence external
writers. Values are literal typed cells, never formulas. Blank slots are reused;
deletion clears a row without shifting others. Keep formula/output columns
outside the managed block. Existing expense/catalog tabs are reserved.

**Firestore:** configure an application-owned `collection_path` in the runtime's
existing project/default database. Document IDs are stable; unexpected fields
or types fail explicitly. Reads use one read-only transaction across pages.
Conditional writes atomically include a proof under `assistant_table_operations`
for recovery after lost acknowledgement. Internal state/history/expense
namespaces cannot be registered. Full scans need no special query index.

References are validated against current records. Cross-provider transactions,
foreign-key/cascade enforcement, arbitrary SQL/code, nested document/array
fields, automatic schema creation and arbitrary business workflows are not
implemented. A new backend/operator needs engine/adapter work once; another
ordinary schema does not.

The current executor scans complete datasets: configured 1–10,000 records and
400,000 encoded bytes per snapshot, plus existing context/deadline constraints.
Oversized scans fail explicitly. Provider query pushdown is a future optimization
behind the same contract; configurable schemas do not imply unlimited volume.

REST contracts: [Sheets cell requests](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request),
[Firestore snapshot reads](https://docs.cloud.google.com/firestore/docs/reference/rest/v1/projects.databases.documents/list),
[atomic commits](https://docs.cloud.google.com/firestore/docs/reference/rest/v1/projects.databases.documents/commit).

## Validation

The table suites exercise people/income/shopping and a newly added book schema,
typed operations, full-input calculations, relationships, Calendar joins, partial
drafts, source evidence, reviewed changes, delivery/stale checks, restart/replay,
lost acknowledgements, pagination and exact transport permits. Signed webhook
tests run both REST adapters through cold production-factory instances with
synthetic services. These do not prove live access or general language accuracy.
