"""Firestore REST snapshot/lease adapter.

Conditional document commits fence state checkpoints. They cannot fence a Google
Sheets HTTP write already in flight; real-adapter reconciliation never infers
not_committed from absence. No transaction remains open around provider I/O.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from .persistence import restore_state, state_snapshot
from .state import MemoryState
from .snapshot_codec import encode as encode_snapshot, decode as decode_snapshot
from .transport import ApiError, OutcomeUnknown, RestClient


class LeaseBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


def same_commit_fields(actual, expected):
    if set(actual) != set(expected):
        return False
    for key in expected:
        if key == "lease_until":
            left = datetime.fromisoformat(actual[key]["timestampValue"].replace("Z", "+00:00"))
            right = datetime.fromisoformat(expected[key]["timestampValue"].replace("Z", "+00:00"))
            if left != right:
                return False
        elif actual[key] != expected[key]:
            return False
    return True


class FirestoreState(MemoryState):
    def __init__(self, client: RestClient, owner_id: str, *, project_id="placeholder-project", database_id="(default)",
                 collection_id="assistant_states", lease_seconds=120, clock=None, max_snapshot_bytes=800_000,
                 compact_snapshots=False):
        super().__init__()
        if client.service != "firestore" or not owner_id or not 10 <= lease_seconds <= 600:
            raise ValueError("Explicit Firestore owner/lease configuration required")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id) or not re.fullmatch(r"(?:\(default\)|[A-Za-z0-9_-]+)", database_id):
            raise ValueError("Server-owned resource segments required")
        if not isinstance(collection_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", collection_id):
            raise ValueError("Server-owned collection segment required")
        self.client, self.owner_id = client, owner_id
        self.owner_hash = hashlib.sha256(owner_id.encode()).hexdigest()
        self.database = f"projects/{project_id}/databases/{database_id}"
        self.name = f"{self.database}/documents/{collection_id}/{self.owner_hash}"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_seconds, self.max_snapshot_bytes = lease_seconds, max_snapshot_bytes
        self.compact_snapshots = compact_snapshots
        self.holder = None
        self.fence = 0
        self.document = None

    def _read(self):
        try:
            document = self.client.request("GET", "/v1/" + self.name)
        except ApiError as error:
            if error.status == 404:
                return None
            raise
        fields = document.get("fields", {})
        if document.get("name") != self.name or fields.get("owner_hash", {}).get("stringValue") != self.owner_hash:
            raise LeaseLost("Firestore owner/resource binding changed")
        if not document.get("updateTime"):
            raise OutcomeUnknown("Firestore version missing")
        return document

    def _snapshot_state(self):
        return state_snapshot(self, self.owner_id)

    def _restore_state(self, snapshot):
        restore_state(self, snapshot, self.owner_id)

    def _fields(self, snapshot, holder, fence, until):
        encoded = encode_snapshot(snapshot, self.max_snapshot_bytes, compact=self.compact_snapshots)
        return {"owner_hash": {"stringValue": self.owner_hash}, "snapshot": {"stringValue": encoded},
                "lease_owner": {"stringValue": holder or ""}, "fence": {"integerValue": str(fence)},
                "lease_until": {"timestampValue": until.isoformat()},
                "commit_nonce": {"stringValue": uuid.uuid4().hex}}

    def _commit(self, previous, fields):
        condition = {"updateTime": previous["updateTime"]} if previous else {"exists": False}
        body = {"writes": [{"update": {"name": self.name, "fields": fields}, "currentDocument": condition}]}
        try:
            response = self.client.request("POST", f"/v1/{self.database}/documents:commit", body=body)
            update_time = response["writeResults"][0]["updateTime"]
        except ApiError as error:
            if error.status in {409, 412} or error.code in {"FAILED_PRECONDITION", "ABORTED", "ALREADY_EXISTS"}:
                raise LeaseLost("Conditional state commit rejected") from None
            raise
        except (OutcomeUnknown, KeyError, IndexError):
            # Resolve a lost commit acknowledgement by exact nonce AND fields.
            fresh = self._read()
            if not fresh or not same_commit_fields(fresh["fields"], fields):
                raise OutcomeUnknown("State commit outcome remains unverified") from None
            return fresh
        return {"name": self.name, "fields": fields, "updateTime": update_time}

    @contextmanager
    def session(self):
        with self.lock:
            if self.holder is not None:
                raise LeaseBusy("Nested lease sessions are unsupported")
            previous = self._read()
            now = self.clock()
            if previous:
                fields = previous["fields"]
                until = datetime.fromisoformat(fields["lease_until"]["timestampValue"].replace("Z", "+00:00"))
                if fields["lease_owner"]["stringValue"] and until > now:
                    raise LeaseBusy("Owner state is leased by another worker")
                snapshot = decode_snapshot(fields["snapshot"]["stringValue"], self.max_snapshot_bytes)
                self._restore_state(snapshot)
                fence = int(fields["fence"]["integerValue"]) + 1
            else:
                snapshot, fence = self._snapshot_state(), 1
            holder = uuid.uuid4().hex
            fields = self._fields(snapshot, holder, fence, now + timedelta(seconds=self.lease_seconds))
            document = self._commit(previous, fields)
            self.holder, self.fence, self.document = holder, fence, document
            try:
                yield
            finally:
                try:
                    previous = self._owned_document(self.document)
                    fields = self._fields(self._snapshot_state(), None, self.fence, self.clock())
                    self.document = self._commit(previous, fields)
                except (LeaseLost, OutcomeUnknown, ApiError):
                    # Never overwrite a newer worker or mask the original failure.
                    pass
                finally:
                    self.holder = None

    def _assert_holder(self):
        # Migration needs an authoritative read; ordinary checkpoints instead
        # condition their write on the last acknowledged version in this session.
        if not self.holder:
            raise LeaseLost("No active lease")
        return self._owned_document(self._read())

    def _owned_document(self, document):
        if not self.holder:
            raise LeaseLost("No active lease")
        if (not document or document.get('name') != self.name or not document.get('updateTime')
                or document.get('fields', {}).get('owner_hash', {}).get('stringValue') != self.owner_hash):
            raise LeaseLost("Acknowledged lease owner/resource/version missing")
        fields = document['fields']
        try:
            until = datetime.fromisoformat(fields["lease_until"]["timestampValue"].replace("Z", "+00:00"))
            valid = (fields["lease_owner"]["stringValue"] == self.holder
                     and int(fields["fence"]["integerValue"]) == self.fence and until > self.clock())
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise LeaseLost("Lease expired or was replaced; this worker is fenced from state")
        return document

    def checkpoint(self):
        # CAS rejects any intervening write, including a lease takeover. An extra
        # GET would add latency without strengthening that atomic condition.
        previous = self._owned_document(self.document)
        fields = self._fields(self._snapshot_state(), self.holder, self.fence,
                              self.clock() + timedelta(seconds=self.lease_seconds))
        self.document = self._commit(previous, fields)

    def before_effect(self):
        from .read_cache import invalidate
        invalidate()
        self.checkpoint()  # Verify holder/fence and renew immediately before HTTP.
        self._owned_document(self.document)  # A slow acknowledgement may outlast the renewed lease.
