from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from line_assistant.api_sandbox import api_sandbox
from persistent_api_mock import PersistentApiMock


ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)


def event(identifier="expense"):
    return {"type": "message", "webhookEventId": identifier, "timestamp": int(NOW.timestamp()*1000),
            "source": {"type": "user", "userId": "mock-owner"},
            "message": {"type": "text", "id": identifier, "text": "BTS 65\nseven 100"}}


def worker(directory, message, phase="none", now=NOW):
    with tempfile.NamedTemporaryFile(mode="w", dir=directory.parent, suffix=".json", delete=False) as file:
        json.dump(message, file)
        path = Path(file.name)
    try:
        return subprocess.run([sys.executable, str(ROOT / "tests/id_crash_worker.py"), str(directory), str(path), now.isoformat(), phase],
                              env={**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests")))},
                              text=True, capture_output=True, timeout=30)
    finally:
        path.unlink()


@pytest.mark.parametrize("phase", ["before_append", "after_append", "effect_returned", "action_completed"])
def test_actual_exit_and_expired_lease_recovery_of_identified_append(tmp_path, phase):
    directory = tmp_path / "runtime"
    stopped = worker(directory, event(), phase)
    assert stopped.returncode == 73, stopped.stderr
    expected = "uncertain" if phase == "before_append" else "recorded"
    # Process death leaves a logical Firestore lease until its deadline.
    restarted = worker(directory, event(), now=NOW + timedelta(seconds=121))
    assert restarted.returncode == 0, restarted.stderr
    assert json.loads(restarted.stdout)["status"] == expected
    final = PersistentApiMock(directory)
    assert final.sheet_writes == (0 if phase == "before_append" else 1)
    replay = worker(directory, event(), now=NOW + timedelta(seconds=122))
    assert replay.returncode == 0 and json.loads(replay.stdout)["status"] == expected
    assert PersistentApiMock(directory).sheet_writes == final.sheet_writes
    if phase != "before_append":
        assert len({value for (tab, row, col), value in final.cells.items() if tab == "All Expenses" and row > 1 and col == 5}) == 2


def test_same_webhook_across_concurrent_processes_uses_one_lease_and_one_append(tmp_path):
    directory = tmp_path / "runtime"
    PersistentApiMock(directory)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: worker(directory, event()), range(6)))
    assert any(process.returncode == 0 for process in results)
    assert all(process.returncode in {0, 75} for process in results), [process.stderr for process in results]
    # Busy workers retry the original event after the winner releases its lease.
    for _ in range(6):
        replay = worker(directory, event())
        assert replay.returncode == 0 and json.loads(replay.stdout)["status"] == "recorded", replay.stderr
    final = PersistentApiMock(directory)
    assert final.sheet_writes == 1
    assert len([key for key in final.cells if key[0] == "All Expenses" and key[1] > 1 and key[2] == 5]) == 2


def test_distinct_concurrent_purchases_are_preserved_with_distinct_ids(tmp_path):
    directory = tmp_path / "runtime"
    PersistentApiMock(directory)
    messages = [event(f"expense-{index}") for index in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda message: worker(directory, message), messages))
    assert all(process.returncode in {0, 75} for process in results), [process.stderr for process in results]
    for message in messages:
        replay = worker(directory, message)
        assert replay.returncode == 0 and json.loads(replay.stdout)["status"] == "recorded", replay.stderr
    final = PersistentApiMock(directory)
    ids = [value for (tab, row, col), value in final.cells.items() if tab == "All Expenses" and row > 1 and col == 5]
    assert final.sheet_writes == 6 and len(ids) == len(set(ids)) == 12


def test_recovery_does_not_adopt_a_legacy_matching_row_without_an_id(tmp_path):
    directory = tmp_path / "runtime"
    providers = PersistentApiMock(directory)
    with providers.store.transaction() as connection:
        providers.cells[("All Expenses", 2, 1)] = 46306
        providers.cells[("All Expenses", 2, 2)] = "BTS"
        providers.cells[("All Expenses", 2, 3)] = 65.0
        providers.store.write(connection, "api", providers.snapshot())
    assert worker(directory, event(), "before_append").returncode == 73
    restarted = worker(directory, event(), now=NOW + timedelta(seconds=121))
    assert restarted.returncode == 0 and json.loads(restarted.stdout)["status"] == "uncertain"
    final = PersistentApiMock(directory)
    assert final.sheet_writes == 0 and ("All Expenses", 2, 5) not in final.cells
