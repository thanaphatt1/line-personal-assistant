from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import httpx
import pytest

from line_assistant.google_auth import SCOPES
from line_assistant.readonly_preflight import (PreflightTarget, ReadonlyPreflight, TEST_WORKBOOK_ID, PROJECT_ID,
                                             SERVICE_ACCOUNT, SHEET_FIELDS, local_adc_login_handoff, local_preflight_tokens)
from line_assistant.runtime_config import load_config, load_config_file
from line_assistant.transport import ApiError, LiveDisabled, NotReady, RestClient


NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
TOKEN = "synthetic-preflight-token"


def target():
    return PreflightTarget(TEST_WORKBOOK_ID, PROJECT_ID, SERVICE_ACCOUNT)


def metadata():
    return {"spreadsheetId": TEST_WORKBOOK_ID, "properties": {"timeZone": "Asia/Bangkok"},
            "sheets": [{"properties": {"sheetId": 0, "title": "Synthetic only", "gridProperties": {"rowCount": 1000}}}]}


def database():
    return {"name": f"projects/{PROJECT_ID}/databases/(default)", "type": "FIRESTORE_NATIVE", "locationId": "asia-southeast3"}


def clients(*, sheet=None, firestore=None, sheet_status=200, firestore_status=200):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == "GET" and request.content == b""
        if request.url.host == "sheets.googleapis.com":
            return httpx.Response(sheet_status, json=metadata() if sheet is None else sheet)
        assert request.url.host == "firestore.googleapis.com"
        return httpx.Response(firestore_status, json=database() if firestore is None else firestore)
    return RestClient("sheets", httpx.MockTransport(handle)), RestClient("firestore", httpx.MockTransport(handle)), calls


def test_preflight_is_exact_synthetic_sheet_and_firestore_metadata_only():
    sheets, firestore, calls = clients()
    report = ReadonlyPreflight(target(), sheets, firestore).run()
    assert len(calls) == 2
    assert calls[0].url.path == f"/v4/spreadsheets/{TEST_WORKBOOK_ID}"
    assert dict(calls[0].url.params) == {"fields": SHEET_FIELDS, "includeGridData": "false"}
    assert calls[1].url.path == f"/v1/projects/{PROJECT_ID}/databases/(default)"
    assert not any("/documents" in str(request.url) or "/values" in str(request.url) for request in calls)
    assert report["sheet_metadata_readable"] and report["firestore_metadata_readable"]
    assert report["writes_enabled"] is report["live_requests_enabled"] is report["firestore_data_iam_verified"] is report["schema_verified"] is False
    assert "Synthetic only" not in json.dumps(report) and TEST_WORKBOOK_ID not in repr(target())


@pytest.mark.parametrize("field,value", [("spreadsheet_id", "main-workbook-000000000001"), ("project_id", "other-project"),
                                         ("service_account_email", "other@other-project.iam.gserviceaccount.com"),
                                         ("database_id", "other-db"), ("region", "asia-southeast1")])
def test_other_targets_are_blocked_before_credentials_or_requests(field, value):
    with pytest.raises(NotReady):
        replace(target(), **{field: value})


def test_public_config_is_sufficient_for_planning_without_line_secrets_or_tab_ids():
    config = load_config_file(Path(__file__).parents[1] / ".env.example")
    assert config.channel_secret is None and config.sheet_expense_sheet_id is None
    assert PreflightTarget.from_config(config).spreadsheet_id == TEST_WORKBOOK_ID


def test_plan_and_default_auth_do_not_load_adc_env_or_run_login(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Credential/subprocess access")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "forbidden-file")
    command = local_adc_login_handoff(target())
    assert command == ("gcloud", "auth", "application-default", "login", "--impersonate-service-account=" + SERVICE_ACCOUNT)
    assert "config" not in command and "--quiet" not in command
    called = []
    tokens = local_preflight_tokens(target(), lambda: called.append(True), clock=forbidden)
    for provider in (tokens.google_workspace, tokens.google_cloud, tokens.calendar, tokens.line, tokens.model):
        with pytest.raises(LiveDisabled):
            provider()
    assert called == []


def test_preflight_construction_denies_live_clients_before_token_callbacks():
    calls = []
    with pytest.raises(LiveDisabled):
        ReadonlyPreflight(target(), RestClient("sheets", token=lambda: calls.append(True)),
                          RestClient("firestore", token=lambda: calls.append(True)))
    assert calls == []


def test_wrong_client_service_is_blocked():
    with pytest.raises(NotReady):
        ReadonlyPreflight(target(), RestClient("calendar"), RestClient("firestore"))


@pytest.mark.parametrize("sheet", [{"spreadsheetId": "wrong", "sheets": []}, {"spreadsheetId": TEST_WORKBOOK_ID, "sheets": []},
                                   {"spreadsheetId": TEST_WORKBOOK_ID, "sheets": [None]},
                                   {"spreadsheetId": TEST_WORKBOOK_ID, "sheets": [{"properties": {"sheetId": True, "title": "private"}}]},
                                   {"spreadsheetId": TEST_WORKBOOK_ID, "sheets": [{"properties": {"sheetId": 1, "title": "private"}}] * 2}])
def test_bad_sheet_metadata_stops_before_firestore(sheet):
    sheets, firestore, calls = clients(sheet=sheet)
    with pytest.raises(NotReady):
        ReadonlyPreflight(target(), sheets, firestore).run()
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("name", "private-other-resource"), ("type", "DATASTORE_MODE"),
                                         ("locationId", "asia-southeast1")])
def test_wrong_firestore_metadata_is_rejected_without_echoing_values(field, value):
    data = database()
    data[field] = value
    sheets, firestore, calls = clients(firestore=data)
    with pytest.raises(NotReady) as error:
        ReadonlyPreflight(target(), sheets, firestore).run()
    assert value not in str(error.value) and len(calls) == 2


@pytest.mark.parametrize("service", ["sheets", "firestore"])
def test_403_does_not_retry_write_or_claim_access(service):
    sheets, firestore, calls = clients(sheet_status=403 if service == "sheets" else 200,
                                      firestore_status=403 if service == "firestore" else 200)
    with pytest.raises(ApiError) as error:
        ReadonlyPreflight(target(), sheets, firestore).run()
    assert error.value.status == 403
    assert len(calls) == (1 if service == "sheets" else 2)


def test_mock_impersonation_requests_readonly_sheets_scope_and_firestore_metadata_once():
    auth_calls = []
    def auth(request):
        auth_calls.append(request)
        assert request.method == "POST" and request.url.host == "iamcredentials.googleapis.com"
        assert SERVICE_ACCOUNT in request.url.path
        scope = json.loads(request.content)["scope"]
        assert scope in ([SCOPES["sheets_readonly"]], [SCOPES["firestore"]])
        return httpx.Response(200, json={"accessToken": TOKEN, "expireTime": (NOW + timedelta(hours=1)).isoformat()})
    tokens = local_preflight_tokens(target(), lambda: "synthetic-caller",
                                    transport=httpx.MockTransport(auth), clock=lambda: NOW)
    api_calls = []
    def api(request):
        api_calls.append(request)
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        assert request.method == "GET"
        return httpx.Response(200, json=metadata() if request.url.host == "sheets.googleapis.com" else database())
    preflight = ReadonlyPreflight(target(), RestClient("sheets", httpx.MockTransport(api), token=tokens.google_workspace),
                                 RestClient("firestore", httpx.MockTransport(api), token=tokens.google_cloud))
    assert preflight.run()["sheet_metadata_readable"]
    assert len(auth_calls) == len(api_calls) == 2
