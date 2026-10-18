# Explicit expense storage initialization

Run setup separately from the app, against a user-prepared resource and during
an exclusive setup window. Preview/check are read-only. Apply requires a saved
reviewed plan, its hash and `--exclusive-setup`. Application startup never calls
this command. Running it against real resources is a separate external write.

No spreadsheet creation, sharing, ownership, OAuth, ADC, credential discovery,
IAM changes, migration, environment-file edits or deployment is performed. The
CLI takes an existing short-lived access token on stdin and never prints it.
Keep token, catalog seed, preview and deployment settings outside this public
checkout. Do not paste tokens into arguments or commit private filled examples.

From a source checkout with its existing dependencies:

```sh
PYTHONPATH=src python scripts/initialize_expense_storage.py preview \
  --spreadsheet-id '<existing-workbook-id>' \
  --seed /private/catalog-seed.json \
  < /private/existing-short-lived-token > /private/setup-preview.json

PYTHONPATH=src python scripts/initialize_expense_storage.py check \
  --spreadsheet-id '<existing-workbook-id>' < /private/existing-short-lived-token
```

The preview envelope contains `plan` and `plan_sha256`. Review the precise target,
tab IDs, headers, seed and request list before a separately authorized apply:

```sh
PYTHONPATH=src python scripts/initialize_expense_storage.py apply \
  --spreadsheet-id '<same-existing-workbook-id>' \
  --plan /private/setup-preview.json --plan-sha256 '<reviewed-plan-hash>' \
  --exclusive-setup < /private/existing-short-lived-token
```

## Sheets behavior

New `All Expenses` tabs have 1001 rows by default, 24 A:X columns, exact A:E
headers `Date`, `List`, `Cost`, `Remark`, `transaction_id`, hidden E, and date/money
display formats on new empty input columns. New `Database` tabs have 26 A:Z
columns and headers `Item`, `Categories`, `Sub-categories`. The grid widths match
the runtime's reviewed append scans and transport allowlist. Numeric tab IDs are
chosen without colliding with existing IDs and returned for operator review.
An unrelated default `Sheet1` is preserved.

The initializer fills missing exact headers only in empty target tabs. Existing
correct populated schemas are checked without changing their data. Conflicting
headers, occupied extra headers, formulas in header cells, merged/protected
targets, unsupported grid sizes, missing headers on populated tabs or an
unhidden ID column on a populated tab require manual review. It never deletes,
renames, shrinks, overwrites or repairs those conditions.

The plan binds metadata and the complete bounded target cell/formula reads.
Apply recomputes it; a stale or modified plan does not write. The mutations form
one batch and are read back. A repeated apply to the desired schema is a no-op.
After a timeout, check the observed schema; do not blindly repeat the batch.
Sheets cannot provide an external-writer CAS, so stop other writers throughout
preview/apply and review again if another writer changes anything.

`schema_ready` and `catalog_ready` are separate results. A catalog seed is a
JSON list supplied by the operator, for example:

```json
[
  {"item": "Rail ticket", "category": "Transportation", "subcategory": "Rail"},
  {"item": "Apple snack", "category": "Food", "subcategory": "Snacks"}
]
```

These are synthetic examples, not assumed owner categories. Seed duplicate
normalized names are rejected. Existing catalog data must exactly match a seed;
the initializer does not merge or overwrite it. A schema without categories can
be initialized, but categories must be supplied before category-based work.

## Firestore alternative bootstrap

The same CLI supports `--backend firestore --project-id '<project>' --owner-id
'<configured-owner>' --dataset-id default`. Use the same project and default
database as the workflow state. Preview creates no documents. Apply uses
create-only preconditions to write a new ledger metadata document and explicit
catalog seed atomically; it creates no workflow state or expenses. The initial
seed is bounded to 99 catalog rows and 200 KB. Orphan business documents or a
conflicting existing schema/catalog cause a failure. Repeating a matching setup
is a no-op. There is no import or ongoing catalog overwrite command.

After setup, review IDs, complete schema/catalog, existing account access and
the backend configuration described in [storage backends](storage-backends.md).
The command does not set schema/readiness flags or change production. A backend
switch, live canary, migration and release require their own approved plan.
