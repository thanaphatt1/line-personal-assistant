# Documentation

These guides describe the current source. Feature flags and configured access
control activation; publishing source does not deploy a running service.

| Start here | Purpose |
| --- | --- |
| [Project overview](../README.md) | Capabilities, illustration, stack and local setup |
| [Runtime flow](system-flow.md) | Diagrams separating interpretation, SQL, provider reads and confirmed writes |
| [Architecture](architecture.md) | Table/Calendar abstractions, modules and state boundaries |
| [SQL analysis](relational-queries.md) | Primary composable query layer over complete observed snapshots |
| [Table metadata](tables.md) | Sheets/Firestore schemas, relationships and reviewed CRUD |
| [Behavior and examples](behavior.md) | User questions, record changes and Calendar behavior |
| [Source read tools](read-queries.md) | Expense/Calendar retrieval, simple reads and legacy combinations |

| Setup and operations | Purpose |
| --- | --- |
| [Configuration](configuration.md) | Explicit targets, feature flags and secret bindings |
| [Storage backends](storage-backends.md) | Expense storage in Sheets or Firestore |
| [Initialization](storage-initialization.md) | Explicit setup of a prepared empty resource |
| [Database and fields](database-schema.md) | Business data, temporary SQL and internal state contracts |
| [Development](development.md) / [Tests](../tests/README.md) | Local checks, synthetic cases and source packaging |
| [Deployment](deployment.md) | Separate release and compatible recovery |
| [Action traces](action-traces.md) | Redacted operational diagnostics |

Shared workflow details: [pending patches](pending-records.md),
[temporal plans and repair](temporal-plans.md), [data coverage](data-inventory.md),
[saved-record changes](saved-record-edits.md), [provisional categories](provisional-categories.md),
[historical category review](historical-categories.md) and
[previous expense amounts](previous-expense-prices.md).

Dated benchmark inventories and release-pinned schema references retain their
original scope; they are not a claim that every later feature is live.
