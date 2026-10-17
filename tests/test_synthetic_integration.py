from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from google.auth import impersonated_credentials
from google.oauth2.credentials import Credentials as UserCredentials
import httpx
import pytest

from line_assistant.adc_preflight import IAM_URL, OAUTH_URL
from line_assistant.firestore_state import FirestoreState
from line_assistant.mock_api import MockApiServices
from line_assistant.readonly_preflight import TEST_WORKBOOK_ID, SERVICE_ACCOUNT
from line_assistant.synthetic_integration import (SyntheticGoogleSession, run_one_synthetic_expense, SCOPED_ACCESS,
                                                 COLLECTION, MAX_API_REQUESTS)
from line_assistant.transport import LiveDisabled, RestClient


RUN_ID = "ab" * 16
TOKEN = "synthetic-google-token-do-not-log"


def adc(**kwargs):
    assert tuple(kwargs["scopes"]) == SCOPED_ACCESS
    source = UserCredentials(token=None, refresh_token="synthetic-refresh", token_uri=OAUTH_URL,
                             client_id="synthetic-client", client_secret="synthetic-secret")
    return impersonated_credentials.Credentials(source, SERVICE_ACCOUNT, target_scopes=SCOPED_ACCESS), None


class NativeIdsApi(MockApiServices):
    def __init__(self):
        super().__init__(id_column="E")
        self.row_count = self.database_row_count = 12
        self.column_count = 5
        self.native_calls = []
        self.deny_firestore_commit = False
        self.missing_id_after_append = False
        self.cells.update({("All Expenses", 2, 1): 46000, ("All Expenses", 2, 2): "BTS", ("All Expenses", 2, 3): 99,
                           ("All Expenses", 2, 4): "PREEXISTING_SYNTHETIC", ("All Expenses", 2, 5): "existing-fixture-id"})

    def handle(self, request):
        self.native_calls.append((request.method, request.url.host, request.url.path))
        if str(request.url) == OAUTH_URL:
            return httpx.Response(200, json={"access_token": TOKEN + "-source", "expires_in": 3600})
        if str(request.url) == IAM_URL:
            assert set(json.loads(request.content)["scope"]) == set(SCOPED_ACCESS)
            return httpx.Response(200, json={"accessToken": TOKEN, "expireTime": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        if self.deny_firestore_commit and request.url.host == "firestore.googleapis.com" and request.method == "POST":
            return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "datastore.entities.create denied " + TOKEN}})
        if request.url.host == "sheets.googleapis.com" and request.method == "POST":
            body = json.loads(request.content)
            assert body["requests"][0]["appendCells"]["sheetId"] == 201
            body["requests"][0]["appendCells"]["sheetId"] = 101
            request = httpx.Request(request.method, request.url, headers=request.headers, content=json.dumps(body).encode())
        response = super().handle(request)
        if request.url.host == "sheets.googleapis.com" and response.status_code == 200:
            value = response.json()
            if "spreadsheetId" in value:
                value["spreadsheetId"] = TEST_WORKBOOK_ID
            for tab in value.get("sheets", []):
                props = tab["properties"]
                props["sheetId"] = 201 if props["title"] == "All Expenses" else 202
                if props["title"] == "Database":
                    props["gridProperties"]["columnCount"] = 3
            if self.missing_id_after_append and self.sheet_writes:
                for key in list(self.cells):
                    if key[0] == "All Expenses" and key[2] == 5 and key[1] > 2:
                        del self.cells[key]
            return httpx.Response(200, json=value)
        return response


def execute(api):
    return run_one_synthetic_expense(RUN_ID, datetime.now(timezone.utc), authorized=True,
                                     transport=httpx.MockTransport(api.handle), adc_loader=adc)


def test_one_signed_expense_and_recreated_controller_replay_use_real_adapter_contracts_once(caplog, capsys):
    api = NativeIdsApi()
    existing = deepcopy(api.cells)
    result = execute(api)
    assert result["status"] == "verified_single_synthetic_expense"
    assert result["row"] == 3 and result["amount"] == "1.00" and result["item"] == "BTS"
    assert result["matching_id_count"] == result["planner_calls"] == result["sheet_append_attempts"] == api.sheet_writes == 1
    assert result["replay_same_result"] and result["durable_action_and_receipt_verified"] and result["lease_released"]
    assert result["api_request_count"] <= MAX_API_REQUESTS and result["auth_requests"] == 2
    assert result["isolated_state_document_count"] == 1 and len(api.documents) == 1
    assert "/documents/" + COLLECTION + "/" in next(iter(api.documents))
    assert all(api.cells[key] == value for key, value in existing.items())
    assert result["line_requests"] == result["model_requests"] == result["calendar_requests"] == result["main_workbook_requests"] == 0
    assert TOKEN not in json.dumps(result) + caplog.text + capsys.readouterr().out


def test_lost_append_ack_is_positively_reconciled_before_replay_without_another_append():
    api = NativeIdsApi()
    api.fail_after_sheet_write = True
    result = execute(api)
    assert result["status"] == "verified_single_synthetic_expense"
    assert api.sheet_writes == result["sheet_append_attempts"] == 1
    assert result["exact_payload_verified"] and result["durable_action_and_receipt_verified"]
    assert result["positive_recovery_before_replay"] and not result["replay_same_result"]


def test_missing_receipt_after_uncertain_commit_stops_without_retry():
    api = NativeIdsApi()
    api.fail_after_sheet_write = True
    api.after_sheet_write = lambda server: [server.cells.pop(key, None) for key in list(server.cells) if key[0] == "All Expenses" and key[2] == 5 and key[1] > 2]
    result = execute(api)
    assert result["status"] == "blocked_or_uncertain" and api.sheet_writes == 1
    assert result["sheet_append_attempts"] == 1


def test_firestore_write_denial_stops_before_sheet_append_and_redacts_error():
    api = NativeIdsApi()
    api.deny_firestore_commit = True
    result = execute(api)
    assert result["status"] == "blocked_or_uncertain" and result["error"]["http_status"] == 403
    assert result["error"]["required_permission"] == "datastore.entities.create"
    assert api.sheet_writes == result["sheet_append_attempts"] == 0
    assert TOKEN not in json.dumps(result)


def test_default_test_entrypoint_blocks_before_loading_credentials_or_making_requests():
    with pytest.raises(LiveDisabled):
        run_one_synthetic_expense(RUN_ID, datetime.now(timezone.utc), adc_loader=lambda **kw: pytest.fail("Loaded credentials"))


@pytest.mark.parametrize("service,method,path", [("line", "POST", "/reply"), ("sheets", "POST", "/v4/spreadsheets/main:batchUpdate"),
    ("sheets", "PUT", "/v4/spreadsheets/" + TEST_WORKBOOK_ID), ("firestore", "DELETE", "/v1/anything")])
def test_other_services_targets_and_mutations_are_refused(service, method, path):
    session = SyntheticGoogleSession(RUN_ID, authorized=True, transport=httpx.MockTransport(lambda req: pytest.fail("Network")))
    with pytest.raises(LiveDisabled):
        session._allowed(service, method, path, {}, None)


def test_request_budget_is_enforced_before_http_or_authentication():
    session = SyntheticGoogleSession(RUN_ID, authorized=True, transport=httpx.MockTransport(lambda req: pytest.fail("Network")))
    session.credentials = object()
    session.request_count = MAX_API_REQUESTS
    with pytest.raises(LiveDisabled):
        session.request("sheets", "GET", session.sheet_base)


@pytest.mark.parametrize("collection", ["bad/collection", "../other", "", "x" * 65])
def test_firestore_namespace_cannot_cross_resource_paths(collection):
    with pytest.raises(ValueError):
        FirestoreState(RestClient("firestore"), "synthetic-owner", collection_id=collection)


def test_synthetic_capacity_is_not_extended_intentionally():
    api = NativeIdsApi()
    for row in range(3, 13):
        api.cells[("All Expenses", row, 4)] = "existing-synthetic"
    result = execute(api)
    assert result["status"] == "blocked_or_uncertain" and api.sheet_writes == 0
