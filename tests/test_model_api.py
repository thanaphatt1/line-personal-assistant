from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from line_assistant.model_api import (BudgetBlocked, DurableModelGuard, MODEL_ID, RESERVATION_MICRO_USD,
                                      ResponsesPlanner, request_body, response_tools)
from line_assistant.persistence import SQLiteState, state_snapshot


def response(name="add_transactions", args=None):
    return {"model": MODEL_ID, "status": "completed", "usage": {"input_tokens": 900, "output_tokens": 150},
            "output": [{"type": "function_call", "name": name, "status": "completed", "call_id": "synthetic-call",
                        "arguments": json.dumps(args or {"entries": [{"item": "BTS", "amount": "65", "kind": "expense",
                                                   "date": "2026-10-11", "note": ""}]})}]}


class ModelPort:
    def __init__(self, value=None, failure=None):
        self.value, self.failure, self.calls, self.bodies = value or response(), failure, 0, []

    def request(self, method, path, *, body):
        assert method == "POST" and path == "/v1/responses"
        self.calls += 1
        self.bodies.append(deepcopy(body))
        if self.failure:
            raise self.failure
        return deepcopy(self.value)


def wire(h, port=None, limit=RESERVATION_MICRO_USD * 10, **kwargs):
    port = port or ModelPort()
    guard = DurableModelGuard(h.service.state, monthly_micro_usd=limit, clock=lambda: h.now, **kwargs)
    h.service.planner = ResponsesPlanner(port, guard, lambda: h.service._active_event_id)
    return port, guard


def test_exact_model_strict_schemas_single_call_no_history_or_authorization_fields():
    body = request_body("BTS 65", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert body["model"] == "gpt-6-luna" and body["store"] is body["parallel_tool_calls"] is False
    assert body["reasoning"] == {"effort": "none"} and body["max_output_tokens"] == 1024
    assert not {"previous_response_id", "conversation", "include"} & set(body)
    def check(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
            for value in schema["properties"].values():
                check(value)
        if schema.get("type") == "array":
            check(schema["items"])
    for tool in response_tools():
        assert tool["strict"]
        check(tool["parameters"])
    assert "owner_id" not in json.dumps(response_tools()) and "category" not in json.dumps(response_tools()).lower()


def test_valid_expense_replay_only_calls_model_once_and_reservation_precedes_request(h):
    port, guard = wire(h)
    event = h.event("ซื้อ BTS 65 บาท")
    first = h.service.handle(event)
    assert first.status == "recorded" and h.service.handle(event) == first and port.calls == 1
    budget = h.service.state.model_budgets["2026-10"]
    assert budget.reserved_micro_usd == RESERVATION_MICRO_USD and budget.calls == 1
    assert "ซื้อ BTS 65 บาท" not in json.dumps(state_snapshot(h.service.state, "mock-owner"), ensure_ascii=False)


@pytest.mark.parametrize("change", ["multiple", "free_text", "unknown_tool", "income", "owner", "duplicate_keys", "bad_amount", "incomplete", "wrong_model", "corrected_item", "invented_date"])
def test_model_cannot_manufacture_authorization_or_unverified_expense_fields(h, change):
    value = response()
    call = value["output"][0]
    args = json.loads(call["arguments"])
    if change == "multiple":
        value["output"].append(deepcopy(call))
    elif change == "free_text":
        value["output"] = [{"type": "message", "content": "Saved already!"}]
    elif change == "unknown_tool":
        call["name"] = "delete_event"
    elif change == "owner":
        args["owner_id"] = "other"
    elif change == "duplicate_keys":
        call["arguments"] = '{"entries": [], "entries": []}'
    elif change == "income":
        args["entries"][0]["kind"] = "income"
    elif change == "bad_amount":
        args["entries"][0]["amount"] = "100"
    elif change == "incomplete":
        value["status"] = "incomplete"
    elif change == "wrong_model":
        value["model"] = "gpt-6-sol"
    elif change == "corrected_item":
        args["entries"][0]["item"] = "seven"
    elif change == "invented_date":
        args["entries"][0]["date"] = "2026-10-01"
    if change != "duplicate_keys":
        call["arguments"] = json.dumps(args)
    port, _ = wire(h, ModelPort(value))
    assert h.send("BTS 65").status == "model_unavailable"
    assert not h.service.sheets.entries and port.calls == 1


def test_raw_typo_still_requires_server_item_clarification_and_controls_do_not_call_model(h):
    args = {"entries": [{"item": "sevne", "amount": "100", "kind": "expense", "date": "2026-10-11", "note": ""}]}
    port, _ = wire(h, ModelPort(response(args=args)))
    first = h.send("sevne 100")
    assert first.status == "clarification" and first.data["match"] == "possible_typo"
    assert not h.service.sheets.entries
    token = first.data["token"]
    assert h.send(f"เลือก {token} seven").status == "recorded" and port.calls == 1


def test_unknown_name_is_not_classified_and_cancellation_is_server_owned(h):
    args = {"entries": [{"item": "unknown-shop", "amount": "65", "kind": "expense", "date": "2026-10-11", "note": ""}]}
    port, _ = wire(h, ModelPort(response(args=args)))
    assert h.send("unknown-shop 65").status == "clarification"
    assert h.send("ยกเลิก").status == "cancelled" and not h.service.sheets.entries and port.calls == 1


def test_missing_fields_use_fixed_server_question_and_do_not_write(h):
    wire(h, ModelPort(response("clarify_expense", {"reason": "missing_amount"})))
    assert h.send("ซื้อ BTS").status == "clarification" and not h.service.sheets.entries


@pytest.mark.parametrize("limit,kwargs", [(0, {}), (RESERVATION_MICRO_USD, {}), (RESERVATION_MICRO_USD*10, {"max_calls_day":1}),
                                         (RESERVATION_MICRO_USD*10, {"max_calls_month":1})])
def test_zero_budget_and_reserved_budget_daily_monthly_limits_stop_before_paid_call(h, limit, kwargs):
    port, _ = wire(h, limit=limit, **kwargs)
    first = h.send("BTS 65")
    second = h.send("BTS 65")
    assert first.status == ("model_budget_exhausted" if limit == 0 else "recorded")
    assert second.status == "model_budget_exhausted" and port.calls == (0 if limit == 0 else 1)


def test_timeout_keeps_reservation_and_same_event_never_retries(h):
    port, _ = wire(h, ModelPort(failure=TimeoutError("Synthetic failure contains fake credential")))
    event = h.event("BTS 65")
    result = h.service.handle(event)
    assert result.status == "model_unavailable" and h.service.handle(event) == result and port.calls == 1
    assert h.service.state.model_budgets["2026-10"].reserved_micro_usd == RESERVATION_MICRO_USD
    assert "credential" not in result.message and not h.service.sheets.entries


def test_process_interruption_after_reservation_restores_guard_and_never_retries_model(h, tmp_path):
    path = tmp_path / "model.sqlite"
    h.service.state = SQLiteState(path, "mock-owner")
    first, _ = wire(h, ModelPort(failure=SystemExit("synthetic interruption")))
    event = h.event("BTS 65")
    with pytest.raises(SystemExit):
        h.service.handle(event)
    h.service.state = SQLiteState(path, "mock-owner")
    second, _ = wire(h)
    assert h.service.handle(event).status == "model_budget_exhausted"
    assert first.calls == 1 and second.calls == 0 and not h.service.sheets.entries


def test_reported_usage_exceeding_bound_halts_future_paid_calls(h):
    value = response()
    value["usage"]["input_tokens"] = 8193
    port, _ = wire(h, ModelPort(value))
    assert h.send("BTS 65").status == "model_unavailable" and h.service.state.model_halted
    assert h.send("BTS 65").status == "model_budget_exhausted" and port.calls == 1


def test_backwards_budget_clock_fails_before_another_request(h):
    port, _ = wire(h)
    assert h.send("BTS 65").status == "recorded"
    h.now -= timedelta(seconds=1)
    assert h.send("BTS 65").status == "model_budget_exhausted" and port.calls == 1


def test_request_with_unbounded_utf8_text_stops_before_provider(h):
    port, _ = wire(h)
    assert h.send("ก"*2000).status == "clarification" and port.calls == 0


def test_old_version_one_snapshots_restore_without_model_fields(h, tmp_path):
    state = SQLiteState(tmp_path / "legacy.sqlite", "mock-owner")
    old = state_snapshot(state, "mock-owner")
    for key in ("model_runs", "model_budgets", "model_halted"):
        old.pop(key)
    state._restore(old)
    assert state.model_runs == state.model_budgets == {} and state.model_halted is False


def test_model_complete_then_prepared_effect_interruption_replays_saved_action_without_new_call(h, tmp_path):
    path=tmp_path/"prepared.sqlite"
    h.service.state=SQLiteState(path,"mock-owner")
    first,_=wire(h)
    ev=h.event("BTS 65")
    def interrupt(phase,key):
        if phase=="prepared":
            raise SystemExit("synthetic interruption after durable plan")
    h.service.phase_hook=interrupt
    with pytest.raises(SystemExit):
        h.service.handle(ev)
    assert first.calls==1 and not h.service.sheets.entries
    h.service.state=SQLiteState(path,"mock-owner")
    h.service.phase_hook=lambda phase,key:None
    second,_=wire(h)
    assert h.service.handle(ev).status=="recorded" and second.calls==0 and len(h.service.sheets.entries)==1


def test_summary_is_read_only_and_supported_dates_are_server_validated(h):
    port,_=wire(h,ModelPort(response("spending_summary",{"start_date":"2026-10-11","end_date":"2026-10-11"})))
    assert h.send("สรุปวันนี้").status=="summary" and port.calls==0 and not h.service.sheets.entries
