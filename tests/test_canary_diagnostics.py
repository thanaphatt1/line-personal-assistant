"""Signed local canary diagnostics and safe short-message canonicalization."""

from copy import deepcopy
from datetime import timedelta
import json
import logging

from fastapi.testclient import TestClient
import pytest

from line_assistant.canary import CanaryGate
from line_assistant.main import signature
from line_assistant.model_api import request_body
from line_assistant.state import MemoryState
from test_canary import CanaryApi, app, canary_event, spec
from test_cloud_run import NOW, OWNER, send


def diagnostics(caplog):
    return [json.loads(record.getMessage().removeprefix("canary_diagnostic "))
            for record in caplog.records if record.getMessage().startswith("canary_diagnostic ")]


def gate(now=NOW):
    return CanaryGate(spec(), MemoryState(), OWNER, lambda: now)


@pytest.mark.parametrize("field,value,reason", [
    (("source", "type"), "group", "source_not_user"),
    (("source", "userId"), "U" + "b" * 32, "owner_mismatch"),
    (("type",), "follow", "event_not_message"),
    (("mode",), "standby", "mode_not_active"),
    (("message", "type"), "image", "message_not_text"),
    (("message", "text"), "wrong literal", "literal_mismatch"),
    (("webhookEventId",), "bad/id", "invalid_event_id"),
    (("timestamp",), True, "invalid_timestamp"),
    (("timestamp",), 10**40, "invalid_timestamp"),
    (("message", "id"), [], "invalid_message_id"),
    (("replyToken",), "", "invalid_reply_token"),
    (("timestamp",), int((spec().starts_at - timedelta(milliseconds=1)).timestamp() * 1000), "event_outside_window"),
])
def test_specific_rejection_code_has_no_state_or_provider_access(field, value, reason, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    ev = canary_event()
    target = ev
    for part in field[:-1]:
        target = target[part]
    target[field[-1]] = value
    api = CanaryApi()
    with TestClient(app(api)) as client:
        assert send(client, ev).status_code == 200
    assert diagnostics(caplog)[0]["reason_code"] == reason
    assert api.metadata == api.models == api.lines == api.google.sheet_writes == 0
    assert not api.google.documents and not api.google.native_calls


def test_signed_empty_verify_and_message_events_are_distinguishable(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    api = CanaryApi()
    with TestClient(app(api)) as client:
        body = b'{"events":[]}'
        assert client.post("/webhooks/line", content=body, headers={
            "x-line-signature": signature(body, "synthetic-line-channel-secret")}).status_code == 200
        body = json.dumps({"events": [canary_event(owner="U" + "b" * 32),
                                      canary_event(text="unmatched")]}).encode()
        assert client.post("/webhooks/line", content=body, headers={
            "x-line-signature": signature(body, "synthetic-line-channel-secret")}).status_code == 200
    records = diagnostics(caplog)
    assert [r["reason_code"] for r in records] == ["empty_events", "owner_mismatch", "literal_mismatch"]
    assert [r["event_count"] for r in records] == [0, 2, 2]
    assert [r["event_type"] for r in records] == ["none", "message", "message"]
    assert records[1]["owner_match"] is False and records[1]["literal_match"] is True
    assert records[2]["owner_match"] is True and records[2]["literal_match"] is False
    assert api.metadata == api.models == api.lines == api.google.sheet_writes == 0


@pytest.mark.parametrize("now,status", [(spec().starts_at - timedelta(seconds=1), "before_window"),
                                       (spec().expires_at, "expired")])
def test_receive_window_rejection_logs_only_window_status(now, status, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    api = CanaryApi()
    with TestClient(app(api, clock=lambda: now)) as client:
        assert send(client, canary_event()).status_code == 200
    record = diagnostics(caplog)[0]
    assert record["reason_code"] == "window_not_active" and record["canary_window_status"] == status
    assert api.metadata == api.models == api.lines == api.google.sheet_writes == 0


def test_unverified_or_malformed_envelope_has_no_canary_payload_diagnostics(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    api = CanaryApi()
    with TestClient(app(api)) as client:
        assert send(client, canary_event(), secret="wrong").status_code == 401
        for body in (b'{"events":[null]}', b'{"events":{}}', b'invalid'):
            assert client.post("/webhooks/line", content=body, headers={
                "x-line-signature": signature(body, "synthetic-line-channel-secret")}).status_code == 400
    assert diagnostics(caplog) == [] and api.metadata == 0


def test_safe_records_emit_under_existing_server_warning_level(caplog):
    caplog.set_level(logging.INFO)
    server_logger = logging.getLogger("uvicorn.error")
    prior = server_logger.level
    try:
        server_logger.setLevel(logging.WARNING)
        gate().observe_verified_events([])
    finally:
        server_logger.setLevel(prior)
    assert diagnostics(caplog)[0]["reason_code"] == "empty_events"


def test_untrusted_values_are_never_rendered_in_safe_diagnostics(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    private = "PRIVATE_SENTINEL_MESSAGE_TOKEN_HEADER_ID"
    ev = {"type": private, "source": {"type": "user", "userId": private},
          "message": {"type": "text", "text": private, "id": private},
          "replyToken": private, "webhookEventId": private, "timestamp": private,
          "headers": {"secret": private}, "extra": private}
    observer = gate()
    observer.observe_verified_events([ev])
    ev["type"] = [private]
    observer.observe_verified_events([ev])
    records = diagnostics(caplog)
    assert all(r["event_type"] == "other" and r["reason_code"] == "owner_mismatch" for r in records)
    assert all(set(r) == {"reason_code", "event_type", "event_count", "owner_match", "literal_match",
                          "literal_exact_match", "canary_window_status"} for r in records)
    assert private not in caplog.text and OWNER not in caplog.text and spec().nonce not in caplog.text
    assert "synthetic-line-channel-secret" not in caplog.text
    observer.observe_verified_events([{}])
    empty_event = diagnostics(caplog)[-1]
    assert empty_event["reason_code"] == "source_not_user"
    assert empty_event["event_type"] == "other" and empty_event["event_count"] == 1


@pytest.mark.parametrize("style", ["short", "short_whitespace", "full_whitespace"])
def test_short_or_whitespace_message_uses_frozen_model_input_and_one_effect_each(style, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    text = spec().user_message if style.startswith("short") else spec().message
    if style.endswith("whitespace"):
        text = "\n\t" + text.replace(" ", "\n  \t") + " \r\n"
    ev = canary_event(text=text)
    api = CanaryApi()
    with TestClient(app(api)) as client:
        assert send(client, ev).status_code == 200
    rows_before = deepcopy(api.google.cells)
    with TestClient(app(api)) as client:
        assert send(client, ev).status_code == 200
        assert send(client, canary_event(text=text, event_id="different-event")).status_code == 200
    assert api.bodies == [request_body(spec().message, NOW)]
    assert api.models == api.lines == api.google.sheet_writes == 1
    assert api.google.cells == rows_before
    rows = [row for row in range(2, 13) if api.google.cells.get(("All Expenses", row, 2)) == "TEST Coffee"]
    assert len(rows) == 1
    assert api.google.cells[("All Expenses", rows[0], 3)] == 1
    assert api.google.cells[("All Expenses", rows[0], 4)] == spec().note
    assert api.google.cells[("All Expenses", rows[0], 5)]
    codes = [r["reason_code"] for r in diagnostics(caplog)]
    assert codes[0] == ("accepted" if style == "short" else "accepted_normalized")
    assert "binding_mismatch" in codes


@pytest.mark.parametrize("text", [
    "TEST Coffee 2 วันที่ 2026-10-11", "TEST Coffee 1 วันที่ 2026-10-10",
    "test Coffee 1 วันที่ 2026-10-11", "TEST Coffee 1", "TEST Coffee 1 บาท",
    "TEST Coffee 1 วันที่ 2026-10-11 หมายเหตุ invented", "TEST\u200b Coffee 1 วันที่ 2026-10-11",
])
def test_short_style_does_not_broaden_expense_amount_date_or_note(text):
    api = CanaryApi()
    with TestClient(app(api)) as client:
        assert send(client, canary_event(text=text)).status_code == 200
    assert api.metadata == api.models == api.lines == api.google.sheet_writes == 0
    assert not api.google.documents


def test_short_style_still_rejects_incorrect_model_note_after_consuming_one_attempt():
    from test_model_api import response
    from test_canary import arguments, snapshot
    api = CanaryApi()
    args = arguments()
    args["entries"][0]["note"] = ""
    api.value = response(args=args)
    with TestClient(app(api)) as client:
        assert send(client, canary_event(text=spec().user_message)).status_code == 200
    assert api.models == 1 and api.lines == api.google.sheet_writes == 0
    assert snapshot(api)["canary"]["model_attempted"] is True
    assert snapshot(api)["canary"]["append_attempted"] is False


def test_disabled_factory_does_not_load_secrets_or_observe_payloads(caplog):
    from line_assistant.cloud_run import create_cloud_run_app
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    with TestClient(create_cloud_run_app(secret_loader=lambda _: pytest.fail("secret access"))) as client:
        assert send(client, canary_event(text=spec().user_message)).status_code == 503
        assert client.get("/health").json()["live_requests_enabled"] is False
    assert diagnostics(caplog) == []
