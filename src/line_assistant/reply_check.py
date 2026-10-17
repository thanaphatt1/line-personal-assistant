"""One explicitly authorized fresh-owner reply, isolated from expense accounting."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from .canary import CanaryGate, event_fingerprint
from .line_api import ReplyDispatcher
from .state import Claim, Receipt, Result
from .transport import LiveDisabled


REPLY_CHECK_KEYS = frozenset({"PILOT_REPLY_CHECK_AUTHORIZED", "PILOT_REPLY_CHECK_ID",
                              "PILOT_REPLY_CHECK_START_AT", "PILOT_REPLY_CHECK_EXPIRES_AT"})
REQUEST_TEXT = "ทดสอบตอบกลับ"
RESPONSE_TEXT = "ทดสอบตอบกลับสำเร็จ"


@dataclass(frozen=True, repr=False)
class ReplyCheckSpec:
    authorized: bool
    check_id: str
    starts_at: datetime
    expires_at: datetime

    @classmethod
    def from_mapping(cls, values):
        flag = values.get("PILOT_REPLY_CHECK_AUTHORIZED", "false")
        if flag not in {"true", "false"}:
            raise ValueError("Invalid reply-check authorization flag")
        fields = [values.get(key, "") for key in ("PILOT_REPLY_CHECK_ID", "PILOT_REPLY_CHECK_START_AT",
                                                  "PILOT_REPLY_CHECK_EXPIRES_AT")]
        if flag == "false" and not any(fields):
            return None
        if not all(isinstance(value, str) and value for value in fields):
            raise ValueError("Complete explicit reply-check configuration required")
        check_id, start, end = fields
        if not re.fullmatch(r"[a-f0-9]{32}", check_id):
            raise ValueError("Invalid reply-check ID")
        times = []
        for text in (start, end):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", text):
                raise ValueError("Reply check requires explicit UTC seconds")
            times.append(datetime.fromisoformat(text.replace("Z", "+00:00")))
        if not timedelta(0) < times[1] - times[0] <= timedelta(minutes=5):
            raise ValueError("Reply-check window must be at most five minutes")
        return cls(flag == "true", check_id, *times)

    @property
    def message(self):
        return REQUEST_TEXT

    @property
    def user_message(self):
        return REQUEST_TEXT

    @property
    def fingerprint(self):
        value = {"id": self.check_id, "start": self.starts_at.isoformat(),
                 "end": self.expires_at.isoformat(), "request": REQUEST_TEXT, "response": RESPONSE_TEXT}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def active(self, now):
        return (self.authorized and isinstance(now, datetime) and now.utcoffset() is not None
                and self.starts_at <= now < self.expires_at)

    def matches_message(self, text):
        return text == REQUEST_TEXT

    def state_owner(self, owner_id):
        # Existing FirestoreState hashes this namespace into a separate document.
        return owner_id + ":reply-check:" + self.check_id


class ReplyCheckGate(CanaryGate):
    """Reuse permanent event binding in an isolated snapshot, with only a reply allowance."""

    def reason(self, event, *, now=None):
        current = now if now is not None else self.clock()
        reason = super().reason(event, now=current)
        if reason != "accepted":
            return reason
        delivery = event.get("deliveryContext", {})
        if not isinstance(delivery, dict) or delivery.get("isRedelivery", False) is not False:
            return "event_outside_window"
        occurred = datetime.fromtimestamp(event["timestamp"] / 1000, timezone.utc)
        if not -5 <= (current - occurred).total_seconds() <= 30:
            return "event_outside_window"
        return reason

    def authorize_body(self, body):
        if not self.state.holder:
            raise LiveDisabled("Reply check requires the fenced test lease")
        run = self.current()
        receipt = self.state.receipts.get(run.event_id)
        reply = self.state.replies.get(run.event_id)
        digest = hashlib.sha256(RESPONSE_TEXT.encode()).hexdigest()
        valid = (isinstance(body, dict) and set(body) == {"replyToken", "messages"}
                 and isinstance(body.get("replyToken"), str) and 1 <= len(body["replyToken"]) <= 1000
                 and all(33 <= ord(c) <= 126 for c in body["replyToken"])
                 and body["messages"] == [{"type": "text", "text": RESPONSE_TEXT}]
                 and receipt and receipt.fingerprint == run.fingerprint
                 and receipt.result == Result("reply_check_ready", RESPONSE_TEXT)
                 and reply and reply.status == "executing" and reply.content_hash == digest)
        if not valid:
            raise LiveDisabled("Only the fixed prepared reply check is allowed")

    def reserve_attempt(self, kind):
        run = self.current()
        claim = self.state.claims.get(run.event_id)
        if (kind != "reply" or not self.state.holder or run.reply_attempted
                or run.model_attempted or run.append_attempted
                or not claim or claim.fingerprint != run.fingerprint):
            raise LiveDisabled("Reply-check allowance is unavailable")
        run.reply_attempted = True
        self.state.checkpoint()
        self.state.before_effect()
        self.current()


class ReplyCheckAssistant:
    def __init__(self, settings, state, gate, clock):
        self.settings, self.state, self.gate, self.clock = settings, state, gate, clock

    def handle(self, event):
        if not self.gate.accepts(event):
            return Result("ignored", "")
        from .pilot_http import rpc_budget
        with rpc_budget(24, seconds=10), self.state.session():
            if not self.gate.bind(event):
                self.gate.log_diagnostic(event, "binding_mismatch")
                return Result("ignored", "")
            event_id = event["webhookEventId"]
            fingerprint = event_fingerprint(event)
            previous = self.state.receipts.get(event_id)
            if previous:
                return previous.result if previous.fingerprint == fingerprint else Result("ignored", "")
            result = Result("reply_check_ready", RESPONSE_TEXT)
            expires = self.clock() + timedelta(seconds=self.settings.webhook_ttl_seconds)
            self.state.claims[event_id] = Claim(fingerprint, expires)
            self.state.receipts[event_id] = Receipt(fingerprint, result, expires)
            self.state.checkpoint()
            return result


class ReplyCheckReplies(ReplyDispatcher):
    def deliver(self, event, result, received_at):
        if result != Result("reply_check_ready", RESPONSE_TEXT) or not self.assistant.gate.accepts(event):
            return "skipped"
        from .pilot_http import rpc_budget
        with rpc_budget(24, seconds=10):
            return super().deliver(event, result, received_at)
