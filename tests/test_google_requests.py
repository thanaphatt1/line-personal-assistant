from datetime import date

import pytest

from line_assistant.contracts import AddTransactions, CreateEvent, UpdateEvent
from line_assistant.google_requests import (
    create_event_preview, delete_event_preview, expense_preview, update_event_preview,
)
from line_assistant.sheet_dates import date_to_serial


def transactions(kind="expense"):
    return AddTransactions.model_validate({"entries": [
        {"item": "BTS", "amount": "65.25", "kind": kind, "date": "2026-10-11", "note": '=1+1'},
        {"item": "seven", "amount": "100", "kind": "expense", "date": "2026-10-11"},
    ]})


def test_four_column_batch_uses_raw_dates_amounts_and_literal_notes():
    preview = expense_preview(transactions(), first_row=2)
    assert preview.live_writes_enabled is False
    assert preview.method == "POST"
    assert "{server_spreadsheet_id}" in preview.path_template
    assert preview.body["valueInputOption"] == "RAW"
    assert preview.body["data"] == [{"range": "'All Expenses'!A2:D3", "majorDimension": "ROWS", "values": [
        [date_to_serial(date(2026, 10, 11)), "BTS", 65.25, "=1+1"],
        [date_to_serial(date(2026, 10, 11)), "seven", 100.0, ""],
    ]}]
    assert preview.body["includeValuesInResponse"] is True
    assert preview.body["responseDateTimeRenderOption"] == "SERIAL_NUMBER"


@pytest.mark.parametrize("first_row", [True, 0, 1, -1, 2.5, "446"])
def test_builder_never_accepts_a_header_row_or_implicit_row_conversion(first_row):
    with pytest.raises(ValueError):
        expense_preview(transactions(), first_row)


def test_preview_rejects_mixed_income_and_quotes_adapter_owned_tab_name():
    with pytest.raises(ValueError, match="Income"):
        expense_preview(transactions("income"), 2)
    assert expense_preview(transactions(), 2, "A'B").body["data"][0]["range"] == "'A''B'!A2:D3"


@pytest.mark.parametrize("minutes", [None, 0, 30, 40320])
def test_native_calendar_payloads_and_server_derived_repeatable_id(minutes):
    args = CreateEvent.model_validate({"title": "ตรวจฟัน", "start": "2026-10-11T07:00:00Z",
                                      "end": "2026-10-11T08:00:00Z", "reminder_minutes": minutes})
    preview = create_event_preview(args, "mock-owner:event:create_event")
    assert create_event_preview(args, "mock-owner:event:create_event") == preview
    assert create_event_preview(args, "mock-owner:other:create_event").body["id"] != preview.body["id"]
    assert set(preview.body) == {"id", "summary", "start", "end", "reminders"}
    assert preview.body["start"] == {"dateTime": "2026-10-11T14:00:00+07:00", "timeZone": "Asia/Bangkok"}
    assert preview.body["reminders"] == ({"useDefault": True} if minutes is None else {
        "useDefault": False, "overrides": [{"method": "popup", "minutes": minutes}],
    })
    assert len(preview.body["id"]) == 64
    assert set(preview.body["id"]) <= set("0123456789abcdef")
    assert preview.live_writes_enabled is False


def test_calendar_patch_and_delete_include_fresh_exact_etag():
    args = UpdateEvent.model_validate({"event_ref": "last", "title": "แก้ชื่อ"})
    preview = update_event_preview(args, "adapter-event", '"version-2"')
    assert preview.method == "PATCH"
    assert preview.body == {"summary": "แก้ชื่อ"}
    assert preview.headers == {"If-Match": '"version-2"'}
    assert "last" not in preview.path_template
    deletion = delete_event_preview("adapter/event?wrong", '"version-2"')
    assert deletion.method == "DELETE"
    assert deletion.body is None
    assert deletion.path_template.endswith("adapter%2Fevent%3Fwrong")
    assert deletion.headers == preview.headers
    assert deletion.live_writes_enabled is False


@pytest.mark.parametrize("etag", ["", "*", "unquoted", '"bad\nvalue"', '"bad\rvalue"'])
def test_unverified_or_header_injecting_etag_cannot_build_modification(etag):
    with pytest.raises(ValueError):
        delete_event_preview("adapter-event", etag)
