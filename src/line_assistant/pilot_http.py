"""Dedicated future pilot transports: fixed services/targets, bounded RPCs, no retries.

Only the separate launch factory can enable these. Existing RestClient/auth gates
are unchanged. No credential lookup, ADC, arbitrary URLs or Calendar/push routes.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from threading import RLock
from time import monotonic
from urllib.parse import quote, unquote

import httpx
from pydantic import SecretStr

from .google_auth import SCOPES
from .google_requests import identified_append_preview
from .model_api import MODEL_ID, MAX_BODY_BYTES, MAX_OUTPUT_TOKENS, request_body, response_tools
from .pilot_config import PilotConfig

from .sheet_metadata import TransactionIdMetadata
from .sheets_api import tab_range
from .transport import ApiError, LiveDisabled, NotReady, OutcomeUnknown, log_line_response


_rpc_remaining = ContextVar("pilot_rpc_remaining", default=None)

def remaining_rpc_seconds():
    scope=_rpc_remaining.get()
    return max(0.0,scope['deadline']-monotonic()) if scope is not None else None


@contextmanager
def rpc_budget(limit, *, seconds=30):
    token = _rpc_remaining.set({"remaining":limit, "deadline":monotonic()+seconds})
    try:
        yield
    finally:
        _rpc_remaining.reset(token)


def take_rpc():
    remaining = _rpc_remaining.get()
    if remaining is None or remaining["remaining"] <= 0 or monotonic() >= remaining["deadline"]:
        raise LiveDisabled("No active bounded owner RPC scope")
    remaining["remaining"] -= 1
    return max(0.001, min(12, remaining["deadline"]-monotonic()))


class PilotHttp:
    def __init__(self, config: PilotConfig, secrets, *, transport=None, clock=None):
        reply_check_enabled = config.reply_check and config.reply_check.authorized
        if not config.launch_authorized and not reply_check_enabled:
            raise LiveDisabled("Pilot launch is disabled")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise LiveDisabled("Only dedicated live pilot transport or synthetic MockTransport supported")
        if transport is None and config.mode == "canary" and config.canary is None and not reply_check_enabled:
            raise LiveDisabled("Live transport requires the reviewed canary")
        self.config, self.secrets = config, secrets
        self.client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False, timeout=12)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cached, self.lock = {}, RLock()
        self.state = None
        self.canary = None
        self.reply_check = None
        self.append_attempted_keys = set()
        self.model_attempted_events = set()
        self.calendar_attempted_keys = set()
        self.mocked = transport is not None

    def __repr__(self):
        return "PilotHttp(credentials=<redacted>, fixed_reviewed_target=True)"

    def _http(self, method, url, **kwargs):
        timeout = take_rpc()
        if self.reply_check and method == "POST" and url == "https://api.line.me/v2/bot/message/reply":
            self.reply_check.current()
            timeout = min(timeout, (self.reply_check.spec.expires_at - self.clock()).total_seconds())
            if timeout <= 0:
                raise LiveDisabled("Reply-check window ended before provider request")
        if self.canary and method == "POST" and url.startswith(("https://sheets.googleapis.com/", "https://api.openai.com/", "https://api.line.me/")):
            self.canary.current()
            timeout = min(timeout, (self.canary.spec.expires_at - self.clock()).total_seconds())
            if timeout <= 0:
                raise LiveDisabled("Canary window ended before provider request")
        started = monotonic()
        try:
            response = self.client.request(method, url, timeout=timeout, **kwargs)
        except httpx.HTTPError:
            raise OutcomeUnknown("Provider acknowledgement unavailable") from None
        category = log_line_response(response, round((monotonic() - started) * 1000)) if url == "https://api.line.me/v2/bot/message/reply" else None
        if not 200 <= response.status_code < 300:
            code = None
            if response.status_code in {409, 412}:
                code = "ABORTED"
            raise ApiError(response.status_code, code, category=category) from None
        response_limit = 950000 if self.config.state_codec_enabled and url.startswith('https://firestore.googleapis.com/') else 512 * 1024
        if len(response.content) > response_limit:
            raise OutcomeUnknown("Provider response exceeded pilot bound")
        return response

    def google_token(self, purpose):
        with self.lock:
            now = self.clock()
            if (purpose not in {"sheets", "firestore", "calendar_events", "calendar_readonly"} or now.utcoffset() is None
                    or purpose in {'calendar_events','calendar_readonly'} and not getattr(self.config, 'calendar_enabled', False)
                    or purpose=='calendar_readonly' and not getattr(self.config,'calendar_read_all_enabled',False)):
                raise NotReady("Fixed Google purpose/aware clock required")
            old = self.cached.get(purpose)
            if old and now + timedelta(seconds=60) < old[1]:
                return old[0].get_secret_value()
            self.cached.pop(purpose, None)
            base = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/"
            headers = {"Metadata-Flavor": "Google"}
            identity = self._http("GET", base + "email", headers=headers)
            if identity.headers.get("Metadata-Flavor") != "Google" or identity.text.strip() != self.config.service_account:
                raise NotReady("Attached account identity mismatch")
            response = self._http("GET", base + "token", params={"scopes": SCOPES[purpose]}, headers=headers)
            value = self._json(response)
            token, lifetime = value.get("access_token"), value.get("expires_in")
            if (response.headers.get("Metadata-Flavor") != "Google" or value.get("token_type") != "Bearer"
                    or type(lifetime) is not int or not 60 < lifetime <= 3600
                    or not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(not 33 <= ord(c) <= 126 for c in token)):
                raise NotReady("Invalid metadata token response")
            self.cached[purpose] = (SecretStr(token), now + timedelta(seconds=lifetime))
            return token

    @staticmethod
    def _json(response):
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except ValueError:
            raise OutcomeUnknown("Invalid provider JSON acknowledgement") from None

    def allowed(self, service, method, path, body, params):
        if self.config.reply_check and self.config.reply_check.authorized:
            if service not in {"firestore", "line"}:
                raise LiveDisabled("Reply check cannot access model, Sheets or Calendar")
            if service == "line":
                if (self.reply_check is None or method != "POST" or path != "/v2/bot/message/reply"
                        or params is not None):
                    raise LiveDisabled("Reply check only allows its fixed reply route")
                self.reply_check.authorize_body(body)
                return
        if service == "sheets":
            if self.config.expense_backend != 'sheets':
                raise LiveDisabled('Sheets is outside the selected expense backend')
            target = self.config.sheet_target()
            sheet_base = "/v4/spreadsheets/" + target.spreadsheet_id
            if method == "GET" and path == sheet_base and body is None:
                if any(word in (params or {}).get("fields", "") for word in ("rowData", "effectiveValue", "userEnteredValue")):
                    raise LiveDisabled("Metadata endpoint cannot read cell content")
                return
            range_match = re.fullmatch(re.escape(sheet_base + "/values/") + "(.+)", path)
            cells = re.fullmatch(r"'(All Expenses|Database)'!A1:([A-Z]+)([0-9]+)", unquote(range_match[1])) if range_match else None
            allowed_columns = {"D", "E", "X"} if self.config.mode == "production" else {"D", "E"}
            valid_range = (cells and 2 <= int(cells[3]) <= target.max_rows
                           and ((cells[1] == "All Expenses" and cells[2] in allowed_columns)
                                or (cells[1] == "Database" and (cells[2] == "C" or
                                    (self.config.conversation_enabled and cells[2] == 'Z' and (params or {}).get('valueRenderOption') == 'FORMULA')))))
            if method == "GET" and valid_range and body is None:
                if (params or {}).get("valueRenderOption") in {"FORMULA", "UNFORMATTED_VALUE"} and params == {"valueRenderOption": params["valueRenderOption"], "dateTimeRenderOption": "SERIAL_NUMBER"}:
                    return
            if method == "POST" and path == sheet_base + ":batchUpdate" and params is None and self.state is not None:
                for key, action in self.state.actions.items():
                    if action.kind not in {'add_transactions', 'catalog_expense'} or action.status != "executing" or not key.startswith(self.config.owner_id + ":"):
                        continue
                    from .contracts import AddTransactions
                    if action.kind == 'catalog_expense' and (not self.config.conversation_enabled or not self.state.holder):
                        continue
                    args = AddTransactions.model_validate({'entries': action.payload['entries']})
                    if not 1 <= len(args.entries) <= 5:
                        continue
                    if self.canary:
                        run = self.canary.current()
                        if key != self.config.owner_id + ":" + run.event_id + ":add_transactions" or not self.config.canary.matches_entries(args):
                            continue
                    if self.state.workbook_id != target.spreadsheet_id or (self.config.mode == "production" and not self.state.production_ready):
                        continue
                    expected = identified_append_preview(args, target.expense_sheet_id, TransactionIdMetadata("E", schema_approved=True), key).body
                    if action.kind == 'catalog_expense':
                        from .sheets_api import SheetsApi
                        expected = SheetsApi(PilotPort(self, 'sheets'), target).enrollment_body(action.payload, key)
                    if body == expected:
                        if key in self.append_attempted_keys:
                            raise LiveDisabled("Same action append was already attempted")
                        self.append_attempted_keys.add(key)
                        return
        if service == "firestore" and self.state is not None:
            if method == "GET" and path == "/v1/" + self.state.name and body is None and params is None:
                return
            if method == "POST" and path == "/v1/" + self.state.database + "/documents:commit" and params is None:
                writes = body.get("writes", []) if isinstance(body, dict) else []
                if len(writes) == 1 and set(writes[0]) == {"update", "currentDocument"}:
                    update, condition = writes[0]["update"], writes[0]["currentDocument"]
                    fields = update.get("fields", {})
                    if (update.get("name") == self.state.name and set(fields) == {"owner_hash", "snapshot", "lease_owner", "fence", "lease_until", "commit_nonce"}
                            and fields["owner_hash"] == {"stringValue": self.state.owner_hash}
                            and (condition == {"exists": False} or (set(condition) == {"updateTime"} and isinstance(condition["updateTime"], str)))
                            and len(json.dumps(body).encode()) <= (900000 if self.config.state_codec_enabled else 300000)):
                        return
        if service == "model" and method == "POST" and path == "/v1/responses" and params is None and isinstance(body, dict):
            keys = {"model", "instructions", "input", "tools", "tool_choice", "parallel_tool_calls", "reasoning", "max_output_tokens", "store", "service_tier"}
            from .intent_api import tools as intent_tools, CURRENT_INSTRUCTIONS as intent_instructions
            accepted_tools = body.get('tools') == response_tools() or (self.config.conversation_enabled and body.get('tools') == intent_tools() and body.get('instructions') == intent_instructions)
            if self.config.domain_tool_loop_enabled and self.mocked:
                from .tool_loop import INSTRUCTIONS as loop_instructions
                from .domain_tools import tools as domain_tools
                accepted_tools = accepted_tools or (body.get('tools') == domain_tools() and body.get('instructions') == loop_instructions)
            if (set(body) == keys and body["model"] == MODEL_ID and accepted_tools
                    and body["tool_choice"] == "required" and body["parallel_tool_calls"] is False and body["store"] is False
                    and body["reasoning"] == {"effort": "none"} and body["max_output_tokens"] == MAX_OUTPUT_TOKENS and body["service_tier"] == "default"
                    and len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_BODY_BYTES):
                if self.state is not None and self.state.holder:
                    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    for event_id, run in self.state.model_runs.items():
                        if run.status == "reserved" and run.fingerprint == digest and event_id in self.state.claims:
                            if self.canary:
                                canary_run = self.canary.current()
                                if (event_id != canary_run.event_id
                                        or body != request_body(self.config.canary.message, datetime.fromtimestamp(canary_run.timestamp / 1000, timezone.utc))):
                                    continue
                            if event_id in self.model_attempted_events:
                                raise LiveDisabled("Model request was already attempted")
                            self.model_attempted_events.add(event_id)
                            return
        if service == "line" and method == "POST" and path == "/v2/bot/message/reply" and params is None:
            if ((self.config.conversation_enabled or self.config.state_codec_enabled) and isinstance(body, dict) and set(body) == {'replyToken', 'messages'}
                    and isinstance(body['messages'], list) and len(body['messages']) == 1
                    and isinstance(body['messages'][0], dict) and body['messages'][0].get('type') == 'flex'
                    and self.state is not None and self.state.holder):
                from .line_api import rendered_message, message_hash
                content = body['messages'][0]
                digest = message_hash(content)
                if any(reply.status == 'executing' and reply.content_hash == digest
                       and event_id in self.state.receipts and rendered_message(self.state.receipts[event_id].result) == content
                       for event_id, reply in self.state.replies.items()): return
            if isinstance(body, dict) and set(body) == {"replyToken", "messages"} and len(body["messages"]) == 1 and set(body["messages"][0]) == {"type", "text"} and body["messages"][0].get("type") == "text":
                content = body["messages"][0]["text"]
                if isinstance(content, str) and self.state is not None and self.state.holder:
                    digest = hashlib.sha256(content.encode()).hexdigest()
                    if self.canary:
                        run = self.canary.current()
                        reply, receipt = self.state.replies.get(run.event_id), self.state.receipts.get(run.event_id)
                        action = self.state.actions.get(self.config.owner_id + ":" + run.event_id + ":add_transactions")
                        if (not reply or reply.status != "executing" or reply.content_hash != digest
                                or not receipt or receipt.fingerprint != run.fingerprint or receipt.result.status != "recorded"
                                or len(receipt.result.data.get("refs", ())) != 1 or receipt.result.message != content
                                or not action or action.status != "completed" or action.result != receipt.result):
                            raise LiveDisabled("Only the recorded canary expense can authorize a reply")
                        from .contracts import AddTransactions
                        if not self.config.canary.matches_entries(AddTransactions.model_validate(action.payload)):
                            raise LiveDisabled("Canary reply action changed")
                    if any(reply.status == "executing" and reply.content_hash == digest for reply in self.state.replies.values()):
                        return
        raise LiveDisabled("Provider/route/body is outside fixed expense-only pilot")

    def request(self, service, method, path, *, body=None, params=None, headers=None):
        self.allowed(service, method, path, body, params)
        if headers:
            raise LiveDisabled("Pilot owns request headers")
        if self.reply_check and service == "line":
            self.reply_check.reserve_attempt("reply")
        if self.canary and method == "POST" and service in {"model", "sheets", "line"}:
            if self.state is None or not self.state.holder:
                raise LiveDisabled("Canary effect requires the active owner lease")
            self.canary.reserve_attempt({"model": "model", "sheets": "append", "line": "reply"}[service])
        origins = {"sheets": "https://sheets.googleapis.com", "firestore": "https://firestore.googleapis.com",
                   "model": "https://api.openai.com", "line": "https://api.line.me", "calendar": "https://www.googleapis.com"}
        purpose='calendar_readonly' if service=='calendar' and method=='GET' and getattr(self.config,'calendar_read_all_enabled',False) else 'calendar_events' if service=='calendar' else service
        token = self.google_token(purpose) if service in {"sheets", "firestore", "calendar"} else self.secrets["openai-api-key" if service == "model" else "line-channel-access-token"].get_secret_value()
        response = self._http(method, origins[service] + path, json=body, params=params, headers={"Authorization": "Bearer " + token})
        return self._json(response)

    def close(self):
        self.cached.clear()
        self.secrets = {}
        self.client.close()


class PilotPort:
    def __init__(self, http, service):
        self.http, self.service, self.mocked = http, service, http.mocked

    def request(self, method, path, **kwargs):
        return self.http.request(self.service, method, path, **kwargs)
