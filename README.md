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

Optional [historical category review](docs/historical-categories.md) scans all unmapped names by default, with optional month/date scope and ten-name display pages over a complete input read. Owners can correct a batch, approve only some names or cancel, then separately choose a provisional summary or confirmed permanent mapping. Original transactions are preserved; the feature defaults OFF.

Optional [previous expense amounts](docs/previous-expense-prices.md) can ask whether to use an exact item's last reliable saved total when an amount is missing. The question shows the historical date; the new expense keeps its own date and still requires a final review and confirmation. Explicit amounts take precedence. The feature defaults OFF and creates no automatic recurring payments.

## How it works

- **Safe confirmation:** text and buttons refer to a delivered version and nonce. Edits invalidate old consent; repeated webhooks reconcile durable action IDs instead of blindly repeating a write.
- **Conversation continuity:** persist raw messages separately from compact state, keep recent context and verified rolling summaries, and preserve pending or uncertain effects across restarts.
- **Optional reply wording:** when enabled and a call slot remains, a model phase composes concise natural Thai around structured facts. Query dates, amounts, counts and units stay grounded in the verified result. A bounded fallback returns the server's factual message if that phase fails or is slow.

The baseline Calendar creation path uses one explicitly configured secondary calendar. It does not delete saved events, invite guests, create recurring events or reminders, or interpret multi-day all-day ranges as a single date. Income remains unsupported. Saved-record edits and rescheduling use the separate optional flags and reviewed workflows linked above. Conflict checks cover only the configured calendar. See [behavior and examples](docs/behavior.md).

## Technology stack

| Layer | Technology and role | Source |
| --- | --- | --- |
| Application | Python 3.11+, FastAPI and Uvicorn serve the webhook and health endpoint. Pydantic 2 validates typed data and configuration. | [Dependencies](pyproject.toml), [app](src/line_assistant/main.py) |
| LINE | The LINE Messaging API delivers messages/postbacks to the webhook and sends replies. The server checks the HMAC signature and configured owner before processing events. | [LINE adapter](src/line_assistant/line_api.py), [ingress](src/line_assistant/main.py) |
| Language model | OpenAI Responses API with `gpt-6-luna` interprets language, proposes typed tool calls and optionally words a Thai reply. Code validates evidence, dates, amounts, consent and effects; a model response cannot authorize a write. | [Model API](src/line_assistant/model_api.py), [runtime flow](docs/system-flow.md) |
| HTTP and identity | `httpx` provides bounded HTTP clients and injectable test transports. Adapters call provider REST APIs directly. Google access uses the configured account; `google-auth` also supports local preflight tooling. Secrets use separately configured, version-pinned mounts. | [HTTP boundary](src/line_assistant/pilot_http.py), [authentication](src/line_assistant/google_auth.py), [configuration](docs/configuration.md) |
| Business data | Google Sheets stores expenses and the item/category catalog. Google Calendar stores events on the configured calendar. | [Sheets adapter](src/line_assistant/sheets_api.py), [Calendar adapter](src/line_assistant/calendar_provider.py) |
| Durable state | Firestore stores owner leases, compact workflow state, receipts, action recovery and model accounting. Raw messages have a separate Firestore journal. In-memory and SQLite-backed adapters support local mocks and restart tests. | [Production state](src/line_assistant/production_history_state.py), [local persistence](src/line_assistant/persistence.py), [architecture](docs/architecture.md) |
| Hosting and build | A non-root Python container runs on Cloud Run. Cloud Build verifies a reviewed source archive from Cloud Storage, builds with pinned base/builder images and publishes to Artifact Registry. Cloud Logging supplies startup/error evidence. | [Production Dockerfile](deploy/history-production.Dockerfile), [build template](deploy/cloudbuild.json), [deployment](docs/deployment.md) |
| Tests and CI | pytest, FastAPI's test client, `httpx.MockTransport`, synthetic providers and temporary SQLite stores cover behavior and recovery. GitHub Actions runs offline tests plus source/wheel/startup checks on Ubuntu with Python 3.11. | [Test guide](tests/README.md), [CI workflow](.github/workflows/ci.yml) |

The history-enabled runtime has a maximum of two model calls per owner turn;
verified direct controls can use none. Reply realization, saved-record changes
and category workflows are optional features with explicit flags. The critic
prototype in the [benchmark work](benchmarks/PROTOCOL.md) is not a runtime phase.
Models used to write or review this repository are engineering choices, separate
from the bot's runtime model. See [configuration](docs/configuration.md) for the
actual switches and [behavior](docs/behavior.md) for supported operations.
The [database and field guide](docs/database-schema.md) explains the required
Sheet headers, stable transaction IDs, catalog relationships and internally
generated Firestore state/history fields.

The pinned installation snapshot is in [requirements.lock](requirements.lock);
[requirements-ci.txt](requirements-ci.txt) adds the wheel-build tools. A source
push runs CI and does not build a cloud image or deploy the service. Deployment
requires a separate reviewed artifact, configuration, budget and approval. This
keeps the occasional-use cost goal separate from any promise about a monthly bill.

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

The [test guide](tests/README.md) explains the test families, representative cases,
setup commands, mocks, count interpretation and how to add a regression. The
checked-in suite denies external sockets and needs no paid API access. Packaging
checks also start the exact source archive in an isolated subprocess.

The [role benchmark protocol](benchmarks/PROTOCOL.md) separates development cases
from frozen held-out evaluation. See the [Luna baseline scorecard](benchmarks/v1/results/README.md);
human style ratings remain pending. The [inference validation protocol](docs/inference-validation.md)
explains separate synthetic real-model validation. Private model traces and
release-operation evidence are not part of this repository.

## Documentation

| Guide | Purpose |
| --- | --- |
| [Architecture](docs/architecture.md) | Request flow, module boundaries and persisted contracts |
| [Behavior](docs/behavior.md) | User examples and supported limits |
| [Configuration](docs/configuration.md) | Explicit targets, feature flags and secret mounts |
| [Database and fields](docs/database-schema.md) | Sheet setup, field types, Firestore state and retention |
| [Development](docs/development.md) | Tests, source packaging and change review |
| [Test guide](tests/README.md) | Test families, examples, commands and evidence limits |
| [Deployment](docs/deployment.md) | Container startup, staging and compatible rollback |
| [Security](SECURITY.md) | Data handling and private vulnerability reports |

## License

A license has not been selected. No license file is currently provided.
