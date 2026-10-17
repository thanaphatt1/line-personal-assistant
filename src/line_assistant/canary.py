"""One immutable owner-only synthetic canary, using the existing durable snapshot.

The nonce and absolute window are supplied at authorized launch, never generated
or started at import/startup. A consumed canary is deliberately not TTL-deleted.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import logging
import re

from .adapters import BANGKOK
from .contracts import AddTransactions, ToolRequest
from .planner import PlannerNotice
from .state import CanaryRun
from .transport import LiveDisabled


CANARY_KEYS = frozenset({"PILOT_CANARY_NONCE", "PILOT_CANARY_DATE",
                         "PILOT_CANARY_START_AT", "PILOT_CANARY_EXPIRES_AT"})

# Use the server's existing console handler, without logging payloads or exceptions.
_logger = logging.getLogger("uvicorn.error.line_assistant.canary")
_logger.setLevel(logging.INFO)  # The existing server runs at warning; enable only these safe records.
_ACCEPTED = frozenset({"accepted", "accepted_normalized"})
_REASONS = _ACCEPTED | frozenset({
    "empty_events", "window_not_active", "source_not_user", "owner_mismatch",
    "event_not_message", "mode_not_active", "message_not_text", "literal_mismatch",
    "invalid_event_id", "invalid_timestamp", "invalid_message_id", "invalid_reply_token",
    "event_outside_window", "binding_mismatch",
})
_EVENT_TYPES = frozenset({"message", "follow", "unfollow", "postback", "join", "leave"})


def normalized_canary_text(text):
    # Only whitespace changes are harmless. Keep case, amount, date and nonce exact.
    return " ".join(text.split()) if isinstance(text, str) else None


def _window_status(spec, now):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        return "invalid_clock"
    if now < spec.starts_at:
        return "before_window"
    return "active" if now < spec.expires_at else "expired"


@dataclass(frozen=True, repr=False)
class CanarySpec:
    nonce: str
    day: date
    starts_at: datetime
    expires_at: datetime

    def __repr__(self):
        return "CanarySpec(values=<redacted>)"

    @classmethod
    def from_mapping(cls, values):
        fields = [values.get(key, "") for key in sorted(CANARY_KEYS)]
        if not any(fields):
            return None
        if not all(isinstance(value, str) and value for value in fields):
            raise ValueError("Complete explicit canary configuration required")
        nonce, day = values["PILOT_CANARY_NONCE"], values["PILOT_CANARY_DATE"]
        if not re.fullmatch(r"[a-f0-9]{32}", nonce) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("Invalid canary nonce/date")
        times = []
        for key in ("PILOT_CANARY_START_AT", "PILOT_CANARY_EXPIRES_AT"):
            text = values[key]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", text):
                raise ValueError("Canary window needs explicit UTC seconds")
            times.append(datetime.fromisoformat(text.replace("Z", "+00:00")))
        starts, expires = times
        parsed_day = date.fromisoformat(day)
        if not timedelta(0) < expires - starts <= timedelta(minutes=10) or parsed_day != starts.astimezone(BANGKOK).date():
            raise ValueError("Canary window/date exceeds reviewed bounds")
        return cls(nonce, parsed_day, starts, expires)

    @property
    def note(self):
        return "LPA_CANARY_" + self.nonce

    @property
    def message(self):
        return f"รายจ่าย TEST Coffee 1 วันที่ {self.day.isoformat()} หมายเหตุ {self.note}"

    @property
    def user_message(self):
        return f"TEST Coffee 1 วันที่ {self.day.isoformat()}"

    @property
    def fingerprint(self):
        value = {"message": self.message, "starts_at": self.starts_at.isoformat(),
                 "expires_at": self.expires_at.isoformat()}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def active(self, now):
        return isinstance(now, datetime) and now.utcoffset() is not None and self.starts_at <= now < self.expires_at

    def matches_entries(self, args):
        if not isinstance(args, AddTransactions) or len(args.entries) != 1:
            return False
        entry = args.entries[0]
        return (entry.item == "TEST Coffee" and entry.kind == "expense" and Decimal(entry.amount) == Decimal("1")
                and entry.date == self.day and entry.note == self.note)

    def matches_message(self, text):
        return normalized_canary_text(text) in {self.message, self.user_message}


def event_fingerprint(event):
    return hashlib.sha256(json.dumps({"user": event["source"]["userId"], "timestamp": event["timestamp"],
                                     "text": event["message"]["text"], "message_id": event["message"]["id"]},
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CanaryGate:
    def __init__(self, spec, state, owner_id, clock):
        self.spec, self.state, self.owner_id, self.clock = spec, state, owner_id, clock

    def reason(self, event, *, now=None):
        if not self.spec.active(now if now is not None else self.clock()):
            return "window_not_active"
        source, message = event.get("source"), event.get("message")
        if not isinstance(source, dict) or source.get("type") != "user":
            return "source_not_user"
        if source.get("userId") != self.owner_id:
            return "owner_mismatch"
        if event.get("type") != "message":
            return "event_not_message"
        if event.get("mode", "active") != "active":
            return "mode_not_active"
        if not isinstance(message, dict) or message.get("type") != "text":
            return "message_not_text"
        if not self.spec.matches_message(message.get("text")):
            return "literal_mismatch"
        event_id, stamp, token, message_id = (event.get("webhookEventId"), event.get("timestamp"),
                                            event.get("replyToken"), message.get("id"))
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event_id):
            return "invalid_event_id"
        if type(stamp) is not int or stamp < 0:
            return "invalid_timestamp"
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128:
            return "invalid_message_id"
        if not isinstance(token, str) or not 1 <= len(token) <= 1000 or any(not 33 <= ord(c) <= 126 for c in token):
            return "invalid_reply_token"
        try:
            occurred_at = datetime.fromtimestamp(stamp / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return "invalid_timestamp"
        if not self.spec.active(occurred_at):
            return "event_outside_window"
        return "accepted" if message["text"] in {self.spec.message, self.spec.user_message} else "accepted_normalized"

    def accepts(self, event):
        return self.reason(event) in _ACCEPTED

    def log_diagnostic(self, event, reason, *, event_count=1, now=None):
        """Only fixed reason codes, allowlisted event types and booleans leave here."""
        if reason not in _REASONS or type(event_count) is not int or not 0 <= event_count <= 5:
            raise ValueError("Invalid safe diagnostic")
        source = event.get("source") if event is not None else None
        message = event.get("message") if event is not None else None
        text = message.get("text") if isinstance(message, dict) else None
        event_type = event.get("type") if event is not None else None
        event_type = event_type if isinstance(event_type, str) and event_type in _EVENT_TYPES else "other"
        payload = {
            "reason_code": reason, "event_type": event_type if event is not None else "none",
            "event_count": event_count,
            "owner_match": isinstance(source, dict) and source.get("userId") == self.owner_id,
            "literal_match": self.spec.matches_message(text),
            "literal_exact_match": isinstance(text, str) and text in {self.spec.message, self.spec.user_message},
            "canary_window_status": _window_status(self.spec, now if now is not None else self.clock()),
        }
        _logger.info("canary_diagnostic %s", json.dumps(payload, separators=(",", ":")))

    def observe_verified_events(self, events):
        # Invoked only after HMAC, JSON-envelope and event-count validation.
        now = self.clock()
        if not events:
            self.log_diagnostic(None, "empty_events", event_count=0, now=now)
        for event in events:
            self.log_diagnostic(event, self.reason(event, now=now), event_count=len(events), now=now)

    def bind(self, event):
        """Call under the owner lease; permanently bind this nonce to one event."""
        if not self.accepts(event):
            return False
        run = self.state.canary
        if run is None:
            run = CanaryRun(self.spec.fingerprint, event["webhookEventId"], event_fingerprint(event),
                            event["timestamp"], self.spec.starts_at, self.spec.expires_at)
            self.state.canary = run
            self.state.checkpoint()
        return (run.spec_hash == self.spec.fingerprint and run.event_id == event["webhookEventId"]
                and run.fingerprint == event_fingerprint(event) and run.timestamp == event["timestamp"]
                and run.starts_at == self.spec.starts_at and run.expires_at == self.spec.expires_at)

    def current(self):
        run = self.state.canary
        if (run is None or run.spec_hash != self.spec.fingerprint or run.starts_at != self.spec.starts_at
                or run.expires_at != self.spec.expires_at or not self.spec.active(self.clock())):
            raise LiveDisabled("Canary is unavailable, changed or outside its window")
        return run

    def reserve_attempt(self, kind):
        """Persist a nonrefundable allowance before its provider boundary."""
        if kind not in {"model", "append", "reply"}:
            raise LiveDisabled("Unsupported canary effect")
        run = self.current()
        claim = self.state.claims.get(run.event_id)
        if (not claim or claim.fingerprint != run.fingerprint or getattr(run, kind + "_attempted")
                or (kind in {"append", "reply"} and not run.model_attempted)
                or (kind == "reply" and not run.append_attempted)):
            raise LiveDisabled("Canary allowance is unavailable")
        setattr(run, kind + "_attempted", True)
        self.state.checkpoint()
        self.state.before_effect()
        self.current()  # A checkpoint/token lookup must not extend the window.


class CanaryPlanner:
    def __init__(self, planner, spec):
        self.planner, self.spec = planner, spec

    def plan(self, text, occurred_at):
        # Enrich the short owner message BEFORE planning with the frozen date/note.
        # Model output must still match every field; never repair it after validation.
        if not self.spec.matches_message(text):
            return PlannerNotice("canary_blocked", "")
        planned = self.planner.plan(self.spec.message, occurred_at)
        if isinstance(planned, ToolRequest) and planned.name == "add_transactions":
            if self.spec.matches_entries(planned.validated_arguments()):
                return planned
        # No guessed expense, summary, clarification or success reply in this run.
        return PlannerNotice("canary_blocked", "")
