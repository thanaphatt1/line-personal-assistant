# Capability and data-scope awareness

Every raw-history model request includes `available_tools`, derived directly from that request's actual tool definitions. It also includes `data_sources` and current-request `data_source_observations`. Read-analysis requests omit all write/proposal tools; the names in `available_tools` reflect that restricted set. User text, stored labels and old assistant claims cannot add tools or provider targets.

These describe separate boundaries:

| Metadata | Meaning |
|---|---|
| Available tools and schemas | Operations the application can actually execute now, including argument limits |
| Configured source scope | The selected expense dataset and server-selected configured/shared calendars; actual provider access must pass a read |
| Observed data coverage | Dates, years and counts verified from that source at a stated time; unknown values remain unknown |

The read-only `inspect_data_sources` tool accepts `source=capabilities/expense/calendar/all`, current owner references and `continue_analysis`. It cannot select a workbook, database, arbitrary URL, SQL or a different owner. False returns the code-rendered inventory answer; true returns compact metadata and permits subsequent read-only planning. A plain model answer after inspection cannot replace verified inventory facts with invented dates or permissions. Inspection creates no write action, draft, nonce or consent. Query windows remain367dates for expenses and31dates for Calendar; the24hour dialogue window is independent of stored-record dates.

Expense inspection uses the configured store's existing complete bounded classification read, for both Sheets and Firestore. It returns the record count, first/latest record dates and actual per-year counts. No item names, amounts, notes, transaction IDs or provider coordinates enter the metadata result. Current adapters still scan their bounded dataset internally; this is compact model-facing metadata, not a new indexed metadata store or unrestricted large-database scan. A future adapter can supply indexed/maintained aggregates with its own verified completeness and freshness contract.

`first_record_date` and `last_record_date` describe observed rows. They do not establish that every real-world expense between those dates was imported, that every intervening year has rows, or that an unobserved year is an unsupported query. A complete empty dataset is distinct from a schema/access/capacity failure: the former reports verified absence of saved records; the latter returns unavailable coverage without zero counts or an empty-year claim. Missing source records do not prove zero real-world spending.

Calendar inspection discovers the selected source count through the existing bounded discovery path. Configured IDs and shared candidates are not proof of successful event access, and this is not automatic access to all calendars in the owner's email. Provider access is checked when reading events. The inventory includes the current request's actually queried windows, their literal criteria and completeness. A filtered complete window does not prove unfiltered/global coverage. The global first/last event range remains explicitly unknown; recurring series may have no final occurrence. Inspection performs no unbounded event-history scan.

Observations carry `checked_at` and are cached only within the current owner request. A new turn inspects fresh data when needed; external edits are not assumed to be invalidated by this application. Persisted conversation receipts can show historical observations, whose timestamps remain historical and require rechecking before new absence claims. There is no background synchronization or permanent metadata migration.

Examples include `มีข้อมูลรายจ่ายของปีไหนบ้าง`, `ข้อมูลรายจ่ายเริ่มตั้งแต่วันไหน`, `ตอนนี้ทำอะไรได้และเข้าถึงข้อมูลอะไรบ้าง` and `ปฏิทินมีข้อมูลตั้งแต่วันไหนถึงวันไหน`. The last question receives an honest unknown-global-extent answer plus any verified current-request windows. When an operation is unsupported, the planner must state that limit instead of promising it; when only the per-query window is too large, it can plan several supported reads within the existing deadline and accounting constraints.

Offline tests use signed synthetic webhooks and scripted model choices. They prove metadata boundaries, both-backend contracts, freshness, replay, unavailable/empty distinction and read-only continuation; they do not establish real model planning accuracy. Source publication, paid inference evaluation and production deployment remain separate operations.
