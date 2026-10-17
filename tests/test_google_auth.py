from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import traceback

from fastapi.testclient import TestClient
import httpx
import pytest

from line_assistant.api_sandbox import api_sandbox
from line_assistant.google_auth import (AuthError, CloudRunTokenProvider, ImpersonatedTokenProvider,
                                        SCOPES, cloud_run_token_providers, local_impersonated_token_providers)
from line_assistant.main import create_app, signature
from line_assistant.mock_api import MockApiServices
from line_assistant.transport import LiveDisabled, RestClient


ACCOUNT = "line-assistant@synthetic-test-project.iam.gserviceaccount.com"
NOW = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)
TOKEN = "synthetic-access-DO-NOT-LOG"
CALLER = "synthetic-caller-DO-NOT-LOG"
PREFIX = "/computeMetadata/v1/instance/service-accounts/default"


class Metadata:
    def __init__(self):
        self.calls = []
        self.account = ACCOUNT
        self.headers = {"Metadata-Flavor": "Google"}
        self.status = 200
        self.body = {"access_token": TOKEN, "expires_in": 3600, "token_type": "Bearer"}

    def handle(self, request):
        self.calls.append((request.method, str(request.url)))
        assert request.url.host == "metadata.google.internal"
        assert request.method == "GET" and request.headers["Metadata-Flavor"] == "Google"
        assert "Authorization" not in request.headers
        if request.url.path == PREFIX + "/email":
            return httpx.Response(200, text=self.account, headers=self.headers)
        assert request.url.path == PREFIX + "/token"
        return httpx.Response(self.status, json=self.body, headers=self.headers)

    def provider(self, purpose="sheets", **kwargs):
        return CloudRunTokenProvider(purpose, ACCOUNT, transport=httpx.MockTransport(self.handle), **kwargs)


@pytest.mark.parametrize("purpose", ["sheets", "firestore"])
def test_cloud_run_metadata_flow_verifies_identity_and_requests_one_exact_scope(purpose):
    metadata = Metadata()
    provider = metadata.provider(purpose, clock=lambda: NOW)
    assert metadata.calls == []  # Constructor has no discovery or requests.
    assert provider() == provider() == TOKEN
    assert len(metadata.calls) == 2
    assert httpx.URL(metadata.calls[-1][1]).params["scopes"] == SCOPES[purpose]
    assert "drive" not in SCOPES[purpose] and "calendar" not in SCOPES[purpose]
    assert TOKEN not in repr(provider) and ACCOUNT not in repr(provider)
    provider.close()


def test_metadata_cache_refreshes_at_margin_and_rechecks_identity():
    metadata = Metadata()
    current = [NOW]
    provider = metadata.provider(clock=lambda: current[0])
    assert provider() == TOKEN
    current[0] += timedelta(seconds=3539)
    assert provider() == TOKEN and len(metadata.calls) == 2
    current[0] += timedelta(seconds=1)
    metadata.body["access_token"] = TOKEN + "-refreshed"
    assert provider() == TOKEN + "-refreshed" and len(metadata.calls) == 4
    metadata.account = "another@synthetic-test-project.iam.gserviceaccount.com"
    current[0] += timedelta(seconds=3540)
    with pytest.raises(AuthError, match="identity does not match"):
        provider()
    assert len(metadata.calls) == 5  # No token request after wrong identity.
    assert provider._cached is None


def test_concurrent_requests_share_one_refresh():
    metadata = Metadata()
    provider = metadata.provider(clock=lambda: NOW)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: provider(), range(32))) == [TOKEN] * 32
    assert len(metadata.calls) == 2


@pytest.mark.parametrize("field,value", [
    ("expires_in", True), ("expires_in", "3600"), ("expires_in", 0), ("expires_in", 60),
    ("expires_in", 3601), ("token_type", "bearer"), ("token_type", None),
    ("access_token", ""), ("access_token", "invalid token"), ("access_token", TOKEN + "\n"),
    ("access_token", "é"), ("access_token", None), ("access_token", "x" * 16385),
])
def test_metadata_rejects_malformed_token_response_without_echoing_it(field, value):
    metadata = Metadata()
    metadata.body[field] = value
    with pytest.raises(AuthError) as caught:
        metadata.provider(clock=lambda: NOW)()
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("body", [[], "private-response", None])
def test_metadata_rejects_nonobject_response(body):
    metadata = Metadata()
    metadata.body = body
    with pytest.raises(AuthError, match="invalid response"):
        metadata.provider(clock=lambda: NOW)()


def test_metadata_header_and_identity_must_match_before_a_token_is_requested():
    metadata = Metadata()
    metadata.headers = {}
    with pytest.raises(AuthError, match="metadata response"):
        metadata.provider(clock=lambda: NOW)()
    assert len(metadata.calls) == 1
    metadata.headers = {"Metadata-Flavor": "Google"}
    metadata.account = "unexpected-private-identity"
    with pytest.raises(AuthError, match="identity") as caught:
        metadata.provider(clock=lambda: NOW)()
    assert metadata.account not in str(caught.value)
    assert len(metadata.calls) == 2


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_auth_http_errors_never_follow_redirects_or_retry_or_echo_bodies(status):
    calls = []
    def handle(request):
        calls.append(request.url)
        return httpx.Response(status, text=TOKEN, headers={"Location": "https://unsafe.example/" + TOKEN})
    provider = CloudRunTokenProvider("sheets", ACCOUNT, transport=httpx.MockTransport(handle), clock=lambda: NOW)
    with pytest.raises(AuthError, match=f"HTTP {status}") as caught:
        provider()
    assert len(calls) == 1 and TOKEN not in "".join(traceback.format_exception(caught.value))


def test_refresh_failure_clears_previous_token_without_stale_fallback(caplog, capsys):
    metadata = Metadata()
    current = [NOW]
    provider = metadata.provider(clock=lambda: current[0])
    assert provider() == TOKEN
    current[0] += timedelta(seconds=3540)
    metadata.status = 403
    with pytest.raises(AuthError):
        provider()
    assert provider._cached is provider._expires is None
    with pytest.raises(AuthError):
        provider()
    assert len(metadata.calls) == 6  # Explicit second call; no automatic retry.
    assert TOKEN not in caplog.text + capsys.readouterr().out


def test_transport_and_caller_exceptions_are_redacted():
    def broken(request):
        raise httpx.ReadTimeout(TOKEN)
    provider = CloudRunTokenProvider("sheets", ACCOUNT, transport=httpx.MockTransport(broken), clock=lambda: NOW)
    with pytest.raises(AuthError) as caught:
        provider()
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    def caller():
        raise RuntimeError(CALLER)
    provider = ImpersonatedTokenProvider("sheets", ACCOUNT, caller, transport=httpx.MockTransport(broken), clock=lambda: NOW)
    with pytest.raises(AuthError) as caught:
        provider()
    assert CALLER not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("clock", [lambda: datetime(2026, 10, 11), lambda: None])
def test_invalid_clocks_fail_before_metadata_access(clock):
    metadata = Metadata()
    with pytest.raises(AuthError, match="aware clock"):
        metadata.provider(clock=clock)()
    assert metadata.calls == []


def test_clock_rollback_clears_cached_token_and_slow_refresh_is_not_returned():
    metadata = Metadata()
    current = [NOW]
    provider = metadata.provider(clock=lambda: current[0])
    provider()
    current[0] -= timedelta(seconds=1)
    with pytest.raises(AuthError, match="clock moved backwards"):
        provider()
    assert provider._cached is None
    ticks = iter([NOW, NOW + timedelta(seconds=3541)])
    with pytest.raises(AuthError, match="lifetime is insufficient"):
        metadata.provider(clock=lambda: next(ticks))()


@pytest.mark.parametrize("purpose", ["sheets", "firestore"])
def test_local_impersonation_uses_target_identity_fixed_scope_and_cached_expiry(purpose):
    calls, caller_calls = [], []
    def handle(request):
        calls.append(request)
        assert request.method == "POST" and request.url.host == "iamcredentials.googleapis.com"
        assert request.url.path == "/v1/projects/-/serviceAccounts/" + ACCOUNT + ":generateAccessToken"
        assert request.headers["Authorization"] == "Bearer " + CALLER
        assert json.loads(request.content) == {"scope": [SCOPES[purpose]], "lifetime": "3600s"}
        return httpx.Response(200, json={"accessToken": TOKEN, "expireTime": (NOW + timedelta(hours=1)).isoformat()})
    provider = ImpersonatedTokenProvider(purpose, ACCOUNT, lambda: caller_calls.append(True) or CALLER,
                                         transport=httpx.MockTransport(handle), clock=lambda: NOW)
    assert calls == caller_calls == []
    assert provider() == provider() == TOKEN
    assert len(calls) == len(caller_calls) == 1
    assert TOKEN not in repr(provider) and CALLER not in repr(provider)


@pytest.mark.parametrize("expiry", [None, "bad", "2026-10-11T03:00:00", "2026-10-11", "2026-10-11T02:00:00Z",
                                    "2026-10-11T02:01:00Z", "2026-10-11T03:00:01Z", "2026-19-11T03:00:00Z"])
def test_impersonation_refuses_expired_unbounded_or_unaware_expiry(expiry):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"accessToken": TOKEN, "expireTime": expiry}))
    with pytest.raises(AuthError, match="invalid token lifetime"):
        ImpersonatedTokenProvider("sheets", ACCOUNT, lambda: CALLER, transport=transport, clock=lambda: NOW)()


def test_impersonation_refreshes_and_invalidates_failed_tokens():
    current, calls = [NOW], []
    def handle(request):
        calls.append(True)
        return httpx.Response(403, text=TOKEN) if len(calls) == 3 else httpx.Response(
            200, json={"accessToken": TOKEN + str(len(calls)), "expireTime": (current[0] + timedelta(hours=1)).isoformat()})
    provider = ImpersonatedTokenProvider("firestore", ACCOUNT, lambda: CALLER,
                                         transport=httpx.MockTransport(handle), clock=lambda: current[0])
    assert provider() == TOKEN + "1"
    current[0] += timedelta(seconds=3540)
    assert provider() == TOKEN + "2"
    current[0] += timedelta(seconds=3540)
    with pytest.raises(AuthError, match="HTTP 403"):
        provider()
    assert provider._cached is None


def test_default_auth_and_live_api_gate_precede_any_credential_or_environment_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected credential/environment/file access")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "forbidden-path")
    called = []
    provider = ImpersonatedTokenProvider("sheets", ACCOUNT, lambda: called.append(True), clock=forbidden)
    with pytest.raises(LiveDisabled):
        provider()
    with pytest.raises(LiveDisabled):
        CloudRunTokenProvider("firestore", ACCOUNT, clock=forbidden)()
    metadata = Metadata()
    with pytest.raises(LiveDisabled):
        RestClient("sheets", token=metadata.provider(clock=lambda: NOW)).request("GET", "/blocked")
    assert called == metadata.calls == []
    with pytest.raises(LiveDisabled):
        CloudRunTokenProvider("sheets", ACCOUNT, transport=httpx.HTTPTransport())


@pytest.mark.parametrize("purpose,account", [("calendar", ACCOUNT), ("sheets", "user@example.com"), ("firestore", "invalid/../account")])
def test_unprepared_scope_or_invalid_identity_is_rejected(purpose, account):
    with pytest.raises(AuthError):
        CloudRunTokenProvider(purpose, account)


@pytest.mark.parametrize("factory", ["cloud_run", "local_impersonated"])
def test_factory_keeps_calendar_line_and_model_disabled(factory):
    metadata = Metadata()
    if factory == "cloud_run":
        tokens = cloud_run_token_providers(ACCOUNT, transport=httpx.MockTransport(metadata.handle), clock=lambda: NOW)
    else:
        tokens = local_impersonated_token_providers(ACCOUNT, lambda: CALLER)
    for port in (tokens.calendar, tokens.line, tokens.model):
        with pytest.raises(LiveDisabled):
            port()
    assert metadata.calls == [] and "<redacted>" in repr(tokens)


def test_scoped_mock_tokens_reach_expense_state_and_replay_path_without_calendar_oauth():
    metadata = Metadata()
    def scoped_metadata(request):
        if request.url.path.endswith("/token"):
            purpose = next(key for key, scope in SCOPES.items() if request.url.params["scopes"] == scope)
            metadata.body["access_token"] = TOKEN + "-" + purpose
        return metadata.handle(request)
    tokens = cloud_run_token_providers(ACCOUNT, transport=httpx.MockTransport(scoped_metadata), clock=lambda: NOW)
    services = MockApiServices(id_column="E")
    authorizations = []
    original = services.handle
    def guarded(request):
        if request.url.host in {"firestore.googleapis.com", "sheets.googleapis.com"}:
            authorizations.append((request.url.host, request.headers["Authorization"]))
        return original(request)
    services.transport = lambda: httpx.MockTransport(guarded)
    assistant, replies, _ = api_sandbox(services=services, approved_mock_id_column=True, clock=lambda: NOW, google_tokens=tokens)
    client = TestClient(create_app(assistant, replies))
    message = {"type": "message", "webhookEventId": "synthetic-keyless-purchase", "timestamp": int(NOW.timestamp() * 1000),
               "source": {"type": "user", "userId": "mock-owner"}, "replyToken": "synthetic-reply",
               "message": {"type": "text", "id": "synthetic-message", "text": "BTS 65"}}
    body = json.dumps({"events": [message]}).encode()
    headers = {"x-line-signature": signature(body, "mock-channel-secret")}
    first = client.post("/webhooks/line", content=body, headers=headers)
    assert first.status_code == 200 and first.json()["results"][0]["status"] == "recorded"
    assert client.post("/webhooks/line", content=body, headers=headers).json() == first.json()
    assert services.sheet_writes == len(services.line_replies) == 1
    assert set(authorizations) == {("firestore.googleapis.com", "Bearer " + TOKEN + "-firestore"),
                                   ("sheets.googleapis.com", "Bearer " + TOKEN + "-sheets")}
    assert {httpx.URL(url).params["scopes"] for method, url in metadata.calls if httpx.URL(url).path.endswith("/token")} == {SCOPES["sheets"], SCOPES["firestore"]}
    assert len(metadata.calls) == 4 and not services.calendar_events
    before = len(services.calls)
    with pytest.raises(LiveDisabled):
        assistant.calendar.client.request("GET", "/synthetic-calendar-auth")
    assert len(services.calls) == before
    assert TOKEN not in json.dumps(services.documents) + repr(services.calls)
