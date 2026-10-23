"""Private local SQLite checkpoints. This is a mock harness, not Firestore."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from .state import Action, CanaryRun, Claim, LastAction, MemoryState, ModelBudget, ModelRun, Pending, Receipt, Reply, Result
from .transport import LINE_ERROR_CATEGORIES


class SQLiteStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create the DB privately before SQLite creates its journal/sidecar files.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS snapshots (name TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def read(connection, name):
        row = connection.execute("SELECT value FROM snapshots WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def write(connection, name, value):
        connection.execute("INSERT INTO snapshots(name, value) VALUES (?, ?) "
                           "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                           (name, json.dumps(value, ensure_ascii=False, separators=(",", ":"))))


def result_from(value):
    if value is None:
        return None
    data = value["data"]
    if 'line_actions' in data:
        from .conversation_ui import render_actions
        data['line_message'] = render_actions(value['message'], data.pop('line_actions'))
    if value["status"] == "recorded" and 'refs' in data:
        data["refs"] = tuple(data["refs"])
    return Result(value["status"], value["message"], data)


def jsonable(value):
    # All serialized dates are typed state fields, never whole webhook envelopes.
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()))


def workspace_snapshot(state, *, include_records=True):
    return jsonable({
                     "pending": asdict(state.pending) if state.pending else None,
                     "last": asdict(state.last) if state.last else None,
                     "recent_turns": state.recent_turns,
                     **{name: {key: compact_record(asdict(item)) for key, item in getattr(state, name).items()} if include_records else {}
                        for name in ("receipts", "claims", "actions", "replies")}})

def compact_record(record):
    result = record.get('result')
    if result and result['data'].get('line_message'):
        from .conversation_ui import render_actions
        original = result['data']['line_message']
        actions = [b['action'] for b in original['contents']['footer']['contents']]
        if original == render_actions(result['message'], actions):
            del result['data']['line_message']
            result['data']['line_actions'] = actions
    return record


def state_snapshot(state, owner_id, *, include_records=True):
    return jsonable({"version": 1, "owner_id": owner_id, **workspace_snapshot(state,include_records=include_records),
                     **{name: {key: asdict(item) for key, item in getattr(state, name).items()} if include_records else {}
                        for name in ("model_runs", "model_budgets")},
                     "workbook_id": state.workbook_id,
                     "workspaces": state.workspaces if include_records else
                         {book: {**part, **{name: {} for name in ('claims','receipts','actions','replies')}}
                          for book,part in state.workspaces.items()},
                     "event_workbooks": dict(state.event_workbooks) if include_records else {}, "production_ready": state.production_ready,
                     "model_halted": state.model_halted,
                     "canary": asdict(state.canary) if state.canary else None})


def restore_state(state, value, owner_id):
    if value.get("version") != 1 or value.get("owner_id") != owner_id:
        raise ValueError("Local store version/owner mismatch; use a separate mock directory")
    if type(value.get("production_ready", False)) is not bool:
        raise ValueError("Invalid durable production readiness proof")
    restore_workspace(state, value)
    state.workbook_id = value.get("workbook_id")
    state.workspaces = value.get("workspaces", {})
    state.event_workbooks = value.get("event_workbooks", {})
    state.production_ready = value.get("production_ready", False)
    _restore_global(state, value)


def restore_workspace(state, value):
    state.recent_turns = value.get("recent_turns", [])[-6:]
    pending, last = value["pending"], value["last"]
    state.pending = Pending(**{**pending, "expires_at": datetime.fromisoformat(pending["expires_at"])}) if pending else None
    state.last = LastAction(last["kind"], tuple(last["refs"]), datetime.fromisoformat(last["expires_at"])) if last else None
    state.receipts = {key: Receipt(item["fingerprint"], result_from(item["result"]), datetime.fromisoformat(item["expires_at"]))
                      for key, item in value["receipts"].items()}
    state.claims = {key: Claim(item["fingerprint"], datetime.fromisoformat(item["expires_at"]), item["action_key"])
                    for key, item in value["claims"].items()}
    state.actions = {key: Action(item["kind"], item["payload"], item["metadata"], datetime.fromisoformat(item["deadline"]),
                                datetime.fromisoformat(item["expires_at"]), item["status"], result_from(item["result"]))
                     for key, item in value["actions"].items()}
    state.replies = {}
    for key, item in value.get("replies", {}).items():
        status, category = item.get("http_status"), item.get("error_category")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ValueError("Invalid persisted reply HTTP status")
        if category is not None and (not isinstance(category, str) or category not in LINE_ERROR_CATEGORIES):
            raise ValueError("Invalid persisted reply error category")
        timings = {name: item.get(name) for name in ("receipt_elapsed_ms", "attempt_duration_ms")}
        if any(v is not None and (type(v) is not int or not 0 <= v <= 86400000) for v in timings.values()):
            raise ValueError("Invalid persisted reply timing")
        state.replies[key] = Reply(item["content_hash"], datetime.fromisoformat(item["expires_at"]), item["status"],
                                  datetime.fromisoformat(item["token_deadline"]) if item.get("token_deadline") else None,
                                  status, category, **timings)


def _restore_global(state, value):
    state.model_runs = {key: ModelRun(**{**item, "expires_at": datetime.fromisoformat(item["expires_at"])})
                        for key, item in value.get("model_runs", {}).items()}
    state.model_budgets = {key: ModelBudget(**item) for key, item in value.get("model_budgets", {}).items()}
    state.model_halted = value.get("model_halted", False)
    canary = value.get("canary")
    if canary is not None:
        if any(type(canary.get(key)) is not bool for key in ("model_attempted", "append_attempted", "reply_attempted")):
            raise ValueError("Invalid durable canary allowance")
        state.canary = CanaryRun(**{**canary, "starts_at": datetime.fromisoformat(canary["starts_at"]),
                                    "expires_at": datetime.fromisoformat(canary["expires_at"])})
    else:
        state.canary = None  # Backward-compatible snapshots created before canary support.


class SQLiteState(MemoryState):
    """One local owner, serialized across threads/processes with an OS lock.

    No database transaction spans a provider call. Explicit checkpoint() calls
    expose the same non-atomic boundary a future Firestore adapter must handle.
    """

    def __init__(self, path: Path, owner_id: str):
        super().__init__()
        self.store = SQLiteStore(path)
        self.owner_id = owner_id
        self.lock_path = self.store.path.with_suffix(".lock")
        with self.session():
            pass

    @contextmanager
    def session(self):
        with self.lock:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                with self.store.transaction() as connection:
                    value = self.store.read(connection, "state")
                if value:
                    self._restore(value)
                try:
                    yield
                finally:
                    self.checkpoint()
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def checkpoint(self):
        value = state_snapshot(self, self.owner_id)
        with self.store.transaction() as connection:
            self.store.write(connection, "state", value)

    def _restore(self, value):
        restore_state(self, value, self.owner_id)

    def before_effect(self):
        # Match Firestore's pre-I/O checkpoint; finally is not reached on os._exit.
        self.checkpoint()
