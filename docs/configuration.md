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
| `PILOT_CALENDAR_READ_IDS` | JSON list of additional shared calendar IDs for read-only summaries; default `[]` |

Synthetic targets in code exist for offline tests. A real transport refuses those defaults. A supplied identity/target is bound through metadata, state, adapter and HTTP allowlists; changing a model argument does not change a resource target.

## Reading all calendars

Set `PILOT_CALENDAR_READ_ALL_ENABLED=true` alongside the existing Calendar/history
flags to combine saved appointments from the runtime account's Google Calendar
list and `PILOT_CALENDAR_ID`. For example, “สัปดาห์นี้มีนัดอะไรบ้าง” reads the
whole Monday–Sunday week in Bangkok time, including appointments earlier in
the week. Next/upcoming questions exclude appointments that have ended.

Discovery includes hidden and unselected calendars with event-reading access.
Free/busy-only and deleted calendars are excluded. This uses the attached
runtime account's list, not the LINE owner's personal Google account. Each
desired calendar must already be accessible and present on that runtime
account's calendar list. Sharing alone does not necessarily add it to the list;
this feature does not create subscriptions or change access.

For a calendar shared with the bot but absent from its list, set
`PILOT_CALENDAR_READ_IDS=["shared-reader@example.invalid"]` in private deployment
configuration, using the actual Calendar ID from its settings. Up to 15 unique
additional IDs are allowed; omit `PILOT_CALENDAR_ID` from this list. These
calendars are included directly, without registering a calendar-list
subscription. This works with `PILOT_CALENDAR_READ_ALL_ENABLED=false` using the
existing event-read surface, or alongside discovery when that flag is `true`.
The combined selection remains limited to 16 calendars. Real IDs belong outside
the public checkout and are never model-selected tool arguments.

Discovery and event GET requests use the read-only `calendar.readonly` scope.
Additional event routes are admitted only after complete provider discovery
under the active owner lease. Creation, conflict checks and confirmed
rescheduling remain bound to `PILOT_CALENDAR_ID`; discovering a calendar grants
no write route. With discovery disabled and an empty additional-ID list, reads
retain the existing single-calendar behavior.

The reader supports up to 16 calendars including the configured calendar,
three discovery pages and eight 20-event pages per calendar for a31-date
window, within the existing request budget and deadline. If discovery, access,
pagination or validation fails, the assistant reports that it cannot complete
the summary instead of returning a partial count or claiming there are no
appointments. Shared invitation copies with the same iCalUID and occurrence
boundaries count once; separate recurring occurrences remain separate.

Calendar subject/type analysis also requires
`PILOT_RESPONSE_REALIZATION_ENABLED=true`. It uses the existing second model
request and usage reservation to classify at most120 deduplicated fetched
events, with an eight-second foreground deadline and a48,000-byte request
limit. The ordinary reply deadline remains four seconds. Model-selected
event references are validated and counted by code. Ambiguous membership is
shown separately, and unavailable inference cannot produce a guessed count.
See [semantic query limits](read-queries.md).

See Google's [CalendarList API](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/list),
[sharing rules](https://developers.google.com/workspace/calendar/api/concepts/sharing)
and [authorization scopes](https://developers.google.com/workspace/calendar/api/auth).
Enabling the flag in production is a separate release/configuration action.

## Connect a calendar through LINE

Set `PILOT_CALENDAR_CONNECTIONS_ENABLED=true` with Calendar/history enabled to
let the owner connect shared calendars in LINE. This works without calendar-list
discovery or an operator-supplied additional-ID list:

1. Owner: “เพิ่มปฏิทินใหม่” / “I want to add new calendar”.
2. The model calls `connect_calendar` with `operation=start`. The server replies
   with the configured runtime service-account email, asks the owner to share
   with “See all event details”, and requests the Calendar ID or embed link.
3. After the owner sends the ID, the model calls `operation=add` using the current
   owner's literal ID. The server verifies the `events.list` access role before
   saving the read source. A failed check keeps setup open for another attempt.
4. Future saved-appointment questions automatically include that calendar.

The bot remembers connections in its owner/workspace-bound state. Setup expires
after 24 hours; saved connections do not expire with conversation history. The
prompt must have been delivered before an ID can finish setup. Duplicate adds
are idempotent; cancelling setup preserves existing connections.

The access probe requests only `kind` and `accessRole` with `fields`; it does not
return appointment contents to the model. It uses the existing event scope and
requires a full event-reading role, not free/busy-only access. The source is
registered in assistant state, so Google `CalendarList.insert`, a new OAuth
scope, sharing/ACL changes and calendar write access are unnecessary. Calendar
creation and rescheduling still target `PILOT_CALENDAR_ID`.

The default is disabled. A same-image OFF bridge preserves the registry but
does not read those connected calendars or expose the setup tool. Do not assume
an older image preserves the new registry when saving state. Deployment and
production enablement remain separate actions.

## Sheet schema

The required tabs are `All Expenses` and `Database`. Expense input columns A–D are date, item, cost and remark. Column E is a hidden transaction-ID metadata column used to reconcile uncertain writes. The Database catalog uses A–C for item/category/subcategory. Review the actual grid size, headers, hidden ID column, protections and allowed input cells before asserting the schema flag. Formula columns are not overwritten. Do not point this application at an unreviewed personal workbook.

See the [database and field guide](database-schema.md) for exact headers, types,
required versus generated fields, catalog relationships and Firestore storage.

## Feature and usage flags

| Key | Default | Requirement |
| --- | --- | --- |
| `PILOT_LAUNCH_AUTHORIZED` | `false` | Separate explicit launch decision |
| `PILOT_CONVERSATION_ENABLED` | `false` | Production mode |
| `PILOT_HISTORY_STATE_ENABLED` | `false` | Authorized production conversation |
| `PILOT_HISTORY_ENABLED` | `false` | History state enabled |
| `PILOT_CALENDAR_ENABLED` | `false` | History/state and exact secondary calendar |
| `PILOT_CALENDAR_READ_ALL_ENABLED` | `false` | Calendar/history enabled; read all event-readable calendars on the runtime account's list |
| `PILOT_CALENDAR_CONNECTIONS_ENABLED` | `false` | Calendar/history enabled; owner can connect shared read sources through LINE |
| `PILOT_RESPONSE_REALIZATION_ENABLED` | `false` | History/state |
| `PILOT_MODEL_USAGE_GATES_ENABLED` | `true` | `false` requires explicit history state; counting remains |
| `PILOT_PROVISIONAL_CATEGORIES_ENABLED` | `false` | History/state; proposals do not grant mapping consent |
| `PILOT_PROVISIONAL_MAPPING_ENABLED` | `false` | Proposals and reviewed Database writer policy |
| `PILOT_HISTORICAL_CATEGORIES_ENABLED` | `false` | Provisional categories/history; optional complete-grid historical review |
| `PILOT_PREVIOUS_EXPENSE_PRICES_ENABLED` | `false` | History/state; optional owner-selected amount from exact saved history |
| `PILOT_TABLE_REGISTRY_JSON` | empty | History/state; JSON schemas and explicit existing storage bindings; see [tables](tables.md) |

Historical permanent saves also require the separate mapping flag and
`PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY=reviewed_single_writer`. The process lease
does not control human or other API writers. See [historical category review](historical-categories.md)
for page scope, stale checks and provisional versus permanent consent.

Previous-amount suggestions require no new Sheet schema. They show a saved
transaction total and its date, then require an owner choice and ordinary final
confirmation. See [previous expense amounts](previous-expense-prices.md) for
source exclusions, current-date handling, stale checks and same-image OFF recovery.

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
