# Documentation

These guides describe the current source. Feature flags and configured access
control activation; publishing source does not deploy a running service.

## Choose a reading path

| If you want to… | Read in this order |
| --- | --- |
| Get oriented in a few minutes | [README](../README.md) → [Behavior examples](behavior.md) → [Architecture](architecture.md) |
| Make your first code change | [Development setup](development.md#environment) → [Module map](architecture.md#module-map) → [Run tests](../tests/README.md#run-from-the-repository-root) → [Add a regression](../tests/README.md#add-a-regression) |
| Configure data and deploy | [Configuration](configuration.md) → [Storage backends](storage-backends.md) / [Table metadata](tables.md) → [Initialization](storage-initialization.md) → [Deployment](deployment.md) |
| Work on query planning | [Current intents](intents.md) → [SQL analysis](relational-queries.md) → [Source contracts and compatibility](read-queries.md) |
| Investigate a failed or slow answer | [Action traces](action-traces.md) → [Performance](performance.md) → [Answer composition](answer-composition.md) → [Semantic evaluation](../benchmarks/intent-v2/README.md) |

You do not need every reference page for a first contribution. The current read
entry point is `act`; older `query_*` names in detailed contracts describe
internal or compatibility handlers. See the [interface map](read-queries.md#current-interface-and-compatibility).

## Guide directory

| Start here | Purpose |
| --- | --- |
| [Project overview](../README.md) | Capabilities, illustration, stack and local setup |
| [Runtime flow](system-flow.md) | Diagrams separating interpretation, SQL, provider reads and confirmed writes |
| [Architecture](architecture.md) | Table/Calendar abstractions, modules and state boundaries |
| [Read intents](intents.md) | Current `act` entry point: read, inspect and respond |
| [Answer composition](answer-composition.md) | Linked result facts, layout and validation boundaries |
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
| [Performance](performance.md) | Turn measurement, workflow comparison and token usage |
| [Quality checks](quality.md) | Static checks, dependency audit and their scope |

Shared workflow details: [pending patches](pending-records.md),
[temporal plans and repair](temporal-plans.md), [data coverage](data-inventory.md),
[saved-record changes](saved-record-edits.md), [provisional categories](provisional-categories.md),
[historical category review](historical-categories.md) and
[previous expense amounts](previous-expense-prices.md).

Planning and evaluation: [prompt design](planning-prompts.md),
[semantic boundaries](semantic-boundaries.md),
[current intent evaluation](../benchmarks/intent-v2/README.md) and
[separate inference validation](inference-validation.md).

Dated benchmark inventories and release-pinned schema references retain their
original scope; they are not a claim that every later feature is live.
