from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from line_assistant.config import Settings
from line_assistant.adapters import Conflict, Reconciliation
from line_assistant.local_runtime import durable_mock_assistant


NOW = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1]


def event(text, event_id="purchase", sequence=1):
    return {"type": "message", "webhookEventId": event_id,
            "timestamp": int(NOW.timestamp() * 1000) + sequence,
            "source": {"type": "user", "userId": "mock-owner"},
            "message": {"type": "text", "id": f"message-{event_id}", "text": text}}


def runtime(directory, now=NOW, available=True, **kwargs):
    return durable_mock_assistant(directory, clock=lambda: now, reconciliation_available=available, **kwargs)


def worker(directory, message, phase="none", now=NOW, available=True):
    # Unique files permit concurrent subprocesses without racing test input writes.
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", dir=directory.parent, suffix=".json", delete=False) as file:
        json.dump(message, file)
        event_file = file.name
    try:
        return subprocess.run([sys.executable, str(ROOT / "tests/crash_worker.py"), str(directory), event_file,
                               now.isoformat(), phase, "yes" if available else "no"],
                              env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True,
                              text=True, timeout=30)
    finally:
        Path(event_file).unlink()


def assert_success(process, status):
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["status"] == status


@pytest.mark.parametrize("phase", ["prepared", "executing", "provider_staged", "provider_committed", "effect_returned",
                                  "action_completed", "receipt_completed"])
def test_actual_process_exit_and_restart_recovers_one_sheet_effect(tmp_path, phase):
    directory = tmp_path / "runtime"
    stopped = worker(directory, event("BTS 65"), phase)
    assert stopped.returncode == 73, stopped.stderr
    restarted = runtime(directory)
    assert len(restarted.sheets.entries) == (0 if phase in {"prepared", "executing", "provider_staged"} else 1)
    first = restarted.handle(event("BTS 65"))
    assert first.status == "recorded"
    assert runtime(directory).handle(event("BTS 65")) == first
    final = runtime(directory)
    assert len(final.sheets.entries) == len(final.sheets.write_log) == 1
    assert set(final.sheets.cell_values) == {"A1", "B1", "C1", "D1"}
    assert type(final.sheets.cell_values["A1"]) is int
    assert final.sheets.cell_values["C1"] == 65.0


@pytest.mark.parametrize("kind", ["create", "update", "delete", "clarified_expense"])
@pytest.mark.parametrize("phase", ["executing", "provider_staged", "provider_committed", "action_completed"])
def test_restart_recovery_of_confirmed_and_calendar_actions(tmp_path, kind, phase):
    directory = tmp_path / "runtime"
    assistant = runtime(directory)
    if kind in {"update", "delete"}:
        assistant.handle(event("นัด ตรวจฟัน | 2026-10-11 14:00", "created", 1))
        question = assistant.handle(event("แก้นัด ล่าสุด | ใหม่" if kind == "update" else "ลบนัด ล่าสุด", "proposal", 2))
        command = event(f"ยืนยัน {question.data['token']}", "answer", 3)
    elif kind == "clarified_expense":
        question = assistant.handle(event("sevne 100", "proposal", 1))
        command = event(f"เลือก {question.data['token']} seven", "answer", 2)
    else:
        command = event("นัด ตรวจฟัน | 2026-10-11 14:00")
    stopped = worker(directory, command, phase)
    assert stopped.returncode == 73, stopped.stderr
    restarted = runtime(directory)
    result = restarted.handle(command)
    assert result.status == {"create": "created", "update": "updated", "delete": "deleted",
                             "clarified_expense": "recorded"}[kind]
    assert runtime(directory).handle(command) == result
    final = runtime(directory)
    assert final.state.pending is None
    if kind == "delete":
        assert not final.calendar.events
    elif kind == "clarified_expense":
        assert len(final.sheets.entries) == 1
        assert result.data["raw_items"] == ["sevne"]
    else:
        assert len(final.calendar.events) == 1
        assert next(iter(final.calendar.events.values())).version == (2 if kind == "update" else 1)


def test_concurrent_processes_redeliver_one_event(tmp_path):
    directory = tmp_path / "runtime"
    runtime(directory)  # Schema setup is not part of the concurrency under test.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: worker(directory, event("BTS 65")), range(12)))
    for process in results:
        assert_success(process, "recorded")
    final = runtime(directory)
    assert len(final.sheets.entries) == len(final.sheets.write_log) == 1
    assert len(final.state.actions) == 1


def test_independent_assistants_serialize_threads_and_keep_distinct_purchases(tmp_path):
    directory = tmp_path / "runtime"
    assistants = [runtime(directory) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: assistants[index].handle(event("BTS 65")), range(8)))
    assert all(result.status == "recorded" for result in results)
    runtime(directory).handle(event("BTS 65", "second"))
    assert len(runtime(directory).sheets.entries) == 2


@pytest.mark.parametrize("phase", ["executing", "provider_committed"])
def test_unknown_provider_outcome_stays_closed_across_restarts_and_expiry(tmp_path, phase):
    directory = tmp_path / "runtime"
    command = event("BTS 65")
    assert worker(directory, command, phase, available=False).returncode == 73
    expected_rows = 0 if phase == "executing" else 1
    for index, now in enumerate((NOW, NOW + timedelta(minutes=31), NOW + timedelta(days=31))):
        restarted = runtime(directory, now, available=False)
        assert restarted.handle(command).status == "uncertain"
        fresh = event("BTS 65", f"new-{index}")
        fresh["timestamp"] = int(now.timestamp() * 1000)
        assert restarted.handle(fresh).status == "uncertain"
        cancel = event("ยกเลิก", f"cancel-{index}")
        cancel["timestamp"] = int(now.timestamp() * 1000)
        assert restarted.handle(cancel).status == "uncertain"
        assert len(runtime(directory, now, available=False).sheets.entries) == expected_rows
    # Independent atomic mock proof can resolve it; a new confirmation cannot.
    recovered = runtime(directory, NOW + timedelta(days=31)).handle(command)
    assert recovered.status == ("expired" if expected_rows == 0 else "recorded")
    assert len(runtime(directory).sheets.entries) == expected_rows


def test_unknown_outcome_allows_read_only_summary_but_no_override_token(tmp_path):
    directory = tmp_path / "runtime"
    assert worker(directory, event("BTS 65"), "provider_committed", available=False).returncode == 73
    restarted = runtime(directory, available=False)
    assert restarted.handle(event("สรุปวันนี้", "read", 2)).status == "summary"
    assert restarted.handle(event("ยืนยัน deadbeef", "override", 3)).status == "uncertain"
    assert len(runtime(directory).sheets.entries) == 1


def test_changed_payload_of_unfinished_event_is_denied_before_recovery(tmp_path):
    directory = tmp_path / "runtime"
    assert worker(directory, event("BTS 65"), "executing").returncode == 73
    assert runtime(directory).handle(event("BTS 100")).status == "conflict"
    assert not runtime(directory).sheets.entries
    assert runtime(directory).handle(event("BTS 65")).status == "recorded"


def test_pending_last_references_cancellation_and_logical_expiry_survive_restart(tmp_path):
    directory = tmp_path / "runtime"
    command = event("sevne 100")
    question = runtime(directory).handle(command)
    assert runtime(directory).state.pending.token == question.data["token"]
    assert runtime(directory).handle(event(f"เลือก {question.data['token']} seven", "answer", 2)).status == "recorded"
    assert runtime(directory).state.last.kind == "transactions"
    assert runtime(directory).handle(command) == question
    assert runtime(directory).state.pending is None
    new_question = runtime(directory).handle(event("sevne 100", "second", 3))
    later = runtime(directory, NOW + timedelta(minutes=31))
    assert later.handle(event(f"เลือก {new_question.data['token']} seven", "late", 4)).status == "no_pending"
    assert len(runtime(directory).sheets.entries) == 1
    third = runtime(directory).handle(event("sevne 100", "third", 5))
    runtime(directory).handle(event("ยกเลิก", "cancel", 6))
    assert runtime(directory).handle(event(f"เลือก {third.data['token']} seven", "answer3", 7)).status == "no_pending"
    expired = runtime(directory, NOW + timedelta(days=2))
    expired.handle(event("ตารางวันนี้", "expire", 8))  # Expire logical state even if event is old.
    assert runtime(directory, NOW + timedelta(days=2)).state.last is None


@pytest.mark.parametrize("phase", ["prepared", "executing"])
def test_accepted_confirmation_not_attempted_before_expiry_cannot_write_on_recovery(tmp_path, phase):
    directory = tmp_path / "runtime"
    assistant = runtime(directory)
    assistant.handle(event("นัด ตรวจฟัน | 2026-10-11 14:00", "first"))
    question = assistant.handle(event("ลบนัด ล่าสุด", "proposal", 2))
    answer = event(f"ยืนยัน {question.data['token']}", "answer", 3)
    assert worker(directory, answer, phase).returncode == 73
    assert runtime(directory, NOW + timedelta(minutes=31)).handle(answer).status == "expired"
    assert len(runtime(directory).calendar.events) == 1


def test_committed_confirmation_recovers_after_question_expiry_without_new_write(tmp_path):
    directory = tmp_path / "runtime"
    assistant = runtime(directory)
    assistant.handle(event("นัด ตรวจฟัน | 2026-10-11 14:00", "first"))
    question = assistant.handle(event("ลบนัด ล่าสุด", "proposal", 2))
    answer = event(f"ยืนยัน {question.data['token']}", "answer", 3)
    assert worker(directory, answer, "provider_committed").returncode == 73
    assert runtime(directory, NOW + timedelta(minutes=31)).handle(answer).status == "deleted"
    assert not runtime(directory).calendar.events


def test_completed_replay_does_not_replace_new_last_reference_or_consume_new_pending(tmp_path):
    directory = tmp_path / "runtime"
    original = event("BTS 65")
    result = runtime(directory).handle(original)
    created = runtime(directory).handle(event("นัด ตรวจฟัน | 2026-10-11 14:00", "calendar", 2))
    question = runtime(directory).handle(event("sevne 100", "proposal", 3))
    assert runtime(directory).handle(original) == result
    final = runtime(directory)
    assert final.state.last.refs == (created.data["id"],)
    assert final.state.pending.token == question.data["token"]


def test_store_is_private_owner_bound_and_has_no_full_text_history(tmp_path):
    directory = tmp_path / "runtime"
    runtime(directory).handle(event("BTS 65"))
    assert directory.stat().st_mode & 0o777 == 0o700
    for path in directory.iterdir():
        assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="owner mismatch"):
        runtime(directory, settings=Settings(owner_id="different-owner"))
    import sqlite3
    with sqlite3.connect(directory / "controller.sqlite3") as connection:
        saved = json.loads(connection.execute("SELECT value FROM snapshots WHERE name='state'").fetchone()[0])
    assert "text" not in saved["claims"]["purchase"]
    assert "channel_secret" not in saved
    assert saved["actions"]["mock-owner:purchase:add_transactions"]["payload"]["entries"][0]["item"] == "BTS"


def test_lost_controller_checkpoint_after_effect_keeps_consumption_and_last_reference(tmp_path):
    directory = tmp_path / "runtime"
    assistant = runtime(directory)
    question = assistant.handle(event("sevne 100", "proposal"))
    original_checkpoint = assistant.state.checkpoint
    failed = False

    def fail_once_after_completion():
        nonlocal failed
        if not failed and any(action.status == "completed" for action in assistant.state.actions.values()):
            failed = True
            raise OSError("Simulated unavailable controller storage")
        original_checkpoint()

    assistant.state.checkpoint = fail_once_after_completion
    answer = event(f"เลือก {question.data['token']} seven", "answer", 2)
    with pytest.raises(OSError):
        assistant.handle(answer)
    restarted = runtime(directory)
    assert restarted.handle(answer).status == "recorded"
    assert restarted.state.pending is None
    assert restarted.state.last.kind == "transactions"
    assert len(runtime(directory).sheets.entries) == 1


@pytest.mark.parametrize("proof_failure", ["exception", "mismatched_payload", "unsupported_outcome"])
def test_untrustworthy_reconciliation_never_retries_an_effect(tmp_path, proof_failure):
    directory = tmp_path / "runtime"
    command = event("BTS 65")
    assert worker(directory, command, "provider_committed").returncode == 73
    assistant = runtime(directory)

    def bad_proof(*args):
        if proof_failure == "exception":
            raise OSError("Provider read unavailable")
        if proof_failure == "mismatched_payload":
            raise Conflict("Different payload receipt")
        return Reconciliation("maybe")

    def forbidden_write(*args):
        pytest.fail("Unresolved outcomes must not be retried")

    assistant.sheets.reconcile = bad_proof
    assistant.sheets.add = forbidden_write
    assert assistant.handle(command).status == "uncertain"
    assert assistant.handle(event("BTS 65", "new", 2)).status == "uncertain"
    assert len(runtime(directory).sheets.entries) == 1


def test_persisted_event_receipt_expiry_does_not_accept_an_ancient_delivery(tmp_path):
    directory = tmp_path / "runtime"
    command = event("BTS 65")
    assert runtime(directory).handle(command).status == "recorded"
    expired = runtime(directory, NOW + timedelta(days=31))
    assert expired.handle(command).status == "stale"
    assert not expired.state.receipts
    assert not expired.state.actions
    assert len(runtime(directory).sheets.entries) == 1


def test_concurrent_distinct_confirmations_consume_pending_once(tmp_path):
    directory = tmp_path / "runtime"
    assistant = runtime(directory)
    assistant.handle(event("นัด ตรวจฟัน | 2026-10-11 14:00", "first"))
    question = assistant.handle(event("ลบนัด ล่าสุด", "proposal", 2))
    confirmations = [event(f"ยืนยัน {question.data['token']}", f"answer-{index}", index + 3) for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda message: worker(directory, message), confirmations))
    statuses = [json.loads(process.stdout)["status"] for process in results if process.returncode == 0]
    assert len(statuses) == 8
    assert statuses.count("deleted") == 1
    assert statuses.count("no_pending") == 7
    assert not runtime(directory).calendar.events


def test_recovered_old_commit_does_not_refresh_expired_last_reference(tmp_path):
    directory = tmp_path / "runtime"
    command = event("BTS 65")
    assert worker(directory, command, "provider_committed").returncode == 73
    restarted = runtime(directory, NOW + timedelta(days=2))
    assert restarted.handle(command).status == "recorded"
    assert restarted.state.last is None


def test_non_owner_cannot_trigger_reconciliation_of_a_persisted_action(tmp_path):
    directory = tmp_path / "runtime"
    command = event("BTS 65")
    assert worker(directory, command, "executing").returncode == 73
    assistant = runtime(directory)
    forged = {**command, "source": {"type": "user", "userId": "attacker"}}
    assert assistant.handle(forged).status == "denied"
    assert not runtime(directory).sheets.entries
    assert runtime(directory).state.actions["mock-owner:purchase:add_transactions"].status == "executing"


def signed_post(client, messages):
    from line_assistant.main import signature
    body = json.dumps({"events": messages}, ensure_ascii=False).encode()
    return client.post("/webhooks/line", content=body,
                       headers={"x-line-signature": signature(body, "mock-channel-secret")})


def test_signed_http_acknowledgement_loss_recovers_with_a_fresh_app(tmp_path):
    from fastapi.testclient import TestClient
    from line_assistant.main import create_app
    directory = tmp_path / "runtime"
    assistant = runtime(directory)
    original = assistant.sheets.add

    def lost_response(*args):
        original(*args)
        raise OSError("Private provider diagnostic")

    assistant.sheets.add = lost_response
    message = event("BTS 65")
    response = signed_post(TestClient(create_app(assistant)), [message])
    assert response.status_code == 503
    assert "Private" not in response.text
    recovered = signed_post(TestClient(create_app(runtime(directory))), [message])
    assert recovered.status_code == 200
    assert recovered.json()["results"][0]["status"] == "recorded"
    assert len(runtime(directory).sheets.entries) == 1


def test_signed_batch_partial_completion_replays_each_event_independently(tmp_path):
    from fastapi.testclient import TestClient
    from line_assistant.main import create_app
    directory = tmp_path / "runtime"
    assistant = runtime(directory)
    original = assistant.sheets.add
    count = 0

    def lose_second_response(*args):
        nonlocal count
        receipt = original(*args)
        count += 1
        if count == 2:
            raise OSError("Second acknowledgement lost")
        return receipt

    assistant.sheets.add = lose_second_response
    messages = [event("BTS 65", "first"), event("seven 100", "second", 2)]
    assert signed_post(TestClient(create_app(assistant)), messages).status_code == 503
    restarted = runtime(directory)
    response = signed_post(TestClient(create_app(restarted)), messages)
    assert [result["status"] for result in response.json()["results"]] == ["recorded", "recorded"]
    assert len(runtime(directory).sheets.entries) == 2


def test_signed_http_unknown_outcome_is_explicit_and_unsigned_retry_cannot_reconcile(tmp_path):
    from fastapi.testclient import TestClient
    from line_assistant.main import create_app
    directory = tmp_path / "runtime"
    message = event("BTS 65")
    assert worker(directory, message, "provider_committed", available=False).returncode == 73
    client = TestClient(create_app(runtime(directory, available=False)))
    body = json.dumps({"events": [message]}).encode()
    assert client.post("/webhooks/line", content=body).status_code == 401
    response = signed_post(client, [message])
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "uncertain"
    assert response.json()["results"][0]["data"]["needs_reconciliation"] is True
    assert len(runtime(directory).sheets.entries) == 1
