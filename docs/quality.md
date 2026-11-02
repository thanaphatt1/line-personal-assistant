# Static checks and dependency advisories

The quality CI job checks Python defects, an initial typed contract subset and
known dependency advisories. Its dependency installation/advisory lookups use
public PyPI; its lint and type checks are offline. It has the same pinned
checkout/setup-python actions and `contents: read` permission as the existing
jobs. No model, business provider, cloud access or deployment is involved.

Install the pinned CI environment before running checks:

```sh
python -m pip install -r requirements-ci.txt
python -m ruff check --no-cache
python -m mypy
python -m pip_audit --disable-pip --no-deps --strict --timeout 10 \
  --progress-spinner off -r requirements.lock -r requirements-quality.lock -r requirements-ci.txt
```

`requirements.lock` retains runtime/test pins, `requirements-quality.lock` pins
the quality tools and their additional dependencies, and `requirements-ci.txt`
combines both with the build backend. Shared dependencies have one version;
`pip check` should pass. The build source does not include the quality lock or
install development extras. Source tests, lint and startup checks need no paid
inference. Use an isolated environment when verifying dependency updates.

## Lint scope

Ruff 0.17.3 checks Python under `src`, `scripts` and `tests`. The initial rule
set is `E9`, `F63`, `F7`, `F82`: syntax, undefined names and selected control-flow
or invalid-expression defects. It intentionally does not impose wholesale
formatting, unused-import cleanup or a broader style migration. Configuration
lives in `pyproject.toml`; no existing defects are globally ignored or baselined.
Add further rules incrementally when their findings have been reviewed.

This gate found a real undefined `correction` name in legacy item enrollment.
A hypothetical owner message could reach that branch instead of returning a
clarification. The branch now returns `owner_no_write`, leaves the draft intact
and performs no business write; natural-language regression cases cover it.
Current raw-history conditional enrollment keeps its existing model/evidence
workflow.

## Type scope

Mypy 2.4.1 runs strict checks on `planner_failures.py` and `sheet_dates.py`.
`PlannerFailure` requires a machine `kind` and permits an integer `http_status`;
message construction and date/serial conversion have explicit types. Imported
modules are skipped in this initial scope. This is not a claim that the whole
assistant, dynamic host/context builder or business-state schemas are typed.
Expand the file list with reviewed annotations and runtime contract tests;
do not hide new errors with repository-wide ignores.

## Advisory scope and coverage

Pip-audit 2.10.1 queries advisories for exact pinned versions without invoking a
pip resolver or installing the audited packages. In the verified no-resolver
mode, nested `-r` includes did not populate the result; auditing only
`requirements-ci.txt` reported its two direct pins. CI therefore names **all
three lock sources explicitly**. The complete checked set contains 60 unique
name/version pairs. When changing includes or splitting locks, update the audit
command and compare its JSON package set to every pinned source; missing or
skipped packages are unknown coverage, not a clean result.

`--strict` and the CI command fail on collection errors and reported findings;
there is no ignore list, automatic `--fix`, or success fallback when the
advisory service is unavailable. Lack of a known advisory does not establish
that a package is free of defects or vulnerabilities. Advisory results change
with the database, so retain the dated local report outside public source when
reviewing updates.

The initial complete scan led to these targeted pin changes:

| Package | Previous | Reviewed pin | Advisory reference |
|---|---|---|---|
| setuptools (CI backend) | 84.0.0 | 84.1.0 | [legacy Windows wheel path handling](https://github.com/advisories/GHSA-xmj3-9gf7-h52w) |
| certifi | 2026.7.22 | 2026.10.23 | [root certificate bundle](https://github.com/advisories/GHSA-6q48-6c7w-9gw2) |
| pyasn1 | 0.6.4 | 0.6.5 | [ASN.1 decoder resource consumption](https://github.com/advisories/GHSA-2qm8-x737-gmw5) |
| pytest | 8.4.2 | 9.0.3 | [temporary-directory handling](https://github.com/advisories/GHSA-6w46-j5rx-g56g) |

Pytest's major-version change also updates the development extra. Full offline
regressions and source/wheel/signed startup must run against the new pins before
accepting the update. These are source/development checks; existing production
images only change through the separate authorized release procedure.

Configuration references: [Ruff](https://docs.astral.sh/ruff/configuration/),
[incremental mypy](https://mypy.readthedocs.io/en/stable/existing_code.html),
[pip-audit](https://github.com/pypa/pip-audit#usage).
