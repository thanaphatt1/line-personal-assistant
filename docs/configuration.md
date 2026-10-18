# Configuration

There are two separate entry points. `line_assistant.main:app` is the local deterministic mock. `line_assistant.history_cloud_run:app_factory` is the explicit production composition. Loading a mock profile never starts live integrations.

## Local mock

`.env.example` contains only generic nonsecret local settings. The application does not discover `.env`, ADC, shell profiles or arbitrary configuration files. The configuration helpers accept an explicit mapping or explicitly selected file. Do not put live keys into shell command arguments or commit local configuration.

## Production targets

Use `deploy/runtime.env.example` as a checklist for environment values configured by the deployment operator. It remains disabled by default and cannot launch unchanged. Configure all resource identifiers for your own environment:

| Key | Meaning |
| --- | --- |
| `PILOT_PROJECT_ID`, `PILOT_REGION` | Project and Cloud Run region |
| `PILOT_SERVICE_ACCOUNT` | Attached runtime account in that project; metadata identity must match |
| `PILOT_LINE_OWNER_ID` | One LINE user ID; groups and other owners are ignored |
| `PILOT_SPREADSHEET_ID` | Exact expense workbook |
| `PILOT_EXPENSE_SHEET_ID`, `PILOT_DATABASE_SHEET_ID` | Distinct numeric tab IDs |
| `PILOT_SHEETS_MAX_ROWS` | Full bounded grid/ID uniqueness scan, 2–1001 rows |
| `PILOT_SHEET_SCHEMA_VERIFIED` | Explicit operator assertion after the schema/access review below |
| `PILOT_LEGACY_SPREADSHEET_ID` | Optional exact prior workbook allowed for compatible state recovery |
| `PILOT_CALENDAR_ID` | A secondary `…@group.calendar.google.com` calendar; never `primary` |

Synthetic targets in code exist for offline tests. A real transport refuses those defaults. A supplied identity/target is bound through metadata, state, adapter and HTTP allowlists; changing a model argument does not change a resource target.

## Sheet schema

The required tabs are `All Expenses` and `Database`. Expense input columns A–D are date, item, cost and remark. Column E is a hidden transaction-ID metadata column used to reconcile uncertain writes. The Database catalog uses A–C for item/category/subcategory. Review the actual grid size, headers, hidden ID column, protections and allowed input cells before asserting the schema flag. Formula columns are not overwritten. Do not point this application at an unreviewed personal workbook.

## Feature and usage flags

| Key | Default | Requirement |
| --- | --- | --- |
| `PILOT_LAUNCH_AUTHORIZED` | `false` | Separate explicit launch decision |
| `PILOT_CONVERSATION_ENABLED` | `false` | Production mode |
| `PILOT_HISTORY_STATE_ENABLED` | `false` | Authorized production conversation |
| `PILOT_HISTORY_ENABLED` | `false` | History state enabled |
| `PILOT_CALENDAR_ENABLED` | `false` | History/state and exact secondary calendar |
| `PILOT_RESPONSE_REALIZATION_ENABLED` | `false` | History/state |
| `PILOT_MODEL_USAGE_GATES_ENABLED` | `true` | `false` requires explicit history state; counting remains |
| `PILOT_PROVISIONAL_CATEGORIES_ENABLED` | `false` | History/state; proposals do not grant mapping consent |
| `PILOT_PROVISIONAL_MAPPING_ENABLED` | `false` | Proposals and reviewed Database writer policy |
| `PILOT_HISTORICAL_CATEGORIES_ENABLED` | `false` | Provisional categories/history; optional complete-grid historical review |

Historical permanent saves also require the separate mapping flag and
`PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY=reviewed_single_writer`. The process lease
does not control human or other API writers. See [historical category review](historical-categories.md)
for page scope, stale checks and provisional versus permanent consent.

Configure `PILOT_MODEL_MONTHLY_MICRO_USD`, `PILOT_MODEL_MAX_CALLS_DAY` and `PILOT_MODEL_MAX_CALLS_MONTH` deliberately. Monetary values are integer micro-USD. The supported model is fixed by the typed adapter; `PILOT_MODEL_ID` must match `model_api.MODEL_ID`. The retained compatibility layer accepts up to 100 calls/day, 300/month and 500000 micro-USD configured allocation in production. These application controls are not a provider billing hard limit.

## Model access

The default and only accepted model ID is `gpt-6-luna`, used through the Responses API. `PILOT_MODEL_ID` asserts this fixed adapter contract; it is not an arbitrary model selector. Other IDs are rejected, and there is no automatic model fallback.

The [official GPT-6 Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna) documents the model and lists the Free API tier as unsupported. Live use requires an appropriately enabled API account and credentials; this repository does not provision billing/model access or verify another user's account entitlement. The local mock and offline tests need no API key. Changing the model requires reviewing the typed request/response contract, token reservations, prompts and regressions.

## Secrets

Provide three independently mounted, version-pinned secret files to the runtime:

- `/var/run/line-assistant-secrets/line-channel-secret/value`
- `/var/run/line-assistant-secrets/line-channel-access-token/value`
- `/var/run/line-assistant-secrets/openai-api-key/value`

Secret values never belong in source, env examples, manifests, argv, test fixtures or diagnostics. The container uses its attached service account for scoped Google access. Grant only the reviewed permissions and resource access for the chosen workbook/calendar/state; this repository contains no IAM-changing automation.
