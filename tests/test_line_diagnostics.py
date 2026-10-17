"""Local-only LINE HTTP/error metadata, redaction and durable no-retry checks."""

from datetime import timedelta
import json
import logging

from fastapi.testclient import TestClient
import httpx
import pytest

from line_assistant.api_sandbox import api_sandbox
from line_assistant.line_api import LineReplyApi, ReplyDispatcher, log_reply
from line_assistant.persistence import restore_state, state_snapshot
from line_assistant.state import MemoryState, Reply
from line_assistant.transport import ApiError, RestClient, line_error_category
from test_canary import CanaryApi, app, canary_event, snapshot, spec
from test_cloud_run import send
from test_line_replies import NOW, event


PRIVATE = "PRIVATE_TOKEN_OWNER_MESSAGE_HEADER_DIAGNOSTIC"


@pytest.mark.parametrize("status,payload,expected", [
    (400, {"message": "Invalid reply token"}, "invalid_reply_token"),
    (401, {"message": PRIVATE}, "authentication_failed"),
    (403, {"message": PRIVATE}, "permission_denied"),
    (404, {}, "not_found"),
    (429, {}, "rate_limited"),
    (500, {}, "provider_server_error"),
    (400, {"details": [{"property": "messages[0].text", "message": PRIVATE}]}, "invalid_message"),
    (400, {"details": [{"property": "replyToken", "message": PRIVATE}]}, "bad_request"),
    (400, {"message": "Authentication failed due to the following reason: " + PRIVATE}, "authentication_failed"),
    (400, {"message": PRIVATE, "details": PRIVATE}, "bad_request"),
    (400, [PRIVATE], "bad_request"),
    (200, {}, None),
])
def test_bounded_classifier_returns_only_fixed_categories(status, payload, expected):
    assert line_error_category(httpx.Response(status, json=payload)) == expected


@pytest.mark.parametrize("body", [b"not json", b"x" * 8193])
def test_invalid_or_oversized_error_body_has_safe_status_fallback(body):
    assert line_error_category(httpx.Response(401, content=body)) == "authentication_failed"


def test_api_error_never_carries_unrecognized_category_or_provider_prose():
    error = ApiError(401, category=PRIVATE)
    assert error.category is None and PRIVATE not in str(error)
    with pytest.raises(ValueError, match="Invalid provider HTTP status"):
        ApiError(PRIVATE)


@pytest.mark.parametrize("status,message,category,outcome", [
    (400, "Invalid reply token", "invalid_reply_token", "rejected"),
    (401, PRIVATE, "authentication_failed", "rejected"),
    (403, PRIVATE, "permission_denied", "rejected"),
    (404, PRIVATE, "not_found", "rejected"),
    (429, PRIVATE, "rate_limited", "uncertain"),
    (500, PRIVATE, "provider_server_error", "uncertain"),
])
def test_dispatcher_persists_exact_status_category_and_elapsed_without_retry(status, message, category, outcome, caplog):
    caplog.set_level(logging.INFO)
    current = [NOW]
    assistant, _, providers = api_sandbox(clock=lambda: current[0])
    calls = []
    def failure(request):
        calls.append(1)
        assert request.headers["Content-Type"] == "application/json"
        body = json.loads(request.content)
        assert set(body) == {"replyToken", "messages"} and len(body["messages"]) == 1
        assert body["messages"][0]["type"] == "text"
        return httpx.Response(status, json={"message": message,
                              "details": [{"message": PRIVATE, "property": PRIVATE}]},
                              headers={"private-header": PRIVATE})
    dispatcher = ReplyDispatcher(LineReplyApi(RestClient("line", httpx.MockTransport(failure))), assistant)
    ev = event(); result = assistant.handle(ev)
    current[0] += timedelta(seconds=10)
    assert dispatcher.deliver(ev, result, NOW) == outcome
    reply = assistant.state.replies["purchase"]
    assert reply.http_status == status and reply.error_category == category
    assert reply.receipt_elapsed_ms == 10000 and reply.attempt_duration_ms >= 0
    assert dispatcher.deliver(ev, result, NOW) == outcome and calls == [1]
    assert providers.sheet_writes == 1
    serialized = json.dumps(providers.documents)
    assert PRIVATE not in serialized and PRIVATE not in caplog.text
    assert "ephemeral-reply-token" not in serialized and "ephemeral-reply-token" not in caplog.text
    assert '"http_status":' + str(status) in caplog.text
    restored = MemoryState()
    restore_state(restored, state_snapshot(assistant.state, "mock-owner"), "mock-owner")
    assert restored.replies["purchase"] == reply


@pytest.mark.parametrize("status,message,category", [(400, "Invalid reply token", "invalid_reply_token"),
                                                     (401, PRIVATE, "authentication_failed")])
def test_actual_pilot_http_path_keeps_one_attempt_and_exact_safe_error_metadata(status, message, category, caplog):
    caplog.set_level(logging.INFO)
    api = CanaryApi(); original = api.handle
    def failure(request):
        if request.url.host == "api.line.me":
            api.lines += 1
            assert snapshot(api)["canary"]["reply_attempted"] is True
            return httpx.Response(status, json={"message": message, "extra": PRIVATE})
        return original(request)
    api.handle = failure
    ev = canary_event(text=spec().user_message)
    with TestClient(app(api)) as client:
        assert send(client, ev).status_code == 200
    with TestClient(app(api)) as client:
        assert send(client, ev).status_code == 200
    assert api.models == api.google.sheet_writes == api.lines == 1
    stored = snapshot(api)
    assert stored["replies"]["pilot-event"]["http_status"] == status
    assert stored["replies"]["pilot-event"]["error_category"] == category
    assert stored["canary"]["reply_attempted"] is True and stored["production_ready"] is False
    assert PRIVATE not in json.dumps(stored) and PRIVATE not in caplog.text
    assert "synthetic-line-channel-access-token" not in caplog.text
    assert '"http_status":' + str(status) in caplog.text


@pytest.mark.parametrize("payload", [{}, {"sentMessages": None}, {"sentMessages": {}},
                                    {"sentMessages": [[]]}, {"sentMessages": [{"id": ""}]},
                                    {"sentMessages": [{"id": True}]}])
def test_bad_success_ack_is_uncertain_and_distinct_from_http_rejection(payload, caplog):
    caplog.set_level(logging.INFO)
    assistant, _, _ = api_sandbox(clock=lambda: NOW)
    calls = []
    def success(request):
        calls.append(1); return httpx.Response(200, json=payload)
    replies = ReplyDispatcher(LineReplyApi(RestClient("line", httpx.MockTransport(success))), assistant)
    ev = event(); result = assistant.handle(ev)
    assert replies.deliver(ev, result, NOW) == "uncertain"
    assert replies.deliver(ev, result, NOW) == "uncertain" and len(calls) == 1
    assert assistant.state.replies["purchase"].error_category == "invalid_acknowledgement"
    assert '"http_status":200' in caplog.text


def test_transport_exception_never_logs_its_private_message(caplog):
    caplog.set_level(logging.INFO)
    assistant, _, _ = api_sandbox(clock=lambda: NOW)
    def timeout(request): raise httpx.ReadTimeout(PRIVATE, request=request)
    replies = ReplyDispatcher(LineReplyApi(RestClient("line", httpx.MockTransport(timeout))), assistant)
    ev = event(); result = assistant.handle(ev)
    assert replies.deliver(ev, result, NOW) == "uncertain"
    assert assistant.state.replies["purchase"].error_category == "provider_outcome_unknown"
    assert PRIVATE not in caplog.text and "ephemeral-reply-token" not in caplog.text


def test_old_snapshot_restores_without_inventing_missing_http_evidence():
    state = MemoryState(); state.replies["old"] = Reply("hash", NOW, "rejected", NOW)
    value = state_snapshot(state, "mock-owner")
    for field in ("http_status", "error_category", "receipt_elapsed_ms", "attempt_duration_ms"):
        value["replies"]["old"].pop(field)
    restored = MemoryState(); restore_state(restored, value, "mock-owner")
    assert restored.replies["old"].status == "rejected"
    assert restored.replies["old"].http_status is None and restored.replies["old"].error_category is None


@pytest.mark.parametrize("field,value", [("http_status", True), ("http_status", PRIVATE),
                                       ("error_category", PRIVATE), ("receipt_elapsed_ms", -1),
                                       ("attempt_duration_ms", 86400001)])
def test_untrusted_persisted_metadata_is_rejected_without_printing_values(field, value):
    state = MemoryState(); state.replies["old"] = Reply("hash", NOW)
    snapshot_value = state_snapshot(state, "mock-owner"); snapshot_value["replies"]["old"][field] = value
    with pytest.raises(ValueError) as error:
        restore_state(MemoryState(), snapshot_value, "mock-owner")
    assert PRIVATE not in str(error.value)


def test_log_helper_cannot_render_untrusted_metadata(caplog):
    caplog.set_level(logging.INFO)
    reply = Reply("hash", NOW, status=PRIVATE, http_status=PRIVATE, error_category=PRIVATE,
                  receipt_elapsed_ms=PRIVATE, attempt_duration_ms=PRIVATE)
    log_reply(reply)
    assert PRIVATE not in caplog.text
