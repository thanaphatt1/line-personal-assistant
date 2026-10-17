"""One explicitly authorized synthetic expense; no general live runtime switch.

One AppendCells request maximum, one isolated Firestore snapshot document, fixed
targets/ranges, bounded RPC budget, no LINE/model/Calendar network or cleanup.
"""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from .adapters import MockCalendar
from .adc_preflight import _AuthRequest, _failure, PreflightFailure
from .config import Settings
from .contracts import AddTransactions, ToolRequest
from .firestore_state import FirestoreState
from .google_auth import SCOPES
from .google_requests import identified_append_preview
from .main import create_app, signature
from .readonly_preflight import PROJECT_ID, SERVICE_ACCOUNT, TEST_WORKBOOK_ID
from .service import Assistant
from .sheet_metadata import TransactionIdMetadata, transaction_id
from .sheets_api import SheetTarget, SheetsApi, tab_range
from .transport import ApiError, LiveDisabled, OutcomeUnknown, RestClient, NotReady


COLLECTION = "assistant_integration_tests"
SCOPED_ACCESS = (SCOPES["sheets"], SCOPES["firestore"])
MAX_API_REQUESTS = 64


class FrozenExpensePlanner:
    def __init__(self, label, arguments):
        self.label, self.arguments, self.calls = label, arguments, 0

    def plan(self, text, occurred_at):
        if text != self.label:
            raise ValueError("Only the frozen synthetic test event is permitted")
        self.calls += 1
        return ToolRequest(name="add_transactions", arguments=self.arguments)


class SyntheticGoogleSession:
    def __init__(self, run_id, *, authorized=False, transport=None, adc_loader=None):
        if not authorized:
            raise LiveDisabled("One synthetic expense requires explicit write-test authorization")
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Unique server-owned integration run ID required")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise LiveDisabled("Only the dedicated integration transport or a mock is supported")
        self.run_id, self.owner_id, self.event_id = run_id, "synthetic-owner-" + run_id, "synthetic-event-" + run_id
        self.key = self.owner_id + ":" + self.event_id + ":add_transactions"
        self.identifier = transaction_id(self.key, 0)
        self.label = "CODEX_SYNTHETIC_TEST_" + run_id
        self.database = f"projects/{PROJECT_ID}/databases/(default)"
        self.document_name = self.database + "/documents/" + COLLECTION + "/" + hashlib.sha256(self.owner_id.encode()).hexdigest()
        self.sheet_base = "/v4/spreadsheets/" + TEST_WORKBOOK_ID
        self.client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False, timeout=15)
        self.auth_request = _AuthRequest(self.client)
        self.credentials = None
        self.adc_loader = adc_loader
        self.calls = Counter()
        self.request_count = self.append_attempts = 0
        self.accepted_state_commits = 0
        self.failure = None
        self.expected_append_body = None
        self.created_state = False
        self.mocked = transport is not None

    def __repr__(self):
        return "SyntheticGoogleSession(credentials=<redacted>, general_live_startup=False)"

    def authenticate(self):
        import google.auth
        from google.auth import impersonated_credentials
        try:
            loader = self.adc_loader or google.auth.default
            credentials, _ = loader(scopes=SCOPED_ACCESS, request=self.auth_request)
            if type(credentials) is not impersonated_credentials.Credentials or credentials.signer_email != SERVICE_ACCOUNT:
                raise PreflightFailure("adc_identity_mismatch")
            credentials = credentials.with_scopes(SCOPED_ACCESS)
            credentials.refresh(self.auth_request)
            if not self.auth_request.target_mint_confirmed:
                raise PreflightFailure("identity_mint_unverified")
            self.credentials = credentials
        except PreflightFailure:
            raise
        except Exception:
            raise self.auth_request.failure or PreflightFailure("adc_load_or_refresh_failed") from None

    def freeze(self, args):
        if self.expected_append_body is not None or len(args.entries) != 1 or args.entries[0].note != self.label or args.entries[0].amount != "1.00":
            raise ValueError("Exactly one labeled THB 1.00 synthetic expense is required")
        self.expected_append_body = identified_append_preview(args, 201, TransactionIdMetadata("E", schema_approved=True), self.key).body

    def _allowed(self, service, method, path, body, params):
        if service == "sheets":
            if method == "GET" and path == self.sheet_base and body is None:
                fields = (params or {}).get("fields", "")
                if "rowData" in fields or "effectiveValue" in fields or "userEnteredValue" in fields:
                    raise NotReady("Only metadata fields may use this endpoint")
                return "sheets_metadata_get"
            ranges = {tab_range("All Expenses", "A1:D12"), tab_range("All Expenses", "A1:E12"), tab_range("Database", "A1:C12")}
            if method == "GET" and path in {self.sheet_base + "/values/" + quote(value, safe="") for value in ranges} and body is None:
                if params != {"valueRenderOption": (params or {}).get("valueRenderOption"), "dateTimeRenderOption": "SERIAL_NUMBER"} or params["valueRenderOption"] not in {"FORMULA", "UNFORMATTED_VALUE"}:
                    raise NotReady("Bounded value rendering contract required")
                return "sheets_values_get"
            if method == "POST" and path == self.sheet_base + ":batchUpdate" and params is None:
                if self.append_attempts or self.expected_append_body is None or body != self.expected_append_body:
                    raise LiveDisabled("Only one exact frozen synthetic append is authorized")
                self.append_attempts += 1  # Includes lost acknowledgements: never retry blindly.
                return "sheets_append_post"
        if service == "firestore":
            if method == "GET" and path == "/v1/" + self.document_name and body is None and params is None:
                return "firestore_document_get"
            if method == "POST" and path == "/v1/" + self.database + "/documents:commit" and params is None:
                writes = body.get("writes", []) if isinstance(body, dict) else []
                if len(writes) != 1 or set(writes[0]) != {"update", "currentDocument"}:
                    raise LiveDisabled("Only one isolated state document update is authorized")
                update = writes[0]["update"]
                fields = update.get("fields", {})
                condition = writes[0]["currentDocument"]
                if (update.get("name") != self.document_name or set(fields) != {"owner_hash", "snapshot", "lease_owner", "fence", "lease_until", "commit_nonce"}
                        or fields["owner_hash"] != {"stringValue": hashlib.sha256(self.owner_id.encode()).hexdigest()}
                        or not (condition == {"exists": False} or (set(condition) == {"updateTime"} and isinstance(condition["updateTime"], str)))
                        or len(json.dumps(body).encode()) > 65536):
                    raise LiveDisabled("State body/resource/version is outside the approved test")
                return "firestore_commit_post"
        raise LiveDisabled("Provider, route or method is outside the approved test")

    def request(self, service, method, path, *, body=None, params=None, headers=None):
        if self.failure:
            raise LiveDisabled("Stop all dependent RPCs after provider denial")
        if self.credentials is None:
            raise LiveDisabled("Expected scoped identity must authenticate first")
        if self.request_count >= MAX_API_REQUESTS:
            raise LiveDisabled("Bounded integration request budget reached")
        route = self._allowed(service, method, path, body, params)
        if headers:
            raise LiveDisabled("Integration transport owns request headers")
        origin = "https://sheets.googleapis.com" if service == "sheets" else "https://firestore.googleapis.com"
        url = origin + path
        outgoing = {}
        try:
            self.credentials.before_request(self.auth_request, method, url, outgoing)
            self.request_count += 1
            self.calls[route] += 1
            response = self.client.request(method, url, json=body, params=params, headers=outgoing)
        except httpx.HTTPError:
            raise OutcomeUnknown("Synthetic provider acknowledgement unavailable") from None
        if not 200 <= response.status_code < 300:
            failure = _failure(route, response)
            if response.status_code in {401, 403}:
                self.failure = failure
            code = failure.summary["rpc_code"]
            if response.status_code in {409, 412}:
                code = "ABORTED"
            raise ApiError(response.status_code, code)
        if route == "firestore_commit_post":
            self.accepted_state_commits += 1
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except ValueError:
            raise OutcomeUnknown("Synthetic provider returned invalid acknowledgement") from None

    def close(self):
        self.credentials = None
        self.client.close()


class SyntheticRestPort:
    """Explicit adapter port; never changes the general RestClient live gate."""
    def __init__(self, session, service):
        if service not in {"sheets", "firestore"}:
            raise LiveDisabled("Only approved Google test services are wired")
        self.session, self.service, self.mocked = session, service, session.mocked

    def request(self, method, path, **kwargs):
        return self.session.request(self.service, method, path, **kwargs)


def fixed_test_target():
    return SheetTarget(spreadsheet_id=TEST_WORKBOOK_ID, expense_sheet_id=201, database_sheet_id=202,
                       max_rows=12, writes_enabled=True, input_access_verified=True, recovery="approved_id_column",
                       transaction_id_metadata=TransactionIdMetadata("E", schema_approved=True), row_allocation="server_append_cells")


def padded(rows):
    if len(rows) > 12 or any(len(row) > 5 for row in rows):
        raise NotReady("Synthetic grid grew beyond approved bounds")
    return [(list(rows[index]) + [""] * 5)[:5] if index < len(rows) else [""] * 5 for index in range(12)]


def run_one_synthetic_expense(run_id, occurred_at, *, authorized=False, transport=None, adc_loader=None):
    session = SyntheticGoogleSession(run_id, authorized=authorized, transport=transport, adc_loader=adc_loader)
    report = {"status": "not_started", "run_id": run_id, "transaction_id": session.identifier,
              "event_id": session.event_id, "action_key": session.key,
              "firestore_document": session.document_name, "normal_live_startup_enabled": False,
              "line_requests": 0, "model_requests": 0, "calendar_requests": 0, "main_workbook_requests": 0}
    sheets = SheetsApi(SyntheticRestPort(session, "sheets"), fixed_test_target())
    try:
        session.authenticate()
        catalog = sheets.catalog()
        if not catalog.items:
            raise NotReady("Synthetic Database has no canonical fixture")
        item = catalog.match("BTS").canonical or next(iter(catalog.items.values()))
        args = AddTransactions.model_validate({"entries": [{"kind": "expense", "item": item, "amount": "1.00",
            "date": occurred_at.astimezone(ZoneInfo("Asia/Bangkok")).date().isoformat(), "note": session.label}]})
        session.freeze(args)
        baseline = padded(sheets._identified_rows())
        if any(row[4] == session.identifier for row in baseline[1:]):
            raise NotReady("Run ID already exists; inspect saved result instead of starting another test")
        planner = FrozenExpensePlanner(session.label, args.model_dump(mode="json"))
        settings = Settings(owner_id=session.owner_id)
        def controller():
            state = FirestoreState(SyntheticRestPort(session, "firestore"), session.owner_id,
                                   project_id=PROJECT_ID, collection_id=COLLECTION, max_snapshot_bytes=50000)
            adapter = SheetsApi(SyntheticRestPort(session, "sheets"), fixed_test_target(), effect_guard=state.before_effect)
            return Assistant(settings, adapter, MockCalendar(), adapter.catalog(), state=state, planner=planner)
        event = {"type": "message", "webhookEventId": session.event_id, "timestamp": int(occurred_at.timestamp() * 1000),
                 "source": {"type": "user", "userId": session.owner_id}, "message": {"type": "text", "id": session.event_id, "text": session.label}}
        body = json.dumps({"events": [event]}, ensure_ascii=False).encode()
        headers = {"x-line-signature": signature(body, settings.channel_secret)}
        def deliver(assistant):
            # In-process signed dummy webhook, with no reply dispatcher or LINE HTTP.
            with TestClient(create_app(assistant)) as client:
                response = client.post("/webhooks/line", content=body, headers=headers)
            if response.status_code != 200:
                raise OutcomeUnknown("Synthetic controller response was not acknowledged")
            return response.json()["results"][0]
        first_controller = controller()
        reconciled_before_replay = False
        try:
            first = deliver(first_controller)
        except Exception:
            if session.failure or not session.append_attempts:
                raise
            proof = sheets.reconcile(session.key, args.model_dump(mode="json"))
            if proof.outcome != "committed":
                raise OutcomeUnknown("Uncertain append lacks exact positive proof; no retry") from None
            reconciled_before_replay = True
            first = None
        if first is not None and first["status"] != "recorded":
            raise NotReady("Synthetic controller did not record the frozen expense")
        second_controller = controller()  # New controller/state loads the real durable snapshot.
        replay = deliver(second_controller)
        if replay["status"] != "recorded" or (first is not None and replay != first):
            raise OutcomeUnknown("Durable replay did not return the same recorded result")
        proof = sheets.reconcile(session.key, args.model_dump(mode="json"))
        after = padded(sheets._identified_rows())
        indices = [index for index, row in enumerate(after) if row[4] == session.identifier]
        if proof.outcome != "committed" or len(indices) != 1 or any(after[index] != baseline[index] for index in range(12) if index not in indices):
            raise OutcomeUnknown("Unique payload proof or preservation of existing rows failed")
        document = second_controller.state._read()
        stored = json.loads(document["fields"]["snapshot"]["stringValue"])
        action = stored["actions"].get(session.key, {})
        receipt = stored["receipts"].get(session.event_id, {})
        if action.get("status") != "completed" or action.get("payload") != args.model_dump(mode="json") or receipt.get("result", {}).get("status") != "recorded":
            raise OutcomeUnknown("Durable completed action/receipt payload was not proven")
        report.update(status="verified_single_synthetic_expense", row=indices[0] + 1, item=item, amount="1.00", note=session.label,
                      date=args.entries[0].date.isoformat(), replay_same_result=first is not None and replay == first,
                      positive_recovery_before_replay=reconciled_before_replay, matching_id_count=1,
                      exact_payload_verified=True, existing_rows_preserved=True, durable_action_and_receipt_verified=True,
                      firestore_write_access_verified=True, lease_released=document["fields"]["lease_owner"]["stringValue"] == "", planner_calls=planner.calls)
    except Exception:
        report.update(status="blocked_or_uncertain", error=(session.failure or session.auth_request.failure or PreflightFailure("integration_guard_or_outcome_unverified")).summary)
    finally:
        report.update(api_request_count=session.request_count, api_operations=dict(session.calls),
                      auth_requests=sum(session.auth_request.calls.values()), sheet_append_attempts=session.append_attempts,
                      firestore_commit_requests=session.calls["firestore_commit_post"], isolated_state_document_count=1 if session.calls["firestore_commit_post"] else 0)
        report["isolated_state_document_count"] = 1 if session.accepted_state_commits else 0
        report["firestore_commits_acknowledged"] = session.accepted_state_commits
        report["google_http_request_count"] = session.request_count + report["auth_requests"]
        session.close()
    return report
