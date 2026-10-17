from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

from line_assistant.canary import CanaryGate
from line_assistant.cloud_run import create_cloud_run_app
from line_assistant.model_api import BudgetBlocked, DurableModelGuard, RESERVATION_MICRO_USD, request_body
from line_assistant.persistence import SQLiteState, restore_state, state_snapshot
from line_assistant.pilot_config import PilotConfig
from line_assistant.runtime_config import ConfigError
from line_assistant.state import Claim, MemoryState
from line_assistant.transport import LiveDisabled
from test_cloud_run import HostedApi, NOW, OWNER, event, fake_secrets, launch_values, send
from test_model_api import response


NONCE = "c7" * 16  # Explicit synthetic test fixture, never an actual launch nonce.


def values():
    return {**launch_values(), "PILOT_MODEL_MONTHLY_MICRO_USD": str(RESERVATION_MICRO_USD),
            "PILOT_MODEL_MAX_CALLS_DAY": "1", "PILOT_MODEL_MAX_CALLS_MONTH": "1",
            "PILOT_CANARY_NONCE": NONCE, "PILOT_CANARY_DATE": "2026-10-11",
            "PILOT_CANARY_START_AT": "2026-10-11T01:59:30Z",
            "PILOT_CANARY_EXPIRES_AT": "2026-10-11T02:09:30Z"}


def spec():
    return PilotConfig.from_mapping(values()).canary


def arguments():
    return {"entries": [{"item": "TEST Coffee", "amount": "1.00", "kind": "expense",
                         "date": "2026-10-11", "note": spec().note}]}


def canary_event(text=None, **kwargs):
    return event(text=spec().message if text is None else text, **kwargs)


def snapshot(api):
    return json.loads(next(iter(api.google.documents.values()))["fields"]["snapshot"]["stringValue"])


class Interrupted(BaseException):
    """Trusted fault injection that bypasses normal provider exception handlers."""


class CanaryApi(HostedApi):
    def __init__(self):
        super().__init__()
        self.google.cells[("Database", 6, 1)] = "TEST Coffee"
        self.google.cells[("Database", 6, 2)] = "Synthetic"
        self.value = response(args=arguments())
        self.fault = None
        self.after_model = None

    def handle(self, request):
        effect = None
        if request.url.host == "api.openai.com":
            effect = "model"
        elif request.url.host == "api.line.me":
            effect = "reply"
        elif request.url.host == "sheets.googleapis.com" and request.method == "POST":
            effect = "append"
        if effect:
            stored = snapshot(self)
            assert stored["canary"][effect + "_attempted"] is True
            assert stored["canary"]["event_id"] == "pilot-event"
            assert stored["canary"]["spec_hash"] == spec().fingerprint
            if effect == "append":
                assert len(json.loads(request.content)["requests"][0]["appendCells"]["rows"]) == 1
            if self.fault == effect + "_timeout":
                self.fault = None
                if effect == "model":
                    self.models += 1
                elif effect == "reply":
                    self.lines += 1
                raise httpx.ReadTimeout("Synthetic acknowledgement unavailable", request=request)
        result = super().handle(request)
        if effect == "model" and self.after_model:
            self.after_model()
        if effect and self.fault == effect + "_crash":
            self.fault = None
            raise Interrupted()
        return result


def app(api, *, clock=None, config=None):
    return create_cloud_run_app(config or values(), transport=httpx.MockTransport(api.handle),
                                secret_loader=fake_secrets, clock=clock or (lambda: NOW))


def test_live_launch_without_complete_gate_fails_before_secret_loading():
    with pytest.raises(ConfigError, match="canary"):
        create_cloud_run_app(launch_values(), secret_loader=lambda _: pytest.fail("Loaded secret"))


@pytest.mark.parametrize("key,value", [
    ("PILOT_CANARY_NONCE", ""), ("PILOT_CANARY_NONCE", "x" * 32),
    ("PILOT_CANARY_DATE", "2026-02-30"), ("PILOT_CANARY_DATE", "2026-10-10"),
    ("PILOT_CANARY_START_AT", "2026-10-11T01:59:30"),
    ("PILOT_CANARY_START_AT", "2026-10-11T08:59:30+07:00"),
    ("PILOT_CANARY_EXPIRES_AT", "2026-10-11T02:09:31Z"),
    ("PILOT_CANARY_EXPIRES_AT", "2026-10-11T01:59:30Z"),
    ("PILOT_MODEL_MONTHLY_MICRO_USD", "500000"),
    ("PILOT_MODEL_MAX_CALLS_DAY", "2"), ("PILOT_MODEL_MAX_CALLS_MONTH", "2"),
])
def test_invalid_or_broader_gate_config_fails_before_secret_loading(key, value):
    config = dict(values(), **{key: value})
    with pytest.raises(ConfigError):
        create_cloud_run_app(config, secret_loader=lambda _: pytest.fail("Loaded secret"))


def test_explicit_future_window_is_not_started_by_bootstrap_or_first_message():
    api = CanaryApi()
    with TestClient(app(api, clock=lambda: spec().starts_at - timedelta(seconds=1))) as client:
        assert send(client, canary_event()).status_code == 200
    assert api.metadata == api.models == api.lines == api.google.sheet_writes == 0
    assert not api.google.documents


@pytest.mark.parametrize("mutation", ["owner", "group", "nontext", "follow", "standby", "different_text",
                                      "missing_token", "malformed_event_id", "malformed_message_id", "old_timestamp"])
def test_nonmatching_events_have_no_provider_or_state_calls(mutation):
    ev = canary_event()
    if mutation == "owner": ev["source"]["userId"] = "U" + "b" * 32
    elif mutation == "group": ev["source"]["type"] = "group"
    elif mutation == "nontext": ev["message"]["type"] = "image"
    elif mutation == "follow": ev["type"] = "follow"
    elif mutation == "standby": ev["mode"] = "standby"
    elif mutation == "different_text": ev["message"]["text"] += " changed"
    elif mutation == "missing_token": ev.pop("replyToken")
    elif mutation == "malformed_event_id": ev["webhookEventId"] = "bad/event"
    elif mutation == "malformed_message_id": ev["message"]["id"] = []
    else: ev["timestamp"] = int((spec().starts_at - timedelta(milliseconds=1)).timestamp() * 1000)
    api = CanaryApi()
    with TestClient(app(api)) as client:
        assert send(client, ev).status_code == 200
    assert api.metadata == api.models == api.lines == api.google.sheet_writes == 0
    assert not api.google.documents and not api.google.native_calls


def test_signature_and_signed_empty_verification_do_not_authorize_effects():
    from line_assistant.main import signature
    api = CanaryApi()
    with TestClient(app(api)) as client:
        assert send(client, canary_event(), secret="wrong").status_code == 401
        body = b'{"events":[]}'
        assert client.post("/webhooks/line", content=body,
                           headers={"x-line-signature": signature(body, "synthetic-line-channel-secret")}).json() == {"ok": True}
    assert api.metadata == api.models == api.lines == api.google.sheet_writes == 0


def test_single_frozen_expense_new_controller_replay_and_changed_event_do_not_repeat():
    api = CanaryApi()
    with TestClient(app(api)) as client:
        assert send(client, canary_event()).json() == {"ok": True}
    before = deepcopy(api.google.cells)
    with TestClient(app(api)) as client:
        assert send(client, canary_event()).status_code == 200
        assert send(client, canary_event(event_id="another-event")).status_code == 200
        changed = canary_event(); changed["message"]["id"] = "changed-id"
        assert send(client, changed).status_code == 200
    assert api.models == api.lines == api.google.sheet_writes == 1 and api.google.cells == before
    stored = snapshot(api)
    assert all(stored["canary"][kind + "_attempted"] for kind in ("model", "append", "reply"))
    assert stored["model_budgets"]["2026-10"]["reserved_micro_usd"] == RESERVATION_MICRO_USD
    assert stored["replies"]["pilot-event"]["status"] == "sent"
    rows = [row for row in range(2, 13) if api.google.cells.get(("All Expenses", row, 2)) == "TEST Coffee"]
    assert len(rows) == 1 and api.google.cells[("All Expenses", rows[0], 3)] == 1
    assert api.google.cells[("All Expenses", rows[0], 4)] == spec().note
    assert api.google.cells[("All Expenses", rows[0], 5)]


@pytest.mark.parametrize("change", ["duplicate_entries", "changed_note", "changed_date", "changed_amount",
                                    "changed_item", "summary", "clarification", "wrong_model", "missing_usage",
                                    "extra_function", "untrusted_field"])
def test_unexpected_model_output_consumes_attempt_but_never_appends_or_replies(change):
    api = CanaryApi(); args = arguments()
    if change == "duplicate_entries": args["entries"] *= 2
    elif change == "changed_note": args["entries"][0]["note"] = "invented"
    elif change == "changed_date": args["entries"][0]["date"] = "2026-10-10"
    elif change == "changed_amount": args["entries"][0]["amount"] = "2026"
    elif change == "changed_item": args["entries"][0]["item"] = "Coffee"
    elif change == "untrusted_field": args["entries"][0]["category"] = "Transportation"
    api.value = response(args=args)
    if change == "summary": api.value = response("spending_summary", {"start_date": "2026-10-11", "end_date": "2026-10-11"})
    elif change == "clarification": api.value = response("clarify_expense", {"reason": "ambiguous"})
    elif change == "wrong_model": api.value["model"] = "other-model"
    elif change == "missing_usage": api.value.pop("usage")
    elif change == "extra_function": api.value["output"] *= 2
    for _ in range(2):
        with TestClient(app(api)) as client:
            assert send(client, canary_event()).status_code == 200
    assert api.models == 1 and api.lines == api.google.sheet_writes == 0
    assert snapshot(api)["canary"]["model_attempted"] is True
    assert snapshot(api)["canary"]["append_attempted"] is False
    assert not any(host == "sheets.googleapis.com" for _, host, _ in api.google.native_calls)


@pytest.mark.parametrize("phase,expected_first", [("canary_bound", (0, 0, 0)), ("prepared", (1, 0, 0)),
                                                 ("executing", (1, 0, 0)), ("effect_returned", (1, 1, 0)),
                                                 ("receipt_completed", (1, 1, 0))])
def test_checkpoint_interruption_preserves_binding_and_each_effect_allowance(phase, expected_first):
    api = CanaryApi(); first = app(api)
    def interrupt(actual, _):
        if actual == phase: raise Interrupted()
    first.state.assistant.phase_hook = interrupt
    try:
        with pytest.raises(Interrupted):
            first.state.assistant.handle(canary_event())  # Trusted fault harness; HMAC tested separately.
    finally:
        first.state.pilot_http.close()
    assert (api.models, api.google.sheet_writes, api.lines) == expected_first
    stored = snapshot(api); original = deepcopy(stored["canary"])
    with TestClient(app(api)) as client:
        assert send(client, canary_event()).status_code == 200
        assert send(client, canary_event(event_id="changed-event")).status_code == 200
    assert api.models <= 1 and api.google.sheet_writes <= 1 and api.lines <= 1
    assert snapshot(api)["canary"]["spec_hash"] == original["spec_hash"]
    assert snapshot(api)["canary"]["event_id"] == original["event_id"]
    if phase == "executing":
        assert api.google.sheet_writes == api.lines == 0  # Absence is not proof of no prior write.
    else:
        assert api.models == api.google.sheet_writes == api.lines == 1


@pytest.mark.parametrize("fault", ["model_crash", "append_crash", "reply_crash"])
def test_interruption_after_provider_boundary_never_retries_that_effect(fault):
    api = CanaryApi(); api.fault = fault
    first = app(api)
    try:
        with pytest.raises(Interrupted):
            result = first.state.assistant.handle(canary_event())
            first.state.pilot_replies.deliver(canary_event(), result, NOW)
    finally:
        first.state.pilot_http.close()
    with TestClient(app(api)) as client:
        assert send(client, canary_event()).status_code == 200
    assert api.models == 1 and api.google.sheet_writes <= 1 and api.lines <= 1
    if fault == "model_crash": assert api.google.sheet_writes == api.lines == 0
    elif fault == "append_crash": assert api.google.sheet_writes == api.lines == 1
    else:
        assert api.lines == 1 and snapshot(api)["replies"]["pilot-event"]["status"] == "uncertain"


@pytest.mark.parametrize("fault", ["model_timeout", "reply_timeout", "append_ack_lost", "append_receipt_missing"])
def test_lost_acknowledgements_keep_allowances_and_require_positive_sheet_proof(fault):
    api = CanaryApi()
    if fault.startswith("append"):
        api.google.fail_after_sheet_write = True
        api.google.missing_id_after_append = fault == "append_receipt_missing"
    else: api.fault = fault
    with TestClient(app(api)) as client:
        assert send(client, canary_event()).status_code in {200, 503}
    with TestClient(app(api)) as client:
        assert send(client, canary_event()).status_code == 200
    assert api.models == 1 and api.google.sheet_writes <= 1 and api.lines <= 1
    if fault == "model_timeout": assert api.google.sheet_writes == api.lines == 0
    elif fault == "append_receipt_missing": assert api.google.sheet_writes == 1 and api.lines == 0
    else: assert api.google.sheet_writes == api.lines == 1


def test_expiry_after_model_response_blocks_append_and_reply_and_recreated_controller():
    now = [NOW]; api = CanaryApi(); api.after_model = lambda: now.__setitem__(0, spec().expires_at)
    with TestClient(app(api, clock=lambda: now[0])) as client:
        assert send(client, canary_event()).status_code == 503
    calls = len(api.google.native_calls)
    with TestClient(app(api, clock=lambda: now[0])) as client:
        assert send(client, canary_event()).status_code == 200
    assert api.models == 1 and api.google.sheet_writes == api.lines == 0
    assert len(api.google.native_calls) == calls


def test_new_nonce_or_extended_window_cannot_reset_the_durable_canary():
    api = CanaryApi()
    with TestClient(app(api)) as client: assert send(client, canary_event()).status_code == 200
    saved = deepcopy(snapshot(api)["canary"])
    for updated in (dict(values(), PILOT_CANARY_NONCE="d8" * 16),
                    dict(values(), PILOT_CANARY_START_AT="2026-10-11T02:00:00Z", PILOT_CANARY_EXPIRES_AT="2026-10-11T02:10:00Z")):
        changed_spec = PilotConfig.from_mapping(updated).canary
        with TestClient(app(api, config=updated)) as client:
            assert send(client, event(text=changed_spec.message)).status_code == 200
    assert api.models == api.google.sheet_writes == api.lines == 1
    assert snapshot(api)["canary"] == saved


@pytest.mark.parametrize("kind", ["budget", "model", "append", "reply"])
def test_actual_process_exit_after_checkpoint_preserves_allowance_and_expiry(tmp_path, kind):
    path = tmp_path / "state.sqlite3"
    script = """import json,os,sys
sys.path.insert(0,sys.argv[5])
from datetime import datetime,timedelta
from line_assistant.canary import CanaryGate
from line_assistant.model_api import DurableModelGuard,request_body
from line_assistant.persistence import SQLiteState
from line_assistant.pilot_config import PilotConfig
from line_assistant.state import Claim
config=PilotConfig.from_mapping(json.loads(sys.argv[2]));event=json.loads(sys.argv[3])
now=datetime.fromisoformat('2026-10-11T02:00:00+00:00')
state=SQLiteState(sys.argv[1],config.owner_id)
with state.session():
 gate=CanaryGate(config.canary,state,config.owner_id,lambda:now)
 assert gate.bind(event)
 state.claims[event['webhookEventId']]=Claim(state.canary.fingerprint,now+timedelta(days=30))
 guard=DurableModelGuard(state,monthly_micro_usd=config.monthly_micro_usd,max_calls_day=1,max_calls_month=1,clock=lambda:now)
 guard.reserve(event['webhookEventId'],request_body(config.canary.message,now))
 if sys.argv[4]=='budget':os._exit(86)
 for stage in ('model','append','reply'):
  gate.reserve_attempt(stage)
  if stage==sys.argv[4]:os._exit(86)
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(path), json.dumps(values()), json.dumps(canary_event()), kind,
                             str(Path(__file__).parents[1] / "src")],
                            cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 86, result.stderr
    state = SQLiteState(path, OWNER)
    with state.session():
        gate = CanaryGate(spec(), state, OWNER, lambda: NOW)
        assert state.canary.expires_at == spec().expires_at and state.canary.spec_hash == spec().fingerprint
        assert state.model_budgets["2026-10"].reserved_micro_usd == RESERVATION_MICRO_USD
        assert state.model_budgets["2026-10"].calls == 1
        guard = DurableModelGuard(state, monthly_micro_usd=RESERVATION_MICRO_USD, max_calls_day=1, max_calls_month=1, clock=lambda: NOW)
        with pytest.raises(BudgetBlocked): guard.reserve("pilot-event", request_body(spec().message, NOW))
        if kind != "budget":
            assert getattr(state.canary, kind + "_attempted") is True
            with pytest.raises(LiveDisabled): gate.reserve_attempt(kind)
        else:
            assert state.canary.model_attempted is False
        state.expire(NOW + timedelta(days=90))
        if kind != "budget": assert getattr(state.canary, kind + "_attempted") is True
    restored = SQLiteState(path, OWNER)
    assert restored.canary.spec_hash == spec().fingerprint
    if kind != "budget": assert getattr(restored.canary, kind + "_attempted") is True


def test_older_snapshot_and_invalid_allowance_flags_fail_safely():
    old = state_snapshot(MemoryState(), OWNER); old.pop("canary")
    state = MemoryState(); restore_state(state, old, OWNER)
    assert state.canary is None
    gate = CanaryGate(spec(), state, OWNER, lambda: NOW); assert gate.bind(canary_event())
    malformed = state_snapshot(state, OWNER); malformed["canary"]["model_attempted"] = "false"
    with pytest.raises(ValueError, match="canary"): restore_state(MemoryState(), malformed, OWNER)
