"""Explicit, narrowly authorized ADC preflight, separate from the disabled app.

Official Google auth loads existing ADC. No raw credential files are inspected by
this code; tokens stay inside credential/request objects. Only fixed auth endpoints,
the synthetic workbook's metadata/header GETs and Firestore metadata GET are allowed.
"""

import json
from urllib.parse import quote

import httpx

from .google_auth import SCOPES
from .readonly_preflight import PreflightTarget, TEST_WORKBOOK_ID, PROJECT_ID, SERVICE_ACCOUNT
from .transport import LiveDisabled


PREFLIGHT_SCOPES = (SCOPES["sheets_readonly"], SCOPES["firestore"])
SHEET_URL = "https://sheets.googleapis.com/v4/spreadsheets/" + TEST_WORKBOOK_ID
FIRESTORE_URL = "https://firestore.googleapis.com/v1/projects/" + PROJECT_ID + "/databases/(default)"
IAM_URL = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/" + SERVICE_ACCOUNT + ":generateAccessToken"
OAUTH_URL = "https://oauth2.googleapis.com/token"
METADATA_FIELDS = "spreadsheetId,properties(timeZone),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),protectedRanges(range,warningOnly,requestingUserCanEdit))"
HEADER_FIELDS = "spreadsheetId,sheets(properties(sheetId,title),data(startRow,startColumn,columnMetadata(hiddenByUser),rowData(values(effectiveValue))))"
_CODES = {"PERMISSION_DENIED", "UNAUTHENTICATED", "NOT_FOUND", "RESOURCE_EXHAUSTED", "INVALID_ARGUMENT", "UNAVAILABLE"}
_REASONS = {"SERVICE_DISABLED", "IAM_PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "USER_PROJECT_DENIED", "CONSUMER_INVALID", "BILLING_DISABLED"}
_PERMISSIONS = {"iam.serviceAccounts.getAccessToken", "datastore.databases.getMetadata", "datastore.databases.get", "serviceusage.services.use",
                "datastore.entities.get", "datastore.entities.create", "datastore.entities.update"}


class PreflightFailure(RuntimeError):
    def __init__(self, stage, *, status=None, code=None, reason=None, permission=None):
        self.summary = {"stage": stage, "http_status": status, "rpc_code": code, "reason": reason, "required_permission": permission}
        super().__init__("Read-only preflight stopped")


def _failure(stage, response):
    code = reason = permission = None
    try:
        data = response.json()
        error = data.get("error", {})
        if isinstance(error, str):  # OAuth errors; never copy descriptions.
            reason = error if error in {"invalid_grant", "invalid_client", "invalid_scope"} else None
        elif isinstance(error, dict):
            code = error.get("status") if error.get("status") in _CODES else None
            message = error.get("message", "")
            if isinstance(message, str):
                permission = next((value for value in sorted(_PERMISSIONS, key=len, reverse=True) if value in message), None)
            for detail in error.get("details", []):
                if not isinstance(detail, dict):
                    continue
                if detail.get("reason") in _REASONS:
                    reason = detail["reason"]
                metadata = detail.get("metadata", {})
                if isinstance(metadata, dict) and metadata.get("permission") in _PERMISSIONS:
                    permission = metadata["permission"]
    except (ValueError, AttributeError, TypeError):
        pass
    return PreflightFailure(stage, status=response.status_code, code=code, reason=reason, permission=permission)


class _SdkResponse:
    def __init__(self, response):
        self.status, self.headers, self.data = response.status_code, response.headers, response.content


class _AuthRequest:
    def __init__(self, client):
        self.client, self.calls, self.failure = client, {}, None
        self.target_mint_confirmed = False

    def __call__(self, url, method="GET", body=None, headers=None, timeout=30, **kwargs):
        stage = "adc_source_refresh" if url == OAUTH_URL else "service_account_impersonation"
        if method != "POST" or url not in {OAUTH_URL, IAM_URL}:
            raise PreflightFailure("auth_endpoint_blocked")
        if self.calls.get(url, 0) >= 1:
            raise PreflightFailure("auth_retry_blocked")
        self.calls[url] = self.calls.get(url, 0) + 1
        try:
            response = self.client.post(url, content=body, headers=headers, timeout=15)
        except httpx.HTTPError:
            self.failure = PreflightFailure(stage, reason="TRANSPORT_UNAVAILABLE")
            raise self.failure from None
        if response.status_code != 200:
            self.failure = _failure(stage, response)
            raise self.failure
        if url == IAM_URL:
            self.target_mint_confirmed = True
        return _SdkResponse(response)


class AuthorizedAdcPreflight:
    def __init__(self, target: PreflightTarget, *, authorized=False, transport=None, adc_loader=None):
        # A dedicated call is required. No environment/config/app-startup switch exists.
        if not authorized:
            raise LiveDisabled("Existing ADC preflight requires explicit action authorization")
        if type(target) is not PreflightTarget:
            raise PreflightFailure("target_blocked")
        target.__post_init__()
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise LiveDisabled("Use the dedicated verified preflight transport or a mock")
        self.target = target
        self.client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False, timeout=15)
        self.auth_request = _AuthRequest(self.client)
        self.adc_loader = adc_loader
        self.report = {"status": "not_started", "identity_verified": False, "sheet_metadata_readable": False,
                       "sheet_header_schema_readable": False, "firestore_metadata_readable": False,
                       "firestore_data_iam_verified": False, "write_permissions_verified": False,
                       "writes_enabled": False, "normal_live_startup_enabled": False}
        self.credentials = None

    def __repr__(self):
        return "AuthorizedAdcPreflight(credentials=<redacted>, writes_enabled=False)"

    def _load(self):
        import google.auth
        from google.auth import impersonated_credentials
        try:
            loader = self.adc_loader or google.auth.default
            credentials, _ = loader(scopes=PREFLIGHT_SCOPES, request=self.auth_request)
            if type(credentials) is not impersonated_credentials.Credentials or credentials.signer_email != SERVICE_ACCOUNT:
                raise PreflightFailure("adc_identity_mismatch")
            credentials = credentials.with_scopes(PREFLIGHT_SCOPES)
            credentials.refresh(self.auth_request)
            if not self.auth_request.target_mint_confirmed:
                raise PreflightFailure("identity_mint_unverified")
            self.credentials = credentials
            self.report["identity_verified"] = True
            self.report["identity_evidence"] = "expected_sdk_impersonated_target_and_google_iam_mint"
        except PreflightFailure:
            raise
        except Exception:
            if self.auth_request.failure:
                raise self.auth_request.failure from None
            raise PreflightFailure("adc_load_or_refresh_failed") from None

    def _get(self, url, stage, params=None):
        if url not in {SHEET_URL, FIRESTORE_URL} or self.credentials is None:
            raise PreflightFailure("resource_endpoint_blocked")
        headers = {}
        try:
            self.credentials.before_request(self.auth_request, "GET", url, headers)
            response = self.client.get(url, params=params, headers=headers)
        except PreflightFailure:
            raise
        except Exception:
            if self.auth_request.failure:
                raise self.auth_request.failure from None
            raise PreflightFailure(stage, reason="TRANSPORT_UNAVAILABLE") from None
        if response.status_code != 200:
            raise _failure(stage, response)
        try:
            if len(response.content) > 1_048_576:
                raise ValueError
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except ValueError:
            raise PreflightFailure(stage, reason="INVALID_RESPONSE") from None

    def _sheet(self):
        sheet = self._get(SHEET_URL, "sheet_metadata", {"fields": METADATA_FIELDS, "includeGridData": "false"})
        tabs = sheet.get("sheets")
        if sheet.get("spreadsheetId") != TEST_WORKBOOK_ID or not isinstance(tabs, list) or not 1 <= len(tabs) <= 32:
            raise PreflightFailure("sheet_identity_or_metadata_mismatch")
        summaries, ranges, ids = [], [], set()
        for tab in tabs:
            props = tab.get("properties", {}) if isinstance(tab, dict) else {}
            identifier, title = props.get("sheetId"), props.get("title")
            grid = props.get("gridProperties", {})
            if (type(identifier) is not int or identifier < 0 or identifier in ids or not isinstance(title, str)
                    or not 1 <= len(title) <= 100 or any(ord(char) < 32 for char in title)
                    or type(grid.get("rowCount")) is not int or type(grid.get("columnCount")) is not int
                    or grid["rowCount"] < 1 or grid["columnCount"] < 1):
                raise PreflightFailure("sheet_tab_metadata_invalid")
            ids.add(identifier)
            ranges.append("'" + title.replace("'", "''") + "'!A1:E1")
            protections = tab.get("protectedRanges", [])
            if not isinstance(protections, list):
                raise PreflightFailure("sheet_protection_metadata_invalid")
            summaries.append({"sheet_id": identifier, "title": title, "row_count": grid["rowCount"],
                              "column_count": grid["columnCount"], "protected_range_count": len(protections)})
        self.report.update(sheet_metadata_readable=True, sheet_tab_count=len(tabs),
                           sheet_timezone=sheet.get("properties", {}).get("timeZone"), tabs=summaries)
        headers = self._get(SHEET_URL, "sheet_header_schema", [("fields", HEADER_FIELDS)] + [("ranges", value) for value in ranges])
        if headers.get("spreadsheetId") != TEST_WORKBOOK_ID or not isinstance(headers.get("sheets"), list):
            raise PreflightFailure("sheet_header_identity_mismatch")
        received = set()
        for tab in headers["sheets"]:
            props = tab.get("properties", {})
            identifier = props.get("sheetId")
            if type(identifier) is not int or identifier not in ids or identifier in received:
                raise PreflightFailure("sheet_header_tab_mismatch")
            received.add(identifier)
            summary = next(value for value in summaries if value["sheet_id"] == identifier)
            blocks = tab.get("data", [])
            values = []
            hidden = None
            for block in blocks:
                if block.get("startRow", 0) != 0 or block.get("startColumn", 0) != 0:
                    raise PreflightFailure("sheet_header_range_mismatch")
                rows = block.get("rowData", [])
                if len(rows) > 1:
                    raise PreflightFailure("sheet_header_range_mismatch")
                if rows:
                    values = [cell.get("effectiveValue", {}).get("stringValue") for cell in rows[0].get("values", [])]
                columns = block.get("columnMetadata", [])
                if len(columns) >= 5:
                    hidden = columns[4].get("hiddenByUser", False)
            summary.update(expense_headers_match=values[:4] == ["Date", "List", "Cost", "Remark"],
                           database_headers_match=values[:3] == ["Item", "Categories", "Sub-categories"],
                           receipt_e_header_match=len(values) >= 5 and values[4] == "transaction_id", receipt_e_hidden=hidden)
        if received != ids:
            raise PreflightFailure("sheet_header_coverage_incomplete")
        self.report["sheet_header_schema_readable"] = True

    def run(self):
        try:
            self._load()
            self._sheet()
            database = self._get(FIRESTORE_URL, "firestore_database_metadata")
            if (database.get("name") != "projects/" + PROJECT_ID + "/databases/(default)"
                    or database.get("type") != "FIRESTORE_NATIVE" or database.get("locationId") != "asia-southeast3"):
                raise PreflightFailure("firestore_database_metadata_mismatch")
            self.report.update(status="verified_readonly", firestore_metadata_readable=True,
                               firestore_location="asia-southeast3", firestore_mode="FIRESTORE_NATIVE")
        except PreflightFailure as failure:
            self.report.update(status="blocked", error=failure.summary)
        except Exception:
            self.report.update(status="blocked", error=PreflightFailure("metadata_response_invalid").summary)
        finally:
            self.client.close()
            self.credentials = None
        return self.report
