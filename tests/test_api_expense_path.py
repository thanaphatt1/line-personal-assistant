from datetime import datetime, timezone
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from line_assistant.api_sandbox import api_sandbox
from line_assistant.main import create_app, signature
from line_assistant.mock_api import MockApiServices
from line_assistant.service import ProviderFailure
from line_assistant.sheets_api import SheetTarget, SheetsApi
from line_assistant.sheet_metadata import TransactionIdMetadata
from line_assistant.transport import LiveDisabled, RestClient, WriteBlocked


NOW = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)


def event(text="BTS 65", identifier="purchase", number=1):
    return {"type": "message", "webhookEventId": identifier, "timestamp": int(NOW.timestamp() * 1000) + number,
            "source": {"type": "user", "userId": "mock-owner"}, "replyToken": "ephemeral-dummy-reply-token",
            "message": {"type": "text", "id": f"message-{identifier}", "text": text}}


def post(client, messages, signed=True):
    body = json.dumps({"events": messages}, ensure_ascii=False).encode()
    return client.post("/webhooks/line", content=body, headers={"x-line-signature": signature(body, "mock-channel-secret")} if signed else {})


def sandbox(services=None, **kwargs):
    return api_sandbox(services=services, clock=lambda: NOW, **kwargs)


def test_signed_expense_rest_path_persists_once_and_sends_one_mock_reply():
    assistant, replies, providers = sandbox()
    client = TestClient(create_app(assistant, replies))
    response = post(client, [event()])
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "recorded"
    assert response.json()["reply_delivery"] == ["sent"]
    # Fresh controller/HTTP clients reload state from the simulated Firestore API.
    restarted, restarted_replies, _ = sandbox(providers)
    replay = post(TestClient(create_app(restarted, restarted_replies)), [event()])
    assert replay.json() == response.json()
    assert providers.sheet_writes == 1
    assert len(providers.line_replies) == 1
    assert providers.cells[("All Expenses", 2, 2)] == "BTS"
    assert providers.cells[("All Expenses", 2, 3)] == 65.0
    assert not any(tab == "All Expenses" and col > 4 for tab, row, col in providers.cells)
    assert providers.documents
    stored = json.dumps(providers.documents)
    assert "ephemeral-dummy-reply-token" not in stored
    assert "placeholder-not-a-credential" not in stored


def test_signature_owner_and_changed_payload_gate_side_effects():
    assistant, replies, providers = sandbox()
    client = TestClient(create_app(assistant, replies))
    initial_calls = len(providers.calls)
    assert post(client, [event()], signed=False).status_code == 401
    denied = event()
    denied["source"]["userId"] = "intruder"
    assert post(client, [denied]).json()["results"][0]["status"] == "denied"
    assert len(providers.calls) == initial_calls
    post(client, [event()])
    assert post(client, [event("BTS 100")]).json()["results"][0]["status"] == "conflict"
    assert providers.sheet_writes == len(providers.line_replies) == 1


def test_unknown_item_waits_across_firestored_controller_recreation_then_chooses_existing():
    assistant, replies, providers = sandbox()
    proposal = post(TestClient(create_app(assistant, replies)), [event("sevne 100")]).json()["results"][0]
    assert proposal["status"] == "clarification"
    assert providers.sheet_writes == 0
    restarted, replies, _ = sandbox(providers)
    answer = event(f"เลือก {proposal['data']['token']} seven", "answer", 2)
    response = post(TestClient(create_app(restarted, replies)), [answer])
    assert response.json()["results"][0]["status"] == "recorded"
    assert providers.cells[("All Expenses", 2, 2)] == "seven"
    assert providers.cells[("Database", 3, 1)] == "seven"
    assert providers.sheet_writes == 1
    assert len(providers.line_replies) == 2


def test_income_is_blocked_and_calendar_write_decision_is_not_invented():
    assistant, replies, providers = sandbox()
    client = TestClient(create_app(assistant, replies))
    assert post(client, [event("รายรับ เงินเดือน 30000")]).json()["results"][0]["status"] == "income_unconfigured"
    result = post(client, [event("นัด ตรวจฟัน | 2026-10-11 14:00", "calendar", 2)]).json()["results"][0]
    assert result["status"] == "setup_required"
    assert not providers.calendar_events
    assert providers.sheet_writes == 0
    assert post(client, [event("BTS 65", "expense", 3)]).json()["results"][0]["status"] == "recorded"


def test_manual_review_commit_ambiguity_survives_recreation_and_blocks_new_writes():
    assistant, replies, providers = sandbox()
    providers.fail_after_sheet_write = True
    assert post(TestClient(create_app(assistant, replies)), [event()]).status_code == 503
    assert providers.sheet_writes == 1
    for identifier in ("purchase", "different"):
        restarted, replies, _ = sandbox(providers)
        response = post(TestClient(create_app(restarted, replies)), [event(identifier=identifier)])
        assert response.json()["results"][0]["status"] == "uncertain"
    assert providers.sheet_writes == 1


def test_mock_approved_id_column_resolves_a_positive_matching_commit_only():
    assistant, replies, providers = sandbox(approved_mock_id_column=True)
    providers.fail_after_sheet_write = True
    assert post(TestClient(create_app(assistant, replies)), [event()]).status_code == 503
    restarted, replies, _ = sandbox(providers, approved_mock_id_column=True)
    response = post(TestClient(create_app(restarted, replies)), [event()])
    assert response.json()["results"][0]["status"] == "recorded"
    assert providers.sheet_writes == len(providers.line_replies) == 1
    assert len(providers.cells[("All Expenses", 2, 5)]) == 64


@pytest.mark.parametrize("condition", ["missing", "duplicate", "wrong_payload"])
def test_id_readback_never_infers_noncommit_or_accepts_bad_proof(condition):
    assistant, _, providers = sandbox(approved_mock_id_column=True)
    providers.fail_after_sheet_write = True
    with pytest.raises(ProviderFailure):
        assistant.handle(event())
    identity = providers.cells[("All Expenses", 2, 5)]
    if condition == "missing":
        del providers.cells[("All Expenses", 2, 5)]
    elif condition == "duplicate":
        providers.cells[("All Expenses", 3, 5)] = identity
    else:
        providers.cells[("All Expenses", 2, 3)] = 100
    restarted, _, _ = sandbox(providers, approved_mock_id_column=True)
    assert restarted.handle(event()).status == "uncertain"
    assert providers.sheet_writes == 1


def test_firestore_commit_ack_loss_is_read_back_before_a_sheet_write():
    assistant, _, providers = sandbox()
    providers.fail_after_firestore_commit = True
    assert assistant.handle(event()).status == "recorded"
    assert providers.sheet_writes == 1
    assert sandbox(providers)[0].handle(event()).status == "recorded"
    assert providers.sheet_writes == 1


def test_changed_candidate_and_blank_item_with_other_data_are_preserved():
    assistant, _, providers = sandbox()
    providers.cells[("All Expenses", 2, 4)] = "occupied remark without Item"
    assert assistant.handle(event()).status == "recorded"
    assert providers.cells[("All Expenses", 2, 4)] == "occupied remark without Item"
    assert providers.cells[("All Expenses", 3, 2)] == "BTS"
    # Independent simulator: occupancy changes between row selection and refresh.
    providers2 = MockApiServices()
    restarted, _, _ = sandbox(providers2)
    providers2.before_candidate_read = lambda mock: mock.cells.update({("All Expenses", 2, 4): "human changed candidate"})
    assert restarted.handle(event()).status == "conflict"
    assert providers2.sheet_writes == 0


@pytest.mark.parametrize("guard", ["headers", "protection", "capacity", "removed_item"])
def test_write_preflight_failure_never_sends_a_write(guard):
    assistant, _, providers = sandbox()
    if guard == "headers":
        providers.cells[("All Expenses", 1, 3)] = "Category"
    elif guard == "protection":
        providers.protections = [{"range": {"sheetId": 101}, "warningOnly": False}]
    elif guard == "capacity":
        providers.cells[("All Expenses", 1000, 4)] = "occupied"
    else:
        del providers.cells[("Database", 2, 1)]
    assert assistant.handle(event()).status == ("conflict" if guard == "removed_item" else "setup_required")
    assert providers.sheet_writes == 0


def test_catalog_and_summary_use_numeric_serial_dates_without_classification():
    assistant, _, providers = sandbox()
    assistant.handle(event())
    result = assistant.handle(event("สรุปวันนี้", "summary", 2))
    assert result.data == {"income": None, "income_supported": False, "expense": "65.00"}
    providers.cells[("Database", 2, 2)] = "Reclassified"
    assert assistant.sheets.catalog().match("BTS").canonical == "BTS"
    assert assistant.sheets.summary(NOW.date(), NOW.date())["expense"] == "65.00"


@pytest.mark.parametrize("service", ["line", "sheets", "calendar", "firestore"])
def test_default_transport_refuses_network_before_loading_any_credentials(service):
    def forbidden():
        pytest.fail("Credential provider must not be consulted in disabled mode")
    client = RestClient(service, token=forbidden)
    with pytest.raises(LiveDisabled):
        client.request("GET", "/anything")
    client.close()
    with pytest.raises(LiveDisabled):
        RestClient(service, httpx.HTTPTransport())


def test_id_strategy_requires_explicit_schema_choice_and_does_not_reuse_input_columns():
    with pytest.raises(ValueError):
        SheetTarget(recovery="approved_id_column", transaction_id_metadata=TransactionIdMetadata("E"))
    for column in ("A", "B", "C", "D", "A1", "E:Z"):
        with pytest.raises(ValueError):
            SheetTarget(recovery="approved_id_column", transaction_id_metadata=TransactionIdMetadata(column, schema_approved=True))
    api = SheetsApi(RestClient("sheets", MockApiServices().transport()), SheetTarget())
    from line_assistant.contracts import AddTransactions
    args = AddTransactions.model_validate({"entries": [{"item": "BTS", "amount": "65", "kind": "expense", "date": "2026-10-11"}]})
    with pytest.raises(WriteBlocked):
        api.add(args.entries, "key")
