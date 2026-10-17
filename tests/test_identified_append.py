from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
from threading import Barrier

import pytest

from line_assistant.api_sandbox import api_sandbox
from line_assistant.confirmed_target import load_disabled_sheet_target
from line_assistant.contracts import AddTransactions
from line_assistant.google_requests import identified_append_preview
from line_assistant.mock_api import MockApiServices
from line_assistant.service import ProviderFailure
from line_assistant.sheet_metadata import TransactionIdMetadata, transaction_id
from line_assistant.sheets_api import SheetTarget, SheetsApi
from line_assistant.transport import RestClient, WriteBlocked


NOW = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)


def args():
    return AddTransactions.model_validate({"entries": [
        {"item": "BTS", "amount": "65", "date": "2026-10-11", "kind": "expense", "note": "=1+1"},
        {"item": "seven", "amount": "100", "date": "2026-10-11", "kind": "expense"},
    ]})


def api(providers, column="E", guard=None):
    target = SheetTarget(spreadsheet_id="synthetic-sheet", writes_enabled=True, input_access_verified=True,
                         recovery="approved_id_column", transaction_id_metadata=TransactionIdMetadata(column, schema_approved=True))
    return SheetsApi(RestClient("sheets", providers.transport()), target, effect_guard=guard)


def event(identifier="expense"):
    return {"type": "message", "webhookEventId": identifier, "timestamp": int(NOW.timestamp() * 1000),
            "source": {"type": "user", "userId": "mock-owner"},
            "message": {"type": "text", "id": identifier, "text": "BTS 65\nseven 100"}}


def disabled_target_config():
    # Synthetic identity: this suite must also pass without ignored private files.
    return {"spreadsheet_id": "synthetic-sheet", "expense_tab": "All Expenses", "database_tab": "Database",
            "expense_sheet_id": 101, "database_sheet_id": 102, "max_rows": 1000,
            "input_columns": {"date": "A", "item": "B", "amount": "C", "note": "D"},
            "transaction_id_metadata": {"column": "E", "header": "transaction_id", "schema_approved": True, "require_hidden": True},
            "metadata_zero_based_index": 4, "row_allocation": "server_append_cells",
            "live_transport_enabled": False, "writes_enabled": False}


def test_confirmed_target_contract_is_exact_and_stays_disabled(tmp_path):
    path = tmp_path / "target.json"
    path.write_text(json.dumps(disabled_target_config()))
    target = load_disabled_sheet_target(path)
    assert target.expense_sheet_id == 101
    assert target.transaction_id_metadata == TransactionIdMetadata("E", "transaction_id", True, True)
    assert target.transaction_id_metadata.index == 5
    assert target.transaction_id_metadata.zero_based_index == 4
    assert not target.writes_enabled and not target.input_access_verified
    assert target.row_allocation == "server_append_cells"


@pytest.mark.parametrize("change", ["live_flag", "input_columns", "index", "allocation", "column", "header", "approval", "hidden"])
def test_disabled_configuration_refuses_drift(tmp_path, change):
    value = disabled_target_config()
    if change == "live_flag":
        value["live_transport_enabled"] = True
    elif change == "input_columns":
        value["input_columns"]["amount"] = "E"
    elif change == "index":
        value["metadata_zero_based_index"] = 5
    elif change == "allocation":
        value["row_allocation"] = "legacy_mock_update"
    elif change == "column":
        value["transaction_id_metadata"]["column"] = "F"
        value["metadata_zero_based_index"] = 5
    elif change == "header":
        value["transaction_id_metadata"]["header"] = "different_id"
    elif change == "approval":
        value["transaction_id_metadata"]["schema_approved"] = False
    else:
        value["transaction_id_metadata"]["require_hidden"] = False
    path = tmp_path / "target.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_disabled_sheet_target(path)


@pytest.mark.parametrize("column,width", [("E", 5), ("G", 7)])
def test_append_body_has_server_row_allocation_literal_values_and_separate_metadata(column, width):
    metadata = TransactionIdMetadata(column, schema_approved=True)
    preview = identified_append_preview(args(), 101, metadata, "server-key")
    assert preview.path_template.endswith(":batchUpdate")
    assert preview.live_writes_enabled is False
    assert len(preview.body["requests"]) == 1
    append = preview.body["requests"][0]["appendCells"]
    assert set(append) == {"sheetId", "rows", "fields"}
    assert append["fields"] == "userEnteredValue"
    assert len(append["rows"][0]["values"]) == width
    assert append["rows"][0]["values"][3] == {"userEnteredValue": {"stringValue": "=1+1"}}
    assert append["rows"][0]["values"][-1] == {"userEnteredValue": {"stringValue": transaction_id("server-key", 0)}}
    assert all(cell == {} for cell in append["rows"][0]["values"][4:-1])


def test_server_append_uses_latest_occupied_row_without_overwriting_human_data():
    providers = MockApiServices(id_column="E")
    providers.cells[("All Expenses", 3, 24)] = "other column occupied"
    original = deepcopy(providers.cells)
    providers.before_sheet_write = lambda mock: mock.cells.update({("All Expenses", 4, 4): "human raced before commit"})
    refs = api(providers).add(args().entries, "key")
    assert refs == ("'All Expenses'!A5:D5", "'All Expenses'!A6:D6")
    assert all(providers.cells[key] == value for key, value in original.items())
    assert providers.cells[("All Expenses", 4, 4)] == "human raced before commit"
    assert not any(method == "POST" and path.endswith("/values:batchUpdate") for method, host, path in providers.calls)


def test_catalog_read_respects_separate_database_grid_and_refuses_truncated_lookup():
    providers = MockApiServices(id_column="E")
    sheets = api(providers)
    assert sheets.catalog().match("BTS").canonical == "BTS"
    assert any(path.endswith("'Database'!A1:C996") for method, host, path in providers.calls)
    providers.database_row_count = 1001
    from line_assistant.transport import NotReady
    with pytest.raises(NotReady):
        sheets.catalog()


def test_historical_matching_inputs_without_ids_are_never_bot_receipts_or_backfilled():
    providers = MockApiServices(id_column="E")
    inputs = [46306, "BTS", 65.0, "=1+1"]
    for column, value in enumerate(inputs, start=1):
        providers.cells[("All Expenses", 2, column)] = value
    sheets = api(providers)
    assert sheets.reconcile("key", args().model_dump(mode="json")).outcome == "unknown"
    refs = sheets.add(args().entries, "key")
    assert refs[0] == "'All Expenses'!A3:D3"
    assert ("All Expenses", 2, 5) not in providers.cells
    assert [providers.cells[("All Expenses", 2, col)] for col in range(1, 5)] == inputs


@pytest.mark.parametrize("drift", ["case", "space", "moved", "unhidden"])
def test_exact_header_and_hidden_column_drift_blocks_write_and_positive_reconciliation(drift):
    providers = MockApiServices(id_column="E")
    sheets = api(providers)
    sheets.add(args().entries, "key")
    if drift == "case":
        providers.cells[("All Expenses", 1, 5)] = "Transaction_id"
    elif drift == "space":
        providers.cells[("All Expenses", 1, 5)] = "transaction_id "
    elif drift == "moved":
        providers.cells[("All Expenses", 1, 6)] = providers.cells.pop(("All Expenses", 1, 5))
    else:
        providers.hidden_columns.clear()
    assert sheets.reconcile("key", args().model_dump(mode="json")).outcome == "unknown"
    with pytest.raises(WriteBlocked):
        sheets.add(args().entries, "new-key")
    assert providers.sheet_writes == 1


@pytest.mark.parametrize("problem", ["missing_id", "duplicate_id", "changed_payload", "partial_batch", "outside_scan"])
def test_incomplete_or_conflicting_receipts_remain_unknown_without_any_retry(problem):
    providers = MockApiServices(id_column="E")
    sheets = api(providers)
    sheets.add(args().entries, "key")
    if problem in {"missing_id", "partial_batch"}:
        del providers.cells[("All Expenses", 2 if problem == "missing_id" else 3, 5)]
    elif problem == "duplicate_id":
        providers.cells[("All Expenses", 4, 5)] = providers.cells[("All Expenses", 2, 5)]
    elif problem == "changed_payload":
        providers.cells[("All Expenses", 2, 3)] = 66
    else:
        providers.row_count = 1001
        providers.cells[("All Expenses", 1001, 5)] = providers.cells[("All Expenses", 2, 5)]
    assert sheets.reconcile("key", args().model_dump(mode="json")).outcome == "unknown"
    assert providers.sheet_writes == 1


def test_positive_id_and_payload_are_read_in_one_range_response():
    providers = MockApiServices(id_column="E")
    sheets = api(providers)
    sheets.add(args().entries, "key")
    providers.calls.clear()
    assert sheets.reconcile("key", args().model_dump(mode="json")).outcome == "committed"
    value_reads = [path for method, host, path in providers.calls if "/values/" in path]
    assert len(value_reads) == 1
    assert value_reads[0].endswith("'All Expenses'!A1:E1000")


def test_lost_append_ack_recovers_complete_batch_after_controller_recreation():
    assistant, _, providers = api_sandbox(clock=lambda: NOW, approved_mock_id_column=True)
    providers.fail_after_sheet_write = True
    with pytest.raises(ProviderFailure):
        assistant.handle(event())
    restarted, _, _ = api_sandbox(services=providers, clock=lambda: NOW, approved_mock_id_column=True)
    result = restarted.handle(event())
    assert result.status == "recorded" and len(result.data["refs"]) == 2
    assert restarted.handle(event()) == result
    assert providers.sheet_writes == 1


def test_concurrent_distinct_appends_are_allocated_by_server_without_colliding_rows():
    providers = MockApiServices(id_column="E")
    barrier = Barrier(8)
    apis = [api(providers, guard=lambda: barrier.wait(timeout=10)) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: apis[index].add(args().entries, f"key-{index}"), range(8)))
    refs = [ref for result in results for ref in result]
    assert len(refs) == len(set(refs)) == 16
    assert providers.sheet_writes == 8
    for index in range(8):
        assert api(providers).reconcile(f"key-{index}", args().model_dump(mode="json")).outcome == "committed"


def test_ids_do_not_enforce_uniqueness_if_callers_bypass_the_controller_lease():
    providers = MockApiServices(id_column="E")
    barrier = Barrier(2)
    apis = [api(providers, guard=lambda: barrier.wait(timeout=10)) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sheets.add, args().entries, "same-key") for sheets in apis]
        for future in futures:
            try:
                future.result()
            except Exception:
                pass  # Post-write proof refuses duplicated IDs; neither is retried.
    assert providers.sheet_writes == 2
    assert api(providers).reconcile("same-key", args().model_dump(mode="json")).outcome == "unknown"


def test_capacity_race_does_not_overwrite_rows_and_remains_unknown_outside_scan():
    providers = MockApiServices(id_column="E")
    sheets = api(providers)
    providers.before_sheet_write = lambda mock: mock.cells.update({("All Expenses", 1000, 4): "external writer filled capacity"})
    from line_assistant.transport import OutcomeUnknown
    with pytest.raises(OutcomeUnknown):
        sheets.add(args().entries, "key")
    assert providers.cells[("All Expenses", 1000, 4)] == "external writer filled capacity"
    assert providers.row_count == 1002
    assert sheets.reconcile("key", args().model_dump(mode="json")).outcome == "unknown"
    assert providers.sheet_writes == 1
