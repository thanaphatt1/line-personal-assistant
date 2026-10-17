# Development

## Environment

Use Python 3.11+ and the checked-in dependency lock. Editable installation is intended for local development; run commands from the repository root. Runtime modules are under `src/line_assistant`, tests under `tests`, container/build support under `deploy` and `scripts`.

```sh
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -p no:cacheprovider
```

The suite denies external sockets. Tests inject HTTP transports, synthetic owners, Sheets/Calendar services and model outputs. Files that create temporary databases or subprocess crash/restart workers use temporary directories. Do not replace these fixtures with production snapshots or recorded owner conversations.

## Source bundle

```sh
python scripts/prepare_calendar_source.py --output /tmp/assistant-source-review
python scripts/prepare_calendar_source.py --verify /tmp/assistant-source-review
```

Choose a new output directory. The packager reads only named inputs, rejects symlinks/hard links and produces a deterministic `source.tar.gz`, checksum list and source manifest. It excludes configuration, secrets, tests, docs, `.git` and runtime records. These outputs are local review artifacts, not permission to upload/build/deploy. `prepare_build_source.py` is the compatibility packager for the older factory; use the Calendar/history packager for the current application.

Packaging regressions verify checksum tampering, deterministic archives and exact imports in an isolated source directory. Signed startup checks use fake transport and make zero provider calls.

## Change boundaries

Keep business validation, consent, persistence and provider effects separate. Add meaningful regressions for changed behavior: incomplete facts, edit/confirm invalidation, cold restore, replay and uncertain acknowledgement are more useful than tests that mirror a helper's implementation.

Persisted protocol/state version strings and compatibility serializers must remain until a deliberate migration is designed and tested. Never clear history, reset the usage ledger or restore an older snapshot to make a test pass. Public documentation describes the architecture by purpose; private release diaries and paid-output reports belong outside the repository.

No deployment or paid-model workflow runs automatically from this repository. Review new automation before enabling it. License selection remains an owner decision.
