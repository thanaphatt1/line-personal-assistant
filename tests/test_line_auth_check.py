"""One-shot disabled-startup probe; all provider/auth requests are mocked."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging

from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr
import pytest

from line_assistant.cloud_run import create_cloud_run_app
from line_assistant.line_auth_check import run_line_auth_check
from line_assistant.pilot_config import PilotConfig
from line_assistant.readonly_preflight import SERVICE_ACCOUNT
from line_assistant.runtime_config import ConfigError

NOW = datetime(2026, 10, 11, 15, 30, tzinfo=timezone.utc)
PRIVATE = "PRIVATE_TOKEN_BODY_BOT_NAME_HEADER"
BOT = "U" + "b" * 32


def values(**extra):
    return {"PILOT_LINE_AUTH_CHECK_ID": "a1" * 16,
            "PILOT_LINE_AUTH_CHECK_EXPIRES_AT": "2026-10-11T15:33:00Z", **extra}


class ProbeApi:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {"userId": BOT, "displayName": PRIVATE}
        self.doc = None; self.line_calls = 0; self.loads = []
        self.owner_snapshot = {"receipt": "preserved", "reservation_micro_usd": 1536}
        self.google_commits = []
        self.fail_claim = False; self.timeout = False

    def loader(self, name):
        self.loads.append(name)
        assert name == "line-channel-access-token"
        return SecretStr(PRIVATE)

    def handle(self, request):
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            if request.url.path.endswith("/email"):
                return httpx.Response(200, text=SERVICE_ACCOUNT, headers={"Metadata-Flavor": "Google"})
            return httpx.Response(200, json={"access_token": "fake-google-only"}, headers={"Metadata-Flavor": "Google"})
        if request.url.host == "firestore.googleapis.com":
            if request.method == "GET":
                assert "/assistant_pilot_states/line-auth-" in request.url.path
                return httpx.Response(200, json=self.doc) if self.doc else httpx.Response(404)
            assert request.method == "POST" and request.url.path.endswith("/documents:commit")
            write = json.loads(request.content)["writes"][0]; self.google_commits.append(write)
            assert "/assistant_pilot_states/line-auth-" in write["update"]["name"]
            assert "snapshot" not in write["update"]["fields"]
            if write["currentDocument"] == {"exists": False}:
                if self.fail_claim: return httpx.Response(409)
                self.doc = deepcopy(write["update"])
            else:
                assert write["currentDocument"] == {"updateTime": NOW.isoformat()}
                assert set(write["updateMask"]["fieldPaths"]) == {"http_status", "identity_match"}
                self.doc["fields"].update(write["update"]["fields"])
            return httpx.Response(200, json={"writeResults": [{"updateTime": NOW.isoformat()}]})
        assert request.url == "https://api.line.me/v2/bot/info" and request.method == "GET"
        assert self.doc["fields"]["claimed"] == {"booleanValue": True}
        assert request.headers["Authorization"] == "Bearer " + PRIVATE
        assert not request.content and not request.url.query
        self.line_calls += 1
        if self.timeout: raise httpx.ReadTimeout(PRIVATE, request=request)
        return httpx.Response(self.status, json=self.body, headers={"private-header": PRIVATE})


@pytest.mark.parametrize("status,expected_bot,identity", [(200, "", "not_configured"), (200, BOT, "match"),
                                                       (200, "U" + "c" * 32, "mismatch"), (401, "", "not_checked"),
                                                       (403, "", "not_checked")])
def test_only_one_line_read_and_two_safe_fields_across_restarted_disabled_factories(status, expected_bot, identity, caplog):
    caplog.set_level(logging.INFO)
    api = ProbeApi(status=status)
    config = values(PILOT_LINE_AUTH_CHECK_EXPECTED_BOT=expected_bot)
    original = deepcopy(api.owner_snapshot)
    for _ in range(2):
        hosted = create_cloud_run_app(config, transport=httpx.MockTransport(api.handle),
                                      secret_loader=api.loader, clock=lambda: NOW)
        with TestClient(hosted) as client:
            for _ in range(3): assert client.get("/health").json()["live_requests_enabled"] is False
            assert client.post("/webhooks/line", json={"events": []}).status_code == 503
            assert client.get("/diagnostics/line").status_code == 404
    assert api.line_calls == 1 and api.loads == ["line-channel-access-token"]
    assert api.owner_snapshot == original
    assert api.doc["fields"]["http_status"] == {"integerValue": str(status)}
    assert api.doc["fields"]["identity_match"] == {"stringValue": identity}
    assert PRIVATE not in caplog.text and BOT not in caplog.text and PRIVATE not in json.dumps(api.doc)


def test_claim_conflict_or_uncertain_claim_never_loads_secret_or_calls_line():
    api = ProbeApi(); api.fail_claim = True
    result = run_line_auth_check(PilotConfig.from_mapping(values()), api.loader,
                                 transport=httpx.MockTransport(api.handle), clock=lambda: NOW)
    assert result == {"http_status": None, "identity_match": "not_checked"}
    assert api.loads == [] and api.line_calls == 0


def test_timeout_is_nonretryable_and_does_not_print_exception(caplog):
    caplog.set_level(logging.INFO)
    api = ProbeApi(); api.timeout = True
    config = PilotConfig.from_mapping(values())
    for _ in range(2):
        result = run_line_auth_check(config, api.loader, transport=httpx.MockTransport(api.handle), clock=lambda: NOW)
        assert result == {"http_status": None, "identity_match": "outcome_unknown"}
    assert api.line_calls == 1 and PRIVATE not in caplog.text


@pytest.mark.parametrize("body", [{}, [], {"userId": PRIVATE}, {"userId": []}, {"userId": BOT, "extra": PRIVATE * 900}])
def test_bad_or_oversized_success_response_is_redacted(body, caplog):
    caplog.set_level(logging.INFO)
    api = ProbeApi(body=body)
    result = run_line_auth_check(PilotConfig.from_mapping(values()), api.loader,
                                 transport=httpx.MockTransport(api.handle), clock=lambda: NOW)
    assert result == {"http_status": 200, "identity_match": "invalid_response"}
    assert PRIVATE not in caplog.text and PRIVATE not in json.dumps(api.doc)


@pytest.mark.parametrize("offset", [-301, 181])
def test_outside_bounded_window_has_no_credential_or_provider_calls(offset):
    result = run_line_auth_check(PilotConfig.from_mapping(values()), lambda _: pytest.fail("secret"),
                                 transport=httpx.MockTransport(lambda _: pytest.fail("network")),
                                 clock=lambda: NOW + timedelta(seconds=offset))
    assert result == {"http_status": None, "identity_match": "window_closed"}


@pytest.mark.parametrize("overrides", [
    {"PILOT_LAUNCH_AUTHORIZED": "true", "PILOT_LINE_OWNER_ID": "U" + "c" * 32, "PILOT_MODEL_MONTHLY_MICRO_USD": "1536"},
    {"PILOT_MODEL_MONTHLY_MICRO_USD": "1536"}, {"PILOT_LINE_AUTH_CHECK_ID": ""},
    {"PILOT_LINE_AUTH_CHECK_EXPIRES_AT": ""}, {"PILOT_LINE_AUTH_CHECK_ID": "PRIVATE"},
    {"PILOT_LINE_AUTH_CHECK_EXPECTED_BOT": "invalid"},
])
def test_probe_cannot_be_combined_with_launch_or_nonzero_budget_or_invalid_config(overrides):
    with pytest.raises(ConfigError): PilotConfig.from_mapping(values(**overrides))
