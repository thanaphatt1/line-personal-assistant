# LINE Personal Assistant

A Thai, single-owner LINE assistant for expense tracking and Google Calendar drafts. It uses typed model tools to interpret requests; the server validates facts and performs writes only after the owner confirms a delivered review.

## Features

- **Expenses:** prepare and correct multiple THB expenses, resolve catalog names, request missing information, and confirm a group once. New catalog entries require the owner's category choices.
- **Summaries:** read spending over bounded date ranges in Asia/Bangkok without creating a write draft.
- **Calendar:** prepare timed or single-date all-day events, including mixed groups. When a date has no time, ask “นัดนี้ทั้งวัน หรือเริ่มกี่โมงครับ?” Answers continue the same draft.
- **Safe confirmation:** text and buttons refer to a delivered version and nonce. Edits invalidate old consent; repeated webhooks reconcile durable action IDs instead of blindly repeating a write.
- **Conversation continuity:** persist raw messages separately from compact state, keep recent context and verified rolling summaries, and preserve pending or uncertain effects across restarts.
- **Optional reply wording:** a second model phase arranges immutable facts into a Thai reply. A bounded fallback returns the server's factual message if that phase fails or is slow.

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

The checked-in suite contains synthetic fixtures, denies external sockets and exercises signed ingress, persistence, consent, expense behavior, Calendar groups, all-day events and response fallback. Packaging tests also start the exact source archive in an isolated subprocess. No paid API access is needed. Historical private model traces and release-operation evidence are not part of this repository.

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
