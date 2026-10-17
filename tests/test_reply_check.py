"""Synthetic reply-only lifecycle; no external account or live provider requests."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from line_assistant.cloud_run import create_cloud_run_app
from line_assistant.main import signature
from line_assistant.persistence import state_snapshot
from line_assistant.pilot_config import PilotConfig
from line_assistant.pilot_http import rpc_budget
from line_assistant.readonly_preflight import PROJECT_ID
from line_assistant.reply_check import REQUEST_TEXT, RESPONSE_TEXT
from line_assistant.runtime_config import ConfigError
from line_assistant.state import CanaryRun, Claim, MemoryState, ModelBudget, Receipt, Reply, Result
from line_assistant.transport import LiveDisabled
from test_cloud_run import HostedApi, NOW, OWNER, event, fake_secrets, send


CHECK_ID = "c3" * 16


def values(**overrides):
    return {"PILOT_LINE_OWNER_ID": OWNER, "PILOT_REPLY_CHECK_AUTHORIZED": "true",
            "PILOT_REPLY_CHECK_ID": CHECK_ID, "PILOT_REPLY_CHECK_START_AT": "2026-10-11T01:59:30Z",
            "PILOT_REPLY_CHECK_EXPIRES_AT": "2026-10-11T02:04:30Z", **overrides}


def document_name(owner):
    return (f"projects/{PROJECT_ID}/databases/(default)/documents/assistant_pilot_states/"
            + hashlib.sha256(owner.encode()).hexdigest())


def probe_name(config=None):
    config = config or PilotConfig.from_mapping(values())
    return document_name(config.reply_check.state_owner(OWNER))


def snapshot(api):
    return json.loads(api.google.documents[probe_name()]["fields"]["snapshot"]["stringValue"])


class Crash(BaseException):
    pass


class ReplyApi(HostedApi):
    def __init__(self):
        super().__init__()
        self.fault = None
        self.reply_bodies = []
        self.after_commit = None

    def handle(self, request):
        if request.url.host == "api.line.me":
            self.lines += 1
            assert request.url.path == "/v2/bot/message/reply"
            assert request.method == "POST" and not request.url.query
            assert request.headers["Authorization"] == "Bearer synthetic-line-channel-access-token"
            body = json.loads(request.content)
            assert body == {"replyToken": "synthetic-reply-token",
                            "messages": [{"type": "text", "text": RESPONSE_TEXT}]}
            self.reply_bodies.append(body)
            stored = snapshot(self)
            assert stored["canary"]["reply_attempted"] is True
            assert stored["canary"]["model_attempted"] is False
            assert stored["canary"]["append_attempted"] is False
            assert stored["replies"][stored["canary"]["event_id"]]["status"] == "executing"
            assert stored["model_runs"] == stored["model_budgets"] == {}
            if self.fault == "timeout":
                raise httpx.ReadTimeout("PRIVATE_PROVIDER_TOKEN_RESPONSE", request=request)
            if self.fault == "crash":
                raise Crash()
            if self.fault == "rejected":
                return httpx.Response(401, json={"message": "PRIVATE_PROVIDER_TOKEN_RESPONSE"})
            if self.fault == "invalid_ack":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"sentMessages": [{"id": "synthetic-ack"}]})
        assert request.url.host not in {"api.openai.com", "sheets.googleapis.com"}
        response = super().handle(request)
        if request.url.host == "firestore.googleapis.com" and request.method == "POST" and self.after_commit:
            self.after_commit(self)
        return response


def app(api, *, config=None, clock=None, secret_loader=None):
    return create_cloud_run_app(config or values(), transport=httpx.MockTransport(api.handle),
                                secret_loader=secret_loader or fake_secrets, clock=clock or (lambda: NOW))


def seed_old_expense(api):
    state = MemoryState()
    expiry = NOW + timedelta(days=30)
    state.canary = CanaryRun("old-spec", "old-expense", "old-fingerprint", int(NOW.timestamp()*1000),
                            NOW - timedelta(minutes=20), NOW - timedelta(minutes=10), True, True, True)
    state.model_budgets["2026-10"] = ModelBudget(reserved_micro_usd=1536, calls=1)
    state.receipts["old-expense"] = Receipt("old-fingerprint", Result("recorded", "old receipt", {"refs": ("row-6",)}), expiry)
    state.replies["old-expense"] = Reply("old-content-hash", expiry, "rejected")
    state.claims["old-expense"] = Claim("old-fingerprint", expiry)
    name = document_name(OWNER)
    api.google.documents[name] = {"name": name, "updateTime": "2026-10-11T01:50:00Z", "fields": {
        "owner_hash": {"stringValue": hashlib.sha256(OWNER.encode()).hexdigest()},
        "snapshot": {"stringValue": json.dumps(state_snapshot(state, OWNER))},
        "lease_owner": {"stringValue": ""}, "fence": {"integerValue": "1"},
        "lease_until": {"timestampValue": "2026-10-11T01:50:00Z"}, "commit_nonce": {"stringValue": "old"}}}
    api.google.cells[("All Expenses", 6, 2)] = "TEST Coffee"
    api.google.cells[("All Expenses", 6, 3)] = 1
    return deepcopy(api.google.documents[name]), deepcopy(api.google.cells)


def test_one_reply_restarted_replay_second_event_and_prior_expense_preserved(caplog):
    api = ReplyApi(); old, cells = seed_old_expense(api)
    loaded = []
    def loader(name):
        loaded.append(name)
        return fake_secrets(name)
    ev = event(REQUEST_TEXT)
    with TestClient(app(api, secret_loader=loader)) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "reply_check"}
        assert send(client, ev).json() == {"ok": True}
    assert loaded == ["line-channel-secret", "line-channel-access-token"]
    with TestClient(app(api)) as client:
        assert send(client, ev).status_code == 200
        assert send(client, event(REQUEST_TEXT, event_id="second-event")).status_code == 200
        altered = deepcopy(ev); altered["message"]["id"] = "changed"
        assert send(client, altered).status_code == 200
    assert api.lines == 1 and api.models == api.google.sheet_writes == 0
    assert api.google.documents[document_name(OWNER)] == old
    assert api.google.cells == cells and len(api.google.documents) == 2
    assert all(path == "/v1/" + probe_name() or path.endswith("/documents:commit")
               for _, host, path in api.google.native_calls if host == "firestore.googleapis.com")
    stored = snapshot(api)
    assert stored["replies"]["pilot-event"]["status"] == "sent"
    assert stored["production_ready"] is False
    assert stored["canary"]["reply_attempted"] is True
    combined = json.dumps(api.google.documents) + caplog.text
    for private in ("synthetic-reply-token", "synthetic-line-channel-access-token", "synthetic-google-token-do-not-log"):
        assert private not in combined


@pytest.mark.parametrize("mutation", ["owner", "group", "follow", "image", "standby", "text", "spaces",
                                      "missing_token", "event_id", "message_id", "old", "future", "redelivery"])
def test_nonmatching_or_stale_events_do_not_access_any_provider(mutation):
    ev = event(REQUEST_TEXT)
    if mutation == "owner": ev["source"]["userId"] = "U" + "b" * 32
    elif mutation == "group": ev["source"]["type"] = "group"
    elif mutation == "follow": ev["type"] = "follow"
    elif mutation == "image": ev["message"]["type"] = "image"
    elif mutation == "standby": ev["mode"] = "standby"
    elif mutation == "text": ev["message"]["text"] = "BTS 65"
    elif mutation == "spaces": ev["message"]["text"] += " "
    elif mutation == "missing_token": ev.pop("replyToken")
    elif mutation == "event_id": ev["webhookEventId"] = "bad/event"
    elif mutation == "message_id": ev["message"]["id"] = []
    elif mutation == "old": ev["timestamp"] -= 31000
    elif mutation == "future": ev["timestamp"] += 6000
    else: ev["deliveryContext"] = {"isRedelivery": True}
    api = ReplyApi()
    with TestClient(app(api)) as client:
        assert send(client, ev).status_code == 200
    assert api.metadata == api.models == api.lines == api.google.sheet_writes == 0
    assert not api.google.documents and not api.google.native_calls


def test_signature_empty_verification_and_limits_precede_provider_access():
    api = ReplyApi()
    with TestClient(app(api)) as client:
        assert send(client, event(REQUEST_TEXT), secret="wrong").status_code == 401
        body = b'{"events":[]}'
        assert client.post("/webhooks/line", content=body, headers={"x-line-signature": signature(body, "synthetic-line-channel-secret")}).status_code == 200
        assert send(client, event("a" * 17000)).status_code == 413
        body = json.dumps({"events": [event(REQUEST_TEXT)] * 6}).encode()
        assert client.post("/webhooks/line", content=body, headers={"x-line-signature": signature(body, "synthetic-line-channel-secret")}).status_code == 400
    assert api.metadata == api.models == api.lines == 0 and not api.google.documents


@pytest.mark.parametrize("clock", [NOW - timedelta(seconds=31), NOW + timedelta(minutes=5)])
def test_bootstrap_does_not_start_or_extend_absolute_window(clock):
    api = ReplyApi()
    with TestClient(app(api, clock=lambda: clock)) as client:
        assert send(client, event(REQUEST_TEXT)).status_code == 200
    assert api.metadata == api.lines == 0 and not api.google.documents


@pytest.mark.parametrize("fault,status,category", [("timeout", "uncertain", "provider_outcome_unknown"),
                                                 ("rejected", "rejected", "authentication_failed"),
                                                 ("invalid_ack", "uncertain", "invalid_acknowledgement")])
def test_failure_consumes_only_reply_allowance_and_never_retries(fault, status, category, caplog):
    api = ReplyApi(); api.fault = fault; old, cells = seed_old_expense(api)
    with TestClient(app(api)) as client:
        assert send(client, event(REQUEST_TEXT)).status_code == 200
    api.fault = None
    with TestClient(app(api)) as client:
        assert send(client, event(REQUEST_TEXT)).status_code == 200
        assert send(client, event(REQUEST_TEXT, event_id="another")).status_code == 200
    stored = snapshot(api)
    assert stored["canary"]["reply_attempted"] is True
    assert stored["replies"]["pilot-event"]["status"] == status
    assert stored["replies"]["pilot-event"]["error_category"] == category
    assert api.lines == 1 and api.models == api.google.sheet_writes == 0
    assert api.google.documents[document_name(OWNER)] == old and api.google.cells == cells
    assert "PRIVATE_PROVIDER_TOKEN_RESPONSE" not in json.dumps(stored) + caplog.text


def test_crash_after_provider_boundary_restart_cannot_reply_again():
    api = ReplyApi(); api.fault = "crash"
    first = app(api)
    try:
        with pytest.raises(Crash):
            result = first.state.assistant.handle(event(REQUEST_TEXT))
            first.state.pilot_replies.deliver(event(REQUEST_TEXT), result, NOW)
    finally:
        first.state.pilot_http.close()
    assert snapshot(api)["canary"]["reply_attempted"] is True
    api.fault = None
    with TestClient(app(api)) as client:
        assert send(client, event(REQUEST_TEXT)).status_code == 200
        assert send(client, event(REQUEST_TEXT, event_id="another")).status_code == 200
    assert api.lines == 1 and snapshot(api)["replies"]["pilot-event"]["status"] == "uncertain"


def test_window_expiry_during_reserved_checkpoint_stops_before_line_io():
    current = [NOW]; api = ReplyApi()
    def advance(provider):
        if (snapshot(provider).get("canary") or {}).get("reply_attempted"):
            current[0] = NOW + timedelta(seconds=11)
    api.after_commit = advance
    config = values(PILOT_REPLY_CHECK_EXPIRES_AT="2026-10-11T02:00:10Z")
    with TestClient(app(api, config=config, clock=lambda: current[0])) as client:
        assert send(client, event(REQUEST_TEXT)).status_code == 200
    assert api.lines == 0 and snapshot(api)["canary"]["reply_attempted"] is True
    assert snapshot(api)["replies"]["pilot-event"]["status"] == "uncertain"


def test_state_commit_denial_blocks_before_line_and_keeps_original_document():
    api = ReplyApi(); old, cells = seed_old_expense(api)
    api.google.deny_firestore_commit = True
    with TestClient(app(api)) as client:
        assert send(client, event(REQUEST_TEXT)).status_code == 503
    assert api.lines == 0 and api.google.documents[document_name(OWNER)] == old
    assert api.google.cells == cells


def test_changed_window_under_same_check_id_does_not_reset_binding_or_allowance():
    api = ReplyApi()
    with TestClient(app(api)) as client:
        assert send(client, event(REQUEST_TEXT)).status_code == 200
    changed = values(PILOT_REPLY_CHECK_START_AT="2026-10-11T02:00:00Z", PILOT_REPLY_CHECK_EXPIRES_AT="2026-10-11T02:05:00Z")
    with TestClient(app(api, config=changed)) as client:
        assert send(client, event(REQUEST_TEXT, event_id="new-event")).status_code == 200
    assert api.lines == 1


@pytest.mark.parametrize("key,value", [("PILOT_REPLY_CHECK_AUTHORIZED", "yes"), ("PILOT_REPLY_CHECK_ID", ""),
    ("PILOT_REPLY_CHECK_ID", "x" * 32), ("PILOT_REPLY_CHECK_START_AT", "2026-10-11T02:00:00"),
    ("PILOT_REPLY_CHECK_START_AT", "2026-10-11T09:00:00+07:00"),
    ("PILOT_REPLY_CHECK_EXPIRES_AT", "2026-10-11T02:04:31Z"),
    ("PILOT_REPLY_CHECK_EXPIRES_AT", "2026-02-30T02:04:00Z"),
    ("PILOT_MODEL_MONTHLY_MICRO_USD", "1536"), ("PILOT_LAUNCH_AUTHORIZED", "true"),
    ("PILOT_LINE_OWNER_ID", ""), ("PILOT_MODE", "production")])
def test_invalid_or_broader_config_rejected_before_secret_loading(key, value):
    config = values(**{key: value})
    with pytest.raises(ConfigError):
        create_cloud_run_app(config, secret_loader=lambda _: pytest.fail("Secret loaded"))


def test_staged_complete_false_authorization_remains_fully_disabled():
    config = values(PILOT_REPLY_CHECK_AUTHORIZED="false")
    with TestClient(create_cloud_run_app(config, secret_loader=lambda _: pytest.fail("Secret loaded"))) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "launch_disabled", "live_requests_enabled": False}
        assert send(client, event(REQUEST_TEXT)).status_code == 503


@pytest.mark.parametrize("service,method,path,body", [
    ("model", "POST", "/v1/responses", {}), ("sheets", "GET", "/v4/spreadsheets/anything", None),
    ("sheets", "POST", "/v4/spreadsheets/anything:batchUpdate", {}),
    ("calendar", "POST", "/calendar/v3/events", {}), ("line", "POST", "/v2/bot/message/push", {}),
    ("firestore", "DELETE", "/v1/any", None), ("firestore", "GET", "/v1/other-owner", None),
    ("line", "POST", "/v2/bot/message/reply", {"replyToken": "synthetic-reply-token", "messages": [{"type": "text", "text": "changed"}]}),
])
def test_narrow_transport_refuses_other_routes_before_network(service, method, path, body):
    api = ReplyApi(); cloud = app(api)
    with TestClient(cloud), rpc_budget(10), pytest.raises(LiveDisabled):
        cloud.state.pilot_http.request(service, method, path, body=body)
    assert api.metadata == api.models == api.lines == api.google.sheet_writes == 0
