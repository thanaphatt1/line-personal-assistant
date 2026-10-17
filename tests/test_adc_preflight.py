from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import traceback

import google.auth
from google.auth import impersonated_credentials
from google.oauth2.credentials import Credentials as UserCredentials
import httpx
import pytest

from line_assistant.adc_preflight import (AuthorizedAdcPreflight, PREFLIGHT_SCOPES, PreflightFailure,
                                         SHEET_URL, FIRESTORE_URL, IAM_URL, OAUTH_URL, METADATA_FIELDS, HEADER_FIELDS)
from line_assistant.readonly_preflight import PreflightTarget, TEST_WORKBOOK_ID, PROJECT_ID, SERVICE_ACCOUNT
from line_assistant.transport import LiveDisabled


PRIVATE = "synthetic-private-token-DO-NOT-LOG"


def target():
    return PreflightTarget(TEST_WORKBOOK_ID, PROJECT_ID, SERVICE_ACCOUNT)


def credentials(account=SERVICE_ACCOUNT):
    source = UserCredentials(token=None, refresh_token="synthetic-refresh", token_uri=OAUTH_URL,
                             client_id="synthetic-client", client_secret="synthetic-client-secret")
    return impersonated_credentials.Credentials(source, account, target_scopes=["https://www.googleapis.com/auth/drive"])


class Providers:
    def __init__(self):
        self.calls = []
        self.fail_url = None
        self.fail_body = {"error": {"status": "PERMISSION_DENIED", "message": "Permission iam.serviceAccounts.getAccessToken denied " + PRIVATE}}
        self.fail_status = 403
        self.wrong_firestore = False
        self.wrong_workbook = False
        self.incomplete_headers = False

    def handle(self, request):
        self.calls.append(request)
        url = str(request.url).split("?")[0]
        if url == self.fail_url:
            return httpx.Response(self.fail_status, json=self.fail_body)
        if url == OAUTH_URL:
            assert request.method == "POST"
            return httpx.Response(200, json={"access_token": PRIVATE + "-source", "expires_in": 3600, "token_type": "Bearer"})
        if url == IAM_URL:
            assert request.method == "POST"
            body = json.loads(request.content)
            assert set(body["scope"]) == set(PREFLIGHT_SCOPES) and "drive" not in " ".join(body["scope"])
            assert request.headers["Authorization"] == "Bearer " + PRIVATE + "-source"
            return httpx.Response(200, json={"accessToken": PRIVATE, "expireTime": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        assert request.method == "GET" and request.content == b""
        assert request.headers["Authorization"] == "Bearer " + PRIVATE
        if url == SHEET_URL:
            if request.url.params["fields"] == METADATA_FIELDS:
                assert request.url.params["includeGridData"] == "false"
                return httpx.Response(200, json={"spreadsheetId": "wrong" if self.wrong_workbook else TEST_WORKBOOK_ID,
                    "properties": {"timeZone": "America/Los_Angeles"}, "sheets": [
                        {"properties": {"sheetId": 0, "title": "All Expenses", "gridProperties": {"rowCount": 1000, "columnCount": 5}}},
                        {"properties": {"sheetId": 1, "title": "Database", "gridProperties": {"rowCount": 1000, "columnCount": 5}}}]})
            assert request.url.params["fields"] == HEADER_FIELDS
            assert request.url.params.get_list("ranges") == ["'All Expenses'!A1:E1", "'Database'!A1:E1"]
            tabs = [{"properties": {"sheetId": 0, "title": "All Expenses"}, "data": [{
                "rowData": [{"values": [{"effectiveValue": {"stringValue": value}} for value in ["Date", "List", "Cost", "Remark", "transaction_id"]]}],
                "columnMetadata": [{}, {}, {}, {}, {"hiddenByUser": True}]}]}]
            if not self.incomplete_headers:
                tabs.append({"properties": {"sheetId": 1, "title": "Database"}, "data": [{"rowData": [{"values": [
                    {"effectiveValue": {"stringValue": value}} for value in ["Item", "Categories", "Sub-categories"]]}]}]})
            return httpx.Response(200, json={"spreadsheetId": TEST_WORKBOOK_ID, "sheets": tabs})
        assert url == FIRESTORE_URL
        return httpx.Response(200, json={"name": f"projects/{PROJECT_ID}/databases/(default)",
                                       "type": "FIRESTORE_NATIVE", "locationId": "asia-southeast1" if self.wrong_firestore else "asia-southeast3"})


def run(providers, credential=None):
    return AuthorizedAdcPreflight(target(), authorized=True, transport=httpx.MockTransport(providers.handle),
                                  adc_loader=lambda **kwargs: (credential or credentials(), None)).run()


def test_official_sdk_mints_exact_expected_identity_then_reads_only_schema_and_database_metadata(monkeypatch, caplog, capsys):
    providers = Providers()
    def loader(**kwargs):
        assert tuple(kwargs["scopes"]) == PREFLIGHT_SCOPES
        return credentials(), "untrusted-other-project"
    monkeypatch.setattr(google.auth, "default", loader)
    result = AuthorizedAdcPreflight(target(), authorized=True, transport=httpx.MockTransport(providers.handle)).run()
    assert result["status"] == "verified_readonly" and result["identity_verified"]
    assert result["sheet_metadata_readable"] and result["sheet_header_schema_readable"] and result["firestore_metadata_readable"]
    assert result["tabs"][0]["expense_headers_match"] and result["tabs"][0]["receipt_e_header_match"] and result["tabs"][0]["receipt_e_hidden"]
    assert result["tabs"][1]["database_headers_match"]
    assert not result["writes_enabled"] and not result["write_permissions_verified"] and not result["firestore_data_iam_verified"]
    assert len(providers.calls) == 5
    assert PRIVATE not in json.dumps(result) + caplog.text + capsys.readouterr().out
    assert not any("/documents" in str(request.url) or "/values" in str(request.url) for request in providers.calls)


def test_default_invocation_blocks_before_credentials_files_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected ADC/file access")
    monkeypatch.setattr(google.auth, "default", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(LiveDisabled):
        AuthorizedAdcPreflight(target())


@pytest.mark.parametrize("kind", ["wrong_target", "plain_user"])
def test_wrong_credential_identity_or_type_stops_before_mint_or_resource_reads(kind):
    providers = Providers()
    credential = credentials("wrong@wrong-project.iam.gserviceaccount.com") if kind == "wrong_target" else UserCredentials(token=PRIVATE)
    result = run(providers, credential)
    assert result["error"]["stage"] == "adc_identity_mismatch"
    assert providers.calls == []


@pytest.mark.parametrize("url", [OAUTH_URL, IAM_URL, SHEET_URL, FIRESTORE_URL])
def test_403_pauses_dependent_reads_and_never_retries_or_prints_error_body(url):
    providers = Providers()
    providers.fail_url = url
    result = run(providers)
    assert result["status"] == "blocked" and result["error"]["http_status"] == 403
    assert result["error"]["rpc_code"] == "PERMISSION_DENIED"
    assert result["error"]["required_permission"] == "iam.serviceAccounts.getAccessToken"
    assert len([request for request in providers.calls if str(request.url).split("?")[0] == url]) == 1
    assert PRIVATE not in json.dumps(result)
    assert str(providers.calls[-1].url).split("?")[0] == url


def test_service_disabled_reason_is_reported_without_raw_details():
    providers = Providers()
    providers.fail_url = IAM_URL
    providers.fail_body = {"error": {"status": "PERMISSION_DENIED", "message": PRIVATE, "details": [
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED", "metadata": {"private": PRIVATE}}]}}
    result = run(providers)
    assert result["error"]["reason"] == "SERVICE_DISABLED" and PRIVATE not in json.dumps(result)


def test_sdk_loader_exception_is_redacted():
    def loader(**kwargs):
        raise RuntimeError(PRIVATE)
    result = AuthorizedAdcPreflight(target(), authorized=True, transport=httpx.MockTransport(lambda request: None), adc_loader=loader).run()
    assert result["error"]["stage"] == "adc_load_or_refresh_failed" and PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("field,stage", [("wrong_workbook", "sheet_identity_or_metadata_mismatch"),
                                        ("incomplete_headers", "sheet_header_coverage_incomplete"),
                                        ("wrong_firestore", "firestore_database_metadata_mismatch")])
def test_schema_target_and_database_mismatches_stop(field, stage):
    providers = Providers()
    setattr(providers, field, True)
    result = run(providers)
    assert result["status"] == "blocked" and result["error"]["stage"] == stage
    assert not result["write_permissions_verified"]


def test_redirect_is_not_followed():
    providers = Providers()
    providers.fail_url, providers.fail_status = OAUTH_URL, 302
    result = run(providers)
    assert result["error"]["http_status"] == 302 and len(providers.calls) == 1


def test_auth_endpoint_override_is_blocked_before_http():
    providers = Providers()
    credential = impersonated_credentials.Credentials(credentials()._source_credentials, SERVICE_ACCOUNT,
        target_scopes=PREFLIGHT_SCOPES, iam_endpoint_override="https://unsafe.example/" + PRIVATE)
    result = run(providers, credential)
    assert result["error"]["stage"] == "auth_endpoint_blocked"
    assert len(providers.calls) == 1 and PRIVATE not in json.dumps(result)


def test_no_api_endpoint_outside_allowlist_and_no_credentials_in_repr():
    providers = Providers()
    preflight = AuthorizedAdcPreflight(target(), authorized=True, transport=httpx.MockTransport(providers.handle))
    with pytest.raises(PreflightFailure):
        preflight._get("https://sheets.googleapis.com/v4/spreadsheets/main-workbook", "unsafe")
    assert providers.calls == [] and "<redacted>" in repr(preflight)


def test_transport_error_is_sanitized():
    def broken(request):
        raise httpx.ConnectError(PRIVATE)
    result = AuthorizedAdcPreflight(target(), authorized=True, transport=httpx.MockTransport(broken),
                                    adc_loader=lambda **kwargs: (credentials(), None)).run()
    assert result["error"]["reason"] == "TRANSPORT_UNAVAILABLE" and PRIVATE not in json.dumps(result)
