"""Only synthetic HTTP fixtures: no credentials or live workbook access."""
from copy import deepcopy
from datetime import timedelta
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from line_assistant.cloud_run import create_cloud_run_app
from line_assistant.mock_api import MockApiServices
from line_assistant.model_api import RESERVATION_MICRO_USD
from line_assistant.persistence import SQLiteState, restore_state, state_snapshot
from line_assistant.pilot_config import MAIN_WORKBOOK_ID, PilotConfig
from line_assistant.readonly_preflight import TEST_WORKBOOK_ID
from line_assistant.runtime_config import ConfigError
from line_assistant.state import Claim, MemoryState, Pending
from line_assistant.transport import LiveDisabled
from test_canary import CanaryApi, canary_event, snapshot, values
from test_cloud_run import NOW, event, fake_secrets, send
from test_model_api import response


def production_values():
    return {key: value for key, value in {**values(), "PILOT_MODE": "production",
            "PILOT_SPREADSHEET_ID": MAIN_WORKBOOK_ID, "PILOT_MODEL_MONTHLY_MICRO_USD": "500000",
            "PILOT_MODEL_MAX_CALLS_DAY": "20", "PILOT_MODEL_MAX_CALLS_MONTH": "300"}.items()
            if not key.startswith("PILOT_CANARY_")}


class SwitchApi(CanaryApi):
    def __init__(self):
        super().__init__()
        self.main = MockApiServices(id_column="E")
        self.main.row_count = 1000
        self.main.column_count = 24
        self.main.database_row_count = 996
        self.sheet_targets = []

    def handle(self, request):
        if request.url.host == "sheets.googleapis.com":
            self.sheet_targets.append(request.url.path.split("/values")[0].split(":")[0])
            if MAIN_WORKBOOK_ID in request.url.path:
                result = self.main.sheets(request)
                body = result.json()
                if "spreadsheetId" in body:
                    body["spreadsheetId"] = MAIN_WORKBOOK_ID
                return httpx.Response(result.status_code, json=body)
        if request.url.host == "api.openai.com" and "BTS" in json.loads(request.content)["input"][0]["content"]:
            self.models += 1
            assert snapshot(self)["model_budgets"]["2026-10"]["calls"] >= 2
            return httpx.Response(200, json=response())
        return super().handle(request)


def app(api, config):
    return create_cloud_run_app(config, transport=httpx.MockTransport(api.handle),
                                secret_loader=fake_secrets, clock=lambda: NOW)


def passed_canary(api):
    with TestClient(app(api, values())) as client:
        assert send(client, canary_event()).status_code == 200
    assert snapshot(api)["production_ready"] is True
    assert api.models == api.lines == api.google.sheet_writes == 1


@pytest.mark.parametrize("override", [
    {"PILOT_MODE": "production"}, {"PILOT_SPREADSHEET_ID": MAIN_WORKBOOK_ID},
    {"PILOT_MODE": "synthetic_ongoing"},
])
def test_mode_and_workbook_must_be_explicitly_paired(override):
    with pytest.raises(ConfigError):
        PilotConfig.from_mapping({**values(), **override})


@pytest.mark.parametrize("override", [{"PILOT_MODEL_MONTHLY_MICRO_USD": "500001"},
    {"PILOT_MODEL_MAX_CALLS_DAY": "101"}, {"PILOT_MODEL_MAX_CALLS_MONTH": "301"},
    {"PILOT_CANARY_NONCE": "c7" * 16}])
def test_production_cannot_expand_budget_or_retain_canary_config(override):
    with pytest.raises(ConfigError):
        PilotConfig.from_mapping({**production_values(), **override})


def test_production_without_passed_canary_has_no_model_sheet_or_reply_calls():
    api = SwitchApi()
    with TestClient(app(api, production_values())) as client:
        assert send(client, event(event_id="real-event")).status_code == 200
    assert api.models == api.lines == api.google.sheet_writes == api.main.sheet_writes == 0
    assert not api.sheet_targets


@pytest.mark.parametrize("fault", ["model_timeout", "append_timeout", "reply_timeout"])
def test_failed_or_uncertain_canary_cannot_activate_production_or_consume_new_attempt(fault):
    api = SwitchApi()
    api.fault = fault
    with TestClient(app(api, values())) as client:
        send(client, canary_event())
    counts = api.models, api.google.sheet_writes, api.lines
    assert snapshot(api)["production_ready"] is False
    with TestClient(app(api, production_values())) as client:
        assert send(client, event(event_id="real-event")).status_code == 200
    assert (api.models, api.google.sheet_writes, api.lines) == counts
    assert api.main.sheet_writes == 0 and not any(MAIN_WORKBOOK_ID in path for path in api.sheet_targets)


def test_passed_canary_switch_preserves_budget_archives_receipts_and_blocks_replay():
    api = SwitchApi()
    passed_canary(api)
    with TestClient(app(api, production_values())) as client:
        assert send(client, canary_event()).status_code == 200
        assert api.models == api.lines == api.google.sheet_writes == 1
        assert not api.main.sheet_writes
        assert send(client, event(event_id="real-event")).status_code == 200
    stored = snapshot(api)
    assert stored["workbook_id"] == MAIN_WORKBOOK_ID
    assert stored["workspaces"][TEST_WORKBOOK_ID]["receipts"]["pilot-event"]["result"]["status"] == "recorded"
    assert stored["workspaces"][TEST_WORKBOOK_ID]["replies"]["pilot-event"]["status"] == "sent"
    assert stored["model_budgets"]["2026-10"]["reserved_micro_usd"] == 2 * RESERVATION_MICRO_USD
    assert stored["event_workbooks"] == {"pilot-event": TEST_WORKBOOK_ID, "real-event": MAIN_WORKBOOK_ID}
    assert stored["receipts"]["real-event"]["result"]["status"] == "recorded"
    assert "ในชีตจริง" in stored["receipts"]["real-event"]["result"]["message"]
    assert api.google.sheet_writes == api.main.sheet_writes == 1
    assert api.main.cells[("All Expenses", 2, 2)] == "BTS"
    assert all(value != "TEST Coffee" for key, value in api.main.cells.items() if key[0] == "All Expenses")
    with TestClient(app(api, production_values())) as client:
        assert send(client, event(event_id="real-event")).status_code == 200
        assert send(client, canary_event()).status_code == 200
    assert api.models == api.lines == 2
    assert api.google.sheet_writes == api.main.sheet_writes == 1


def test_unknown_main_item_is_pending_and_master_is_not_written():
    api = SwitchApi()
    passed_canary(api)
    # LLM spelling stays raw. A genuinely new item cannot inherit fallback category.
    original = deepcopy(api.main.cells)
    api.main.cells.pop(("Database", 2, 1))
    with TestClient(app(api, production_values())) as client:
        assert send(client, event(event_id="real-event")).status_code == 200
    assert snapshot(api)["receipts"]["real-event"]["result"]["status"] == "clarification"
    assert api.main.sheet_writes == 0
    assert all(api.main.cells[key] == value for key, value in original.items() if key != ("Database", 2, 1))


def test_main_capacity_does_not_intentionally_extend_grid():
    api = SwitchApi()
    passed_canary(api)
    api.main.cells[("All Expenses", 1000, 24)] = "existing fixture formula"
    with TestClient(app(api, production_values())) as client:
        assert send(client, event(event_id="real-event")).status_code == 200
    assert api.main.sheet_writes == 0 and api.main.row_count == 1000
    assert snapshot(api)["model_budgets"]["2026-10"]["calls"] == 2


def test_exact_1001_row_main_grid_has_bounded_identified_append_and_reconciliation():
    api = SwitchApi()
    api.main.row_count = 1001
    passed_canary(api)
    assert PilotConfig.from_mapping(production_values()).sheet_target().max_rows == 1001
    assert PilotConfig.from_mapping(values()).sheet_target().max_rows == 12
    with TestClient(app(api, production_values())) as client:
        assert send(client, event(event_id="main-1001")).status_code == 200
    stored = snapshot(api)
    assert stored["receipts"]["main-1001"]["result"]["status"] == "recorded"
    assert api.main.sheet_writes == 1 and api.main.row_count == 1001
    assert api.main.cells[("All Expenses", 2, 2)] == "BTS"
    assert api.google.sheet_writes == 1  # No extra synthetic-workbook write.


def test_larger_main_grid_fails_before_any_main_append():
    api = SwitchApi()
    api.main.row_count = 1002
    passed_canary(api)
    with TestClient(app(api, production_values())) as client:
        assert send(client, event(event_id="main-too-large")).status_code == 503
    assert api.main.sheet_writes == 0 and api.main.row_count == 1002
    assert api.google.sheet_writes == 1


@pytest.mark.parametrize("limit", [{"PILOT_MODEL_MONTHLY_MICRO_USD": str(RESERVATION_MICRO_USD)},
    {"PILOT_MODEL_MAX_CALLS_DAY": "1"}, {"PILOT_MODEL_MAX_CALLS_MONTH": "1"}])
def test_canary_cost_and_call_count_are_not_reset_when_production_limits_apply(limit):
    api = SwitchApi()
    passed_canary(api)
    with TestClient(app(api, {**production_values(), **limit})) as client:
        assert send(client, event(event_id="real-event")).status_code == 200
    assert api.models == api.google.sheet_writes == 1 and api.main.sheet_writes == 0
    assert snapshot(api)["model_budgets"]["2026-10"]["calls"] == 1
    assert snapshot(api)["model_budgets"]["2026-10"]["reserved_micro_usd"] == RESERVATION_MICRO_USD


def test_production_signature_and_owner_checks_precede_any_provider_call():
    api = SwitchApi()
    with TestClient(app(api, production_values())) as client:
        assert send(client, event(), secret="wrong").status_code == 401
        assert send(client, event(owner="U" + "b" * 32)).status_code == 200
        group = event()
        group["source"]["type"] = "group"
        assert send(client, group).status_code == 200
    assert api.metadata == api.models == api.lines == 0
    assert not api.google.documents and not api.sheet_targets


def test_sqlite_workspace_switch_and_legacy_restore_preserve_global_accounting(tmp_path):
    state = SQLiteState(tmp_path / "state.db", "fixture-owner")
    with state.session():
        state.claims["old"] = Claim("fingerprint", NOW + timedelta(days=30))
        state.pending = Pending("unknown_item", "abc", "old", 1, NOW + timedelta(minutes=30), {})
        legacy = state_snapshot(state, "fixture-owner")
        for name in ("workbook_id", "workspaces", "event_workbooks", "production_ready"):
            legacy.pop(name)
        restore_state(state, legacy, "fixture-owner")
        state.select_workbook(MAIN_WORKBOOK_ID, legacy_workbook=TEST_WORKBOOK_ID)
        assert state.pending is None and state.claims == {}
        assert state.event_workbooks["old"] == TEST_WORKBOOK_ID
        state.model_halted = True
    recreated = SQLiteState(tmp_path / "state.db", "fixture-owner")
    with recreated.session():
        assert recreated.model_halted is True
        assert recreated.workbook_id == MAIN_WORKBOOK_ID
        recreated.select_workbook(TEST_WORKBOOK_ID, legacy_workbook=TEST_WORKBOOK_ID)
        assert recreated.pending.origin_event_id == "old"
        assert "old" in recreated.claims and recreated.model_halted is True


def test_production_transport_rejects_wrong_workbook_and_unbounded_range():
    api = SwitchApi()
    instance = app(api, production_values()).state.pilot_http
    for path in (f"/v4/spreadsheets/{TEST_WORKBOOK_ID}", f"/v4/spreadsheets/{MAIN_WORKBOOK_ID}/values/'All Expenses'!A1:X1002"):
        with pytest.raises(LiveDisabled):
            instance.allowed("sheets", "GET", path, None, {"valueRenderOption": "FORMULA", "dateTimeRenderOption": "SERIAL_NUMBER"})
