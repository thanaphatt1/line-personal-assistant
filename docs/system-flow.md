# Runtime flow: inputs, decisions and outputs

This page describes the signed, history-enabled application. **Deployment status:** these diagrams describe this source revision; its LLM-selected query changes require a separate deployment. Publishing a later source change or passing CI does not deploy it automatically. The local mock and older compatibility entry points are separate compositions.

**Legend:** blue **CODE** = deterministic Python; purple **LLM** = a conditional request to the configured model; green **EXT** = LINE, Google or storage. Arrow labels name the data crossing a boundary. Each diagram is one view of the same application, not a sequence of mandatory model calls.

## 1. Shared ingress, history and reply

```mermaid
flowchart TD
    Line["EXT: LINE webhook<br/>Input: event ID, timestamp, owner text or button"]:::ext
    Gate["CODE: verify signature and owner<br/>Output: authenticated owner turn"]:::code
    Load["CODE: acquire owner lease and restore state<br/>Output: receipt replay or current work"]:::code
    Store["EXT: Firestore<br/>Raw speaker records, state, actions and receipts"]:::ext
    Context["CODE: load both speakers within 24 hours<br/>Output: original sources, timestamps and pending work"]:::code
    Summary["LLM: summarize older context only if needed<br/>Output: summary with source references"]:::llm
    Check["CODE: verify summary references<br/>Keep originals; stop if context still cannot fit"]:::code
    Route["CODE: admit verified direct control or interpretation<br/>No semantic query keyword pre-router"]:::code
    Interpret["LLM: interpret language and context<br/>Output: typed read or draft proposal, or clarification"]:::llm
    Core["CODE: validate and execute bounded tool<br/>Output: structured result or reviewed draft"]:::code
    Voice["LLM: optional reply arrangement<br/>Input: immutable facts and bounded relevant context<br/>Output: required fact slots with allowed natural phrases"]:::llm
    Reply["CODE: validate wording or use factual fallback<br/>Output: factual text or review with owner controls"]:::code
    Delivery["EXT: LINE reply API<br/>Output: delivery acknowledgement"]:::ext
    Line --> Gate --> Load
    Load <-->|"Owned records and checkpoints"| Store
    Load -->|"New event"| Context
    Load -->|"Duplicate event: saved response"| Reply
    Context -->|"Context fits; route below"| Route
    Context -.->|"Interpretation context exceeds bound"| Summary
    Summary --> Check
    Check -->|"Verified and within context bound"| Route
    Route -->|"Verified direct control result"| Reply
    Route -->|"Other owner text"| Interpret
    Interpret -->|"Typed arguments"| Core
    Core -->|"Authoritative result"| Reply
    Core -.->|"Enabled and call slot remains"| Voice
    Route -.->|"Enabled and call budget remains"| Voice
    Voice -->|"Candidate wording"| Reply
    Reply -->|"Only if reply is not already delivered"| Delivery
    Delivery -->|"Receipt and verified delivery binding"| Store
    classDef code fill:#E8F0FE,stroke:#1A73E8,color:#111;
    classDef llm fill:#F3E8FD,stroke:#8430CE,color:#111;
    classDef ext fill:#E6F4EA,stroke:#188038,color:#111;
```

History includes original owner messages and delivered assistant replies. A rolling 24-hour context window does not delete stored originals. The interpretation request carries owner source references, message timestamps, draft views and date references. Verified older-context summaries are conditional; the system stops rather than silently clipping required context.

All LLM roles use the configured model, with **at most two provider calls per owner turn in total**. A verified direct control can use zero interpretation calls. Stored-data queries use one interpretation call and, when enabled with a slot remaining, one grounded wording call. A proposal may need another tool-result step. General conversation can return plain interpretation text without any business tool. Optional wording runs only when enabled and a call slot remains; a failed, rejected or slow response uses the code-produced message. Wording receives admitted facts and short relevant snippets, not the full interpretation history, and cannot call business tools.

## 2. Expense write: proposal, review, then effect

```mermaid
flowchart TD
    Input["CODE: authenticated text and owned history<br/>Input: names, amounts, date wording and source refs"]:::code
    Model["LLM: interpret expense request<br/>Output: typed draft proposal or read-tool request"]:::llm
    Catalog["EXT: Sheets GET<br/>Output: current Database names and category mappings"]:::ext
    Validate["CODE: certify sources, Decimal amounts and date policy<br/>Output: missing fields, catalog choices or validated draft"]:::code
    Review["CODE: freeze exact payload, version and nonce<br/>Output: owner-visible expense review"]:::code
    Owner["EXT: owner receives review and confirms in a new LINE turn<br/>Input: text or bound button"]:::ext
    Consent["CODE: verify delivered review, ownership and unchanged payload<br/>Output: authorized action or clarification"]:::code
    Intent["CODE: persist action before external HTTP<br/>Output: stable action key and frozen entries"]:::code
    Sheet["EXT: Sheets append on configured target<br/>Input: confirmed entries, IDs and optional approved catalog enrollment<br/>Output: acknowledgement or unknown outcome"]:::ext
    Recover["CODE: retain receipt; reconcile unknown effect by positive proof<br/>Output: recorded or uncertain; no blind append retry"]:::code
    Result["CODE: core result to shared reply pipeline<br/>Output: admitted status and saved-entry facts"]:::code
    Input --> Model
    Model -->|"Tool arguments are proposals"| Validate
    Validate <-->|"Bounded catalog reads"| Catalog
    Validate -->|"Need facts or explicit category choices"| Result
    Validate -->|"Complete and valid"| Review
    Review --> Owner --> Consent
    Consent -->|"Accepted"| Intent --> Sheet --> Recover --> Result
    Consent -->|"Stale, missing or changed review"| Result
    classDef code fill:#E8F0FE,stroke:#1A73E8,color:#111;
    classDef llm fill:#F3E8FD,stroke:#8430CE,color:#111;
    classDef ext fill:#E6F4EA,stroke:#188038,color:#111;
```

For a new expense with no date, **code uses Bangkok today** from the original owner message timestamp. Unchanged edits retain the certified date; ambiguous or conflicting dates require clarification. The server resolves existing catalog labels; a new item requires the owner's category/subcategory choices before a review. An LLM tool call never grants write permission. Income has no verified storage mapping and is unsupported; it is not a known zero balance.

Confirmation is another authenticated turn through the shared ingress. Named buttons/text bind the frozen review; a short contextual acknowledgement must refer to the appropriate delivered review. Queries create no consent. After an action starts, durable keys and receipts prevent duplicate webhook effects, and uncertain outcomes preserve the original intent until reconciliation proves what happened.

## 3. Calendar write: explicit timing and one configured calendar

```mermaid
flowchart TD
    Input["CODE: owner text, original timestamps and draft context"]:::code
    Model["LLM: propose one event or a group<br/>Output: literal title, date and timing fields with source refs"]:::llm
    Validate["CODE: validate sources and Bangkok date or clock values<br/>Output: missing facts or normalized event payloads"]:::code
    Missing["CODE: targeted question<br/>Output: missing date, all-day versus start time, or end or duration"]:::code
    Busy["EXT: Calendar GET on configured calendar<br/>Output: bounded overlap information"]:::ext
    Review["CODE: build delivered review with version and nonce<br/>Output: dates, timing, conflicts and owner controls"]:::code
    Owner["EXT: owner confirms in a new signed LINE turn"]:::ext
    Consent["CODE: validate consent; persist stable action before HTTP<br/>Output: frozen authorized create payload"]:::code
    Calendar["EXT: Calendar event creation<br/>Input: fixed calendar, stable event ID and payload<br/>Output: proven acknowledgement or unknown outcome"]:::ext
    Recover["CODE: receipt or positive GET reconciliation<br/>Output: created, partial or uncertain; preserve pending effects"]:::code
    Result["CODE: core result to shared reply pipeline"]:::code
    Input --> Model --> Validate
    Validate -->|"Incomplete or ambiguous"| Missing --> Result
    Validate -->|"Complete"| Busy --> Review --> Owner --> Consent
    Consent -->|"Valid current review"| Calendar --> Recover --> Result
    Consent -->|"No valid consent"| Result
    classDef code fill:#E8F0FE,stroke:#1A73E8,color:#111;
    classDef llm fill:#F3E8FD,stroke:#8430CE,color:#111;
    classDef ext fill:#E6F4EA,stroke:#188038,color:#111;
```

Calendar creation does not invent a missing clock or duration. A dated request without timing asks all-day versus start time; an all-day request never needs clock fields. Single-date all-day creation uses `start.date` and the **exclusive next-day** `end.date`. Timed payloads carry explicit offsets. Group reviews retain each event's facts, and partial/uncertain writes retain per-event recovery state. Draft cancellation does not delete saved events. A terse dated title can ask add-versus-read intent before any draft proposal.

## 4. Stored-data queries: LLM chooses scope, code reads and calculates

```mermaid
flowchart TD
    Input["CODE: authenticated owner context<br/>Text, raw history, Bangkok timestamp, catalog and delivered query scope"]:::code
    Model["LLM: interpret request and follow-up<br/>Choose query_expenses or query_calendar with typed arguments"]:::llm
    Scope["CODE: validate dates, exact filter and source references<br/>No language parsing; fixed workbook and calendar"]:::code
    Expense["EXT: Sheets GET<br/>Output: bounded input rows and current catalog mapping"]:::ext
    Money["CODE: validate complete grid and rows; filter and sum Decimal<br/>Output: expense total, count and saved entries"]:::code
    Calendar["EXT: Calendar GET with pagination<br/>Output: saved timed and all-day occurrences"]:::ext
    Events["CODE: validate pages and offsets; apply overlap bounds<br/>Output: events with correct exclusive all-day end display"]:::code
    Error["CODE: honest clarification or unavailable result<br/>Output: no fabricated zero from an incomplete read"]:::code
    Facts["CODE: bounded factual result and query-scope receipt<br/>Output: separate period, total, count and filter facts; verified rows to shared reply pipeline"]:::code
    Input --> Model
    Model -->|"Typed read arguments"| Scope
    Model -->|"Ambiguous intent or unsupported filter"| Error
    Scope -->|"Expense range and item or category"| Expense --> Money
    Scope -->|"Calendar day or week"| Calendar --> Events
    Scope -->|"Ambiguous, missing or unsupported scope"| Error
    Expense -->|"Unavailable or incomplete"| Error
    Calendar -->|"Unavailable or incomplete"| Error
    Money --> Facts
    Events --> Facts
    Error --> Facts
    classDef code fill:#E8F0FE,stroke:#1A73E8,color:#111;
    classDef llm fill:#F3E8FD,stroke:#8430CE,color:#111;
    classDef ext fill:#E6F4EA,stroke:#188038,color:#111;
```

There is no semantic keyword pre-router for queries. The LLM chooses typed calendar frames or explicit dates, filter and total/list mode from the current text and trusted context; it clarifies ambiguity. Code expands model-selected relative calendar frames, validates the typed range and exact catalog identity, performs complete reads and calculates every amount. The old competing `spending_summary` is absent from the raw planner tool set; compatibility applications retain their separate legacy contract. Expense queries use inclusive local dates and the **current** catalog mapping. Calendar queries cover at most seven dates and three pages of 20 events; unfinished pagination yields an unavailable response. Complete replies show at most 20 rows and disclose additional results. Saved all-day events can span several dates even though new all-day creation is single-date only.

A complete empty read can report zero saved records. It does not establish availability in another calendar, income or a remaining balance. Reads create no business write, action, draft or nonce; normal conversation receipts and usage records still persist. Provider reads are bounded but are not a transactionally frozen snapshot across external edits. See [query examples and limits](read-queries.md).

## Boundary inputs and outputs

| Boundary | Input | Output | Decides or performs |
| --- | --- | --- | --- |
| Signed ingress | LINE event, original timestamp, text/button | Verified owner turn or rejection | Code |
| History/context | Both speakers, pending state, original source refs | 24-hour context; verified older summary when needed | Code; conditional LLM summary |
| Interpretation | Current owner text, history, typed tool definitions | Proposed fields/tool call or text | LLM; never write consent |
| Validation/review | Proposed fields and trusted catalog/provider reads | Certified draft, exact review or clarification | Code |
| Confirmation/effect | Delivered review binding and fresh owner action | Stable durable action; provider receipt or uncertainty | Code and fixed external service |
| Stored-data read | Raw owner question/history and trusted current clock/catalog | LLM-selected typed scope, then calculated total/count/list or reason-coded error | LLM interpretation; code and GET-only provider path |
| Reply realization | Admitted facts, short relevant context, remaining call budget | Validated factual wording or original code fallback | Conditional LLM, then code |
| Persistence/delivery | Core result, action state, LINE acknowledgement | Durable receipt, delivery binding and retained originals | Code, Firestore and LINE |

## Source and prompt map

| Purpose | Source |
| --- | --- |
| Signed composition and flags | [`history_cloud_run.py`](../src/line_assistant/history_cloud_run.py), [`main.py`](../src/line_assistant/main.py) |
| Routing, tool loop, review and consent | [`history_first.py`](../src/line_assistant/history_first.py), [`draft_workflow.py`](../src/line_assistant/draft_workflow.py) |
| Interpretation and overflow-summary prompts | [`raw_history.py`](../src/line_assistant/raw_history.py): `RAW_INSTRUCTIONS`, `CALENDAR_INSTRUCTIONS`, `SUMMARY_INSTRUCTIONS`; raw sources remain authoritative |
| Expense evidence and date defaults | [`expense_sources.py`](../src/line_assistant/expense_sources.py), [`expense_evidence.py`](../src/line_assistant/expense_evidence.py), [`history_dates.py`](../src/line_assistant/history_dates.py) |
| Calendar normalization, groups and consent | [`calendar_dates.py`](../src/line_assistant/calendar_dates.py), [`calendar_workflow.py`](../src/line_assistant/calendar_workflow.py), [`calendar_batches.py`](../src/line_assistant/calendar_batches.py) |
| Typed read tools, validation, natural fallback and fixed adapters | [`read_queries.py`](../src/line_assistant/read_queries.py), [`sheets_api.py`](../src/line_assistant/sheets_api.py), [`calendar_provider.py`](../src/line_assistant/calendar_provider.py) |
| Wording prompt and factual fallback | [`response_realization.py`](../src/line_assistant/response_realization.py): `INSTRUCTIONS`, fact extraction, strict response schema and validation |
| Transport limits and recovery | [`pilot_http.py`](../src/line_assistant/pilot_http.py), [`calendar_transport.py`](../src/line_assistant/calendar_transport.py), [`firestore_state.py`](../src/line_assistant/firestore_state.py) |
| Raw history and compatible state envelopes | [`raw_history.py`](../src/line_assistant/raw_history.py), [`production_history_state.py`](../src/line_assistant/production_history_state.py), [`history_persistence.py`](../src/line_assistant/history_persistence.py) |

**Maintenance rule:** whenever a runtime route, model phase, provider effect or input/output contract changes, update the affected diagram, boundary table and module links in the same source change. Keep the README's main-function list and [overview SVG](assets/assistant-overview.svg) aligned with that flow, and check the image at normal README and mobile widths. Keep deployment-status statements aligned with the separately verified live release. Examples must stay synthetic; omit private identifiers, configuration, credentials and operation logs.
