"""Local mock state; deliberately not a production replacement for Firestore."""

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime
from threading import RLock


@dataclass(frozen=True)
class Result:
    status: str
    message: str
    data: dict = field(default_factory=dict)


@dataclass
class Pending:
    kind: str
    token: str
    origin_event_id: str
    source_timestamp: int
    expires_at: datetime
    payload: dict


@dataclass(frozen=True)
class LastAction:
    kind: str
    refs: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class Receipt:
    fingerprint: str
    result: Result
    expires_at: datetime


@dataclass
class Claim:
    fingerprint: str
    expires_at: datetime
    action_key: str | None = None


@dataclass
class Action:
    kind: str
    payload: dict
    metadata: dict
    deadline: datetime
    expires_at: datetime
    status: str = "prepared"
    result: Result | None = None


@dataclass
class Reply:
    content_hash: str
    expires_at: datetime
    status: str = "prepared"
    token_deadline: datetime | None = None
    http_status: int | None = None
    error_category: str | None = None
    receipt_elapsed_ms: int | None = None
    attempt_duration_ms: int | None = None


@dataclass
class ModelRun:
    fingerprint: str
    month: str
    reserved_micro_usd: int
    expires_at: datetime
    status: str = "reserved"
    decision: dict | None = None


@dataclass
class ModelBudget:
    reserved_micro_usd: int = 0
    calls: int = 0
    day_calls: dict[str, int] = field(default_factory=dict)
    last_reserved_at: str | None = None


@dataclass
class CanaryRun:
    spec_hash: str
    event_id: str
    fingerprint: str
    timestamp: int
    starts_at: datetime
    expires_at: datetime
    model_attempted: bool = False
    append_attempted: bool = False
    reply_attempted: bool = False


class MemoryState:
    def __init__(self):
        self.lock = RLock()
        self.pending: Pending | None = None
        self.last: LastAction | None = None
        self.receipts: dict[str, Receipt] = {}
        self.claims: dict[str, Claim] = {}
        self.actions: dict[str, Action] = {}
        self.replies: dict[str, Reply] = {}
        self.model_runs: dict[str, ModelRun] = {}
        self.model_budgets: dict[str, ModelBudget] = {}
        self.model_halted = False
        self.canary: CanaryRun | None = None  # Never expire/reset a consumed allowance.
        self.workbook_id: str | None = None
        self.workspaces: dict[str, dict] = {}
        # Server-owned ingress bindings prevent old events moving to a new workbook.
        self.event_workbooks: dict[str, str] = {}
        self.production_ready = False
        self.recent_turns: list[dict] = []

    def select_workbook(self, workbook_id, *, legacy_workbook):
        from .persistence import workspace_snapshot, restore_workspace
        current = self.workbook_id or legacy_workbook
        if self.workbook_id is None:
            for event_id in set(self.claims) | set(self.receipts) | set(self.model_runs):
                self.event_workbooks.setdefault(event_id, current)
            if self.canary:
                self.event_workbooks.setdefault(self.canary.event_id, current)
        if current != workbook_id:
            self.workspaces[current] = workspace_snapshot(self)
            restore_workspace(self, self.workspaces.pop(workbook_id, None) or workspace_snapshot(MemoryState()))
        self.workbook_id = workbook_id

    @contextmanager
    def session(self):
        with self.lock:
            yield

    def checkpoint(self):
        """Durable implementations commit here, before crossing a provider boundary."""

    def before_effect(self):
        """Lease-backed implementations fence the state holder immediately before I/O."""
        from .read_cache import invalidate
        invalidate()

    def expire(self, now: datetime):
        self.recent_turns = [turn for turn in self.recent_turns
                             if datetime.fromisoformat(turn["expires_at"]) > now][-6:]
        if self.pending and self.pending.expires_at <= now:
            self.pending = None
        if self.last and self.last.expires_at <= now:
            self.last = None
        self.receipts = {key: value for key, value in self.receipts.items() if value.expires_at > now}
        # Never erase an unresolved effect simply because a TTL elapsed.
        self.actions = {key: value for key, value in self.actions.items()
                        if value.status != "completed" or value.expires_at > now}
        self.claims = {key: value for key, value in self.claims.items()
                       if value.expires_at > now or value.action_key in self.actions}
        self.replies = {key: value for key, value in self.replies.items()
                        if value.status in {"executing", "uncertain"} or value.expires_at > now}
        self.model_runs = {key: value for key, value in self.model_runs.items()
                           if value.status != "completed" or value.expires_at > now}
        # Preserve unresolved/global attempts and archived canary proofs, while
        # dropping bindings only after their ordinary replay records expire.
        retained = set(self.claims) | set(self.receipts) | set(self.model_runs)
        for workspace in self.workspaces.values():
            retained.update(workspace.get("claims", {}))
            retained.update(workspace.get("receipts", {}))
        if self.canary:
            retained.add(self.canary.event_id)
        self.event_workbooks = {key: value for key, value in self.event_workbooks.items() if key in retained}
