"""Offline request previews only. No credentials, network transport or live gate.

Targets/rows/ETags are adapter-owned inputs, never model tool arguments. These
builders cannot grant write access; the adapter owns allocation and reconciliation.
"""

from dataclasses import dataclass, field
import hashlib
import re
from urllib.parse import quote

from .adapters import BANGKOK, SheetLayout
from .contracts import AddTransactions, CreateEvent, UpdateEvent
from .sheet_metadata import TransactionIdMetadata, transaction_id


@dataclass(frozen=True)
class RequestPreview:
    method: str
    path_template: str
    body: dict | None
    headers: dict = field(default_factory=dict)
    live_writes_enabled: bool = field(default=False, init=False)


def expense_preview(args: AddTransactions, first_row: int, tab_name: str = "All Expenses") -> RequestPreview:
    args = AddTransactions.model_validate(args.model_dump())
    if any(entry.kind != "expense" for entry in args.entries):
        raise ValueError("Income has no verified storage mapping")
    if type(first_row) is not int or first_row < 2:
        raise ValueError("A freshly selected input row below the header is required")
    if not tab_name or len(tab_name) > 100 or any(ord(char) < 32 for char in tab_name):
        raise ValueError("Invalid adapter-owned tab name")
    layout = SheetLayout({"date": "A", "item": "B", "amount": "C", "note": "D"}, (), date_mode="serial_date")
    # Explicit A:D only: no categories, master changes, ID column or padding E:X.
    rows = [list(layout.cells(first_row + index, entry).values()) for index, entry in enumerate(args.entries)]
    escaped = tab_name.replace("'", "''")
    body = {"valueInputOption": "RAW", "data": [{
        "range": f"'{escaped}'!A{first_row}:D{first_row + len(rows) - 1}",
        "majorDimension": "ROWS", "values": rows,
    }], "includeValuesInResponse": True, "responseValueRenderOption": "UNFORMATTED_VALUE",
        "responseDateTimeRenderOption": "SERIAL_NUMBER"}
    return RequestPreview("POST", "/v4/spreadsheets/{server_spreadsheet_id}/values:batchUpdate", body)


def timed(value):
    return {"dateTime": value.astimezone(BANGKOK).isoformat(), "timeZone": "Asia/Bangkok"}


def identified_append_preview(args: AddTransactions, sheet_id: int, metadata: TransactionIdMetadata,
                              action_key: str) -> RequestPreview:
    args = AddTransactions.model_validate(args.model_dump())
    if not metadata.schema_approved or type(sheet_id) is not int or sheet_id < 0 or not action_key:
        raise ValueError("Approved server-owned metadata/target/action required")
    if any(entry.kind != "expense" for entry in args.entries):
        raise ValueError("Income has no verified input mapping")
    layout = SheetLayout({"date": "A", "item": "B", "amount": "C", "note": "D"}, (), date_mode="serial_date")
    rows = []
    for index, entry in enumerate(args.entries):
        inputs = list(layout.cells(1, entry).values())
        values = [{"userEnteredValue": {"numberValue" if type(value) in {int, float} else "stringValue": value}}
                  for value in inputs]
        # Empty CellData placeholders only align an approved non-adjacent ID column
        # in newly appended rows; they never select or clear an existing row.
        values += [{} for _ in range(metadata.index - 5)]
        values.append({"userEnteredValue": {"stringValue": transaction_id(action_key, index)}})
        rows.append({"values": values})
    body = {"requests": [{"appendCells": {"sheetId": sheet_id, "rows": rows, "fields": "userEnteredValue"}}]}
    # AppendCells has no client-selected row and no header/format/validation mutation.
    return RequestPreview("POST", "/v4/spreadsheets/{server_spreadsheet_id}:batchUpdate", body)


def create_event_preview(args: CreateEvent, action_key: str) -> RequestPreview:
    args = CreateEvent.model_validate(args.model_dump())
    if not action_key:
        raise ValueError("Server-derived action key required")
    reminders = {"useDefault": True} if args.reminder_minutes is None else {
        "useDefault": False, "overrides": [{"method": "popup", "minutes": args.reminder_minutes}],
    }
    body = {"id": hashlib.sha256(action_key.encode()).hexdigest(), "summary": args.title,
            "start": timed(args.start), "end": timed(args.end), "reminders": reminders}
    return RequestPreview("POST", "/calendar/v3/calendars/{server_calendar_id}/events", body)


def target_path(event_id: str, etag: str):
    if not event_id or not re.fullmatch(r'"[^\x00-\x1f\x7f"]+"', etag):
        raise ValueError("A freshly read target ID and exact quoted ETag are required")
    return f"/calendar/v3/calendars/{{server_calendar_id}}/events/{quote(event_id, safe='')}", {"If-Match": etag}


def update_event_preview(args: UpdateEvent, event_id: str, etag: str) -> RequestPreview:
    args = UpdateEvent.model_validate(args.model_dump())
    path, headers = target_path(event_id, etag)
    body = {}
    if args.title is not None:
        body["summary"] = args.title
    if args.start is not None:
        body.update(start=timed(args.start), end=timed(args.end))
    return RequestPreview("PATCH", path, body, headers)


def delete_event_preview(event_id: str, etag: str) -> RequestPreview:
    path, headers = target_path(event_id, etag)
    # Calling this builder cannot grant deletion consent; the controller owns it.
    return RequestPreview("DELETE", path, None, headers)
