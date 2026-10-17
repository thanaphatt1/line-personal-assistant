# Development

## Environment

Use Python 3.11+ and the checked-in dependency lock. Editable installation is intended for local development; run commands from the repository root. Runtime modules are under `src/line_assistant`, tests under `tests`, container/build support under `deploy` and `scripts`.

```sh
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -p no:cacheprovider
```

The suite denies external sockets. Tests inject HTTP transports, synthetic owners, Sheets/Calendar services and model outputs. Files that create temporary databases or subprocess crash/restart workers use temporary directories. Do not replace these fixtures with production snapshots or recorded owner conversations.

Behavior regressions share test-only adapters in `tests/behavior_support.py` and `tests/language_support.py`. They construct proposals from the current synthetic event and owned draft references. The wire-contract tests preserve literal JSON argument strings; routing, provenance, edits, cancellation, cold restore and confirmation tests check business effects independently. Historical paid-response files and release approval records are not test dependencies or model-quality evidence.

## Continuous integration

The `CI` GitHub Actions workflow runs on pushes and pull requests using a GitHub-hosted Ubuntu 24.04 runner and Python 3.11. It runs the full offline suite, verifies a deterministic source bundle, builds and installs a local wheel without downloading dependencies, and starts both packages with synthetic integrations. Startup checks cover the local/durable mock, invalid signatures, and history-enabled OFF bridge/ON candidate configurations with zero provider calls.

Run the same checks locally from the repository root:

```sh
python -m pip install -r requirements-ci.txt
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -p no:cacheprovider
python scripts/check_ci.py
```

Dependency installation needs package-index access; the tests and package/startup checks afterward are offline. Official checkout/setup-python actions use full commit SHA pins, checkout credentials are not persisted, and the workflow has only `contents: read`. It does not reference repository secrets, request OIDC/cloud permissions, upload/download workflow artifacts, or use privileged PR triggers. One job has a 20-minute limit; superseded runs on the same branch/PR are cancelled. Existing fork-PR approval policies are unchanged.

CI results verify synthetic runtime and packaging contracts. They do not test a real LINE owner, establish model quality, build a cloud image or deploy. Branch protection and release approvals remain separate settings; this workflow does not change them.

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
