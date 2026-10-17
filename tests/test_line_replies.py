from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from line_assistant.api_sandbox import api_sandbox
from line_assistant.line_api import LineReplyApi, ReplyDispatcher, bounded_text
from line_assistant.local_runtime import durable_mock_assistant
from line_assistant.mock_api import MockApiServices
from line_assistant.state import Reply, Result
from line_assistant.transport import RestClient


NOW = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)


def event(text="BTS 65", identifier="purchase"):
    return {"type": "message", "webhookEventId": identifier, "timestamp": int(NOW.timestamp()*1000),
            "source": {"type": "user", "userId": "mock-owner"}, "replyToken": "ephemeral-reply-token",
            "message": {"type": "text", "id": identifier, "text": text}}


def test_committed_reply_lost_ack_is_not_retried_after_controller_recreation():
    first, replies, providers = api_sandbox(clock=lambda: NOW)
    message = event()
    result = first.handle(message)
    providers.fail_after_reply = True
    assert replies.deliver(message, result, NOW) == "uncertain"
    second, replies, _ = api_sandbox(services=providers, clock=lambda: NOW)
    assert replies.deliver(message, second.handle(message), NOW) == "uncertain"
    assert providers.sheet_writes == len(providers.line_replies) == 1
    assert "ephemeral-reply-token" not in json.dumps(providers.documents)


def test_sent_marker_survives_token_deadline_and_redelivery_with_a_new_token():
    current = [NOW]
    first, replies, providers = api_sandbox(clock=lambda: current[0])
    message = event()
    result = first.handle(message)
    assert replies.deliver(message, result, NOW) == "sent"
    current[0] += timedelta(minutes=2)
    second, replies, _ = api_sandbox(services=providers, clock=lambda: current[0])
    message["replyToken"] = "new-ephemeral-token"
    message["deliveryContext"] = {"isRedelivery": True}
    assert replies.deliver(message, second.handle(message), current[0]) == "sent"
    assert len(providers.line_replies) == 1


@pytest.mark.parametrize("case", ["delayed_processing", "old_redelivery", "missing_token"])
def test_unavailable_reply_tokens_do_not_create_a_push_or_retry(case):
    current = [NOW]
    first, replies, providers = api_sandbox(clock=lambda: current[0])
    message = event()
    if case == "old_redelivery":
        message["timestamp"] -= 21 * 60 * 1000
        message["deliveryContext"] = {"isRedelivery": True}
    elif case == "missing_token":
        del message["replyToken"]
    result = first.handle(message)
    if case == "delayed_processing":
        current[0] += timedelta(seconds=56)
    assert replies.deliver(message, result, NOW) == ("no_token" if case == "missing_token" else "expired")
    assert not providers.line_replies
    assert providers.sheet_writes == 1


@pytest.mark.parametrize("status,expected", [(400, "rejected"), (401, "rejected"), (429, "uncertain"), (500, "uncertain")])
def test_http_failure_is_persisted_without_reply_or_business_retries(status, expected):
    assistant, _, providers = api_sandbox(clock=lambda: NOW)
    calls = []

    def failure(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "Private diagnostic"})

    replies = ReplyDispatcher(LineReplyApi(RestClient("line", httpx.MockTransport(failure))), assistant)
    message = event()
    result = assistant.handle(message)
    assert replies.deliver(message, result, NOW) == expected
    assert replies.deliver(message, result, NOW) == expected
    assert len(calls) == 1
    assert providers.sheet_writes == 1
    assert "Private diagnostic" not in json.dumps(providers.documents)


def test_prepared_reply_survives_local_restart_without_storing_a_token(tmp_path):
    assistant = durable_mock_assistant(tmp_path / "runtime", clock=lambda: NOW)
    message, providers = event(), MockApiServices()
    result = assistant.handle(message)
    import hashlib
    with assistant.state.session():
        assistant.state.replies["purchase"] = Reply(hashlib.sha256(result.message.encode()).hexdigest(),
                                                    NOW + timedelta(days=30), token_deadline=NOW + timedelta(seconds=55))
    restarted = durable_mock_assistant(tmp_path / "runtime", clock=lambda: NOW)
    replies = ReplyDispatcher(LineReplyApi(RestClient("line", providers.transport())), restarted)
    assert replies.deliver(message, restarted.handle(message), NOW) == "sent"
    assert len(providers.line_replies) == 1
    assert "ephemeral-reply-token" not in (tmp_path / "runtime" / "controller.sqlite3").read_bytes().decode(errors="ignore")


def test_executing_marker_after_crash_is_not_retried_and_unverified_result_cannot_send(tmp_path):
    assistant = durable_mock_assistant(tmp_path / "runtime", clock=lambda: NOW)
    message, providers = event(), MockApiServices()
    result = assistant.handle(message)
    import hashlib
    with assistant.state.session():
        assistant.state.replies["purchase"] = Reply(hashlib.sha256(result.message.encode()).hexdigest(),
                                                    NOW + timedelta(days=30), "executing", NOW + timedelta(seconds=55))
    restarted = durable_mock_assistant(tmp_path / "runtime", clock=lambda: NOW)
    replies = ReplyDispatcher(LineReplyApi(RestClient("line", providers.transport())), restarted)
    assert replies.deliver(message, restarted.handle(message), NOW) == "uncertain"
    assert replies.deliver(event(identifier="unseen"), Result("recorded", "Untrusted model assertion"), NOW) == "skipped"
    assert not providers.line_replies


def test_utf16_text_limit_preserves_valid_surrogates_and_thai_text():
    assert bounded_text("กาแฟ") == "กาแฟ"
    text = bounded_text("😀" * 3000)
    assert text.endswith("…")
    assert len(text.encode("utf-16-le")) <= 10000
    assert text.encode("utf-16-le").decode("utf-16-le") == text
