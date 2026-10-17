# LINE Personal Assistant

A low-budget personal LINE assistant for occasional daily tasks. It is designed for someone who uses an assistant too infrequently to justify a monthly subscription. Keeping running costs small is a design goal; actual API and hosting costs depend on usage.

[![A query enters the assistant framework, which exchanges tool requests and results with an expense sheet, category lookup and Calendar, then returns an output. Writes require confirmation.](docs/assets/assistant-overview.svg)](docs/system-flow.md)

The framework uses LLM interpretation with deterministic checks. It calls tools when needed, uses their results and asks for confirmation before a write. See the [detailed runtime diagrams and input/output map](docs/system-flow.md).

## Two main functions

1. **Log expenses.** Prepare and correct THB expense entries, resolve catalog names, fill missing information and save after reviewing. New catalog entries require the owner's category choices.
2. **Create Calendar appointments.** Prepare timed or single-date all-day appointments, including groups. Review the date, timing and conflicts, then confirm creation.

Read-only expense and Calendar queries support these two workflows. In this source revision, the LLM interprets questions and follow-ups and selects bounded read tools; code validates arguments, reads complete data and calculates totals. Queries create no write draft. This revision requires a separate deployment to change a running service. See [query examples and limits](docs/read-queries.md). Publishing a later source change does not deploy it automatically.

Development also includes [saved expense corrections and appointment rescheduling](docs/saved-record-edits.md), disabled by default. These select stable saved records, show before/after values and require a new owner confirmation. Calendar uses conditional ETag updates; expense edits additionally require a reviewed policy controlling other Sheets writers.

Optional [provisional category suggestions](docs/provisional-categories.md) separately show confirmed, proposed and ambiguous labels. Query-only inclusion and permanent mapping require different confirmations; proposed amounts never silently enter confirmed totals. Both suggestion and mapping flags default OFF.

## How it works

- **Safe confirmation:** text and buttons refer to a delivered version and nonce. Edits invalidate old consent; repeated webhooks reconcile durable action IDs instead of blindly repeating a write.
- **Conversation continuity:** persist raw messages separately from compact state, keep recent context and verified rolling summaries, and preserve pending or uncertain effects across restarts.
- **Optional reply wording:** when enabled and a call slot remains, a model phase composes concise natural Thai around structured facts. Query dates, amounts, counts and units stay grounded in the verified result. A bounded fallback returns the server's factual message if that phase fails or is slow.

The production Calendar path creates events in one explicitly configured secondary calendar. It does not edit/delete saved events, invite guests, create recurring events or reminders, or interpret multi-day all-day ranges as a single date. Income and recorded-expense edits are unsupported. Conflict checks cover only the configured calendar. See [behavior and examples](docs/behavior.md).

## Run the local mock

Python 3.11 or newer is required. Run from the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m uvicorn line_assistant.main:app --host 127.0.0.1 --port 8000
```

`GET /health` reports mock mode. This local entry point uses deterministic mock integrations, has no real credentials and does not call a model. The signed, history-enabled production factory is a separate opt-in described in [deployment](docs/deployment.md). The `.env.example` file documents local configuration; files are never loaded automatically.

## Test

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -p no:cacheprovider
```

The checked-in suite contains synthetic fixtures, denies external sockets and exercises signed ingress, persistence, consent, expense behavior, Calendar groups, all-day events and response fallback. Packaging tests also start the exact source archive in an isolated subprocess. No paid API access is needed. The [role benchmark protocol](benchmarks/PROTOCOL.md) separates development cases from frozen held-out evaluation and keeps the optional critic prototype in development. See the [Luna baseline scorecard](benchmarks/v1/results/README.md); human style ratings remain pending. The [inference validation protocol](docs/inference-validation.md) explains the separate synthetic real-model corpus and exact effect checks. Historical private model traces and release-operation evidence are not part of this repository.

## Documentation

| Guide | Purpose |
| --- | --- |
| [Architecture](docs/architecture.md) | Request flow, module boundaries and persisted contracts |
| [Behavior](docs/behavior.md) | User examples and supported limits |
| [Configuration](docs/configuration.md) | Explicit targets, feature flags and secret mounts |
| [Development](docs/development.md) | Tests, source packaging and change review |
| [Deployment](docs/deployment.md) | Container startup, staging and compatible rollback |
| [Security](SECURITY.md) | Data handling and private vulnerability reports |

## License

A license has not been selected. No license file is currently provided.
