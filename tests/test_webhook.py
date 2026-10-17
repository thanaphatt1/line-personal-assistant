import json

from fastapi.testclient import TestClient
import pytest

from line_assistant.main import create_app, signature


def post(client, payload, signed=True):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {"x-line-signature": signature(body, "mock-channel-secret")} if signed else {}
    return client.post("/webhooks/line", content=body, headers=headers)


def test_signed_empty_verification_does_not_plan(h):
    def forbidden(*args):
        raise AssertionError("Verification should not plan")
    h.service.planner.plan = forbidden
    client = TestClient(create_app(h.service))
    assert post(client, {"events": []}).json() == {"mode": "mock", "results": []}
    assert client.get("/health").json() == {"status": "ok", "mode": "mock"}


def test_signature_checked_before_parse(h):
    client = TestClient(create_app(h.service))
    assert client.post("/webhooks/line", content=b"not json").status_code == 401
    assert client.post("/webhooks/line", content=b"not json", headers={
        "x-line-signature": signature(b"not json", "mock-channel-secret")}).status_code == 400
    assert post(client, {"events": [h.event("BTS 65")]}, signed=False).status_code == 401
    assert not h.service.sheets.entries


def test_body_bytes_cannot_change_after_signing(h):
    client = TestClient(create_app(h.service))
    body = json.dumps({"events": [h.event("BTS 65")]}).encode()
    response = client.post("/webhooks/line", content=body + b" ", headers={
        "x-line-signature": signature(body, "mock-channel-secret")})
    assert response.status_code == 401
    assert not h.service.sheets.entries


@pytest.mark.parametrize("owner,source_type", [("intruder", "user"), ("mock-owner", "group"), ("mock-owner", "room")])
def test_owner_gate_precedes_reads_and_planner(h, owner, source_type):
    def forbidden(*args):
        raise AssertionError("Non-owner input must never plan or access data")
    h.service.planner.plan = forbidden
    response = post(TestClient(create_app(h.service)), {"events": [h.event("today", owner=owner, source_type=source_type)]})
    assert response.status_code == 200
    assert response.json()["results"] == [{"status": "denied", "message": "", "data": {}}]


def test_signed_nontext_is_ignored(h):
    event = h.event("BTS 65")
    event["message"]["type"] = "image"
    assert h.service.handle(event).status == "ignored"
    assert not h.service.sheets.entries


@pytest.mark.parametrize("payload", [{}, {"events": {}}, {"events": [None]}, []])
def test_invalid_envelopes(h, payload):
    assert post(TestClient(create_app(h.service)), payload).status_code == 400


def test_payload_limit(h):
    client = TestClient(create_app(h.service))
    assert client.post("/webhooks/line", content=b"x" * (128 * 1024 + 1)).status_code == 413


def test_adapter_failure_does_not_claim_success(h):
    def failure(*args):
        raise RuntimeError("Sensitive provider diagnostics")
    h.service.sheets.add = failure
    response = post(TestClient(create_app(h.service)), {"events": [h.event("BTS 65")]})
    assert response.status_code == 503
    assert "Sensitive" not in response.text
    assert not h.service.state.receipts
