"""Single-owner LINE replies with persisted attempt markers; no push fallback."""

from datetime import timedelta
import hashlib
import json
import logging
from time import monotonic

from .state import Reply
from .events import fingerprint as event_fingerprint
from .transport import ApiError, OutcomeUnknown, RestClient, LINE_ERROR_CATEGORIES

_logger = logging.getLogger("uvicorn.error.line_assistant.line_reply")
_logger.setLevel(logging.INFO)


class InvalidReplyAcknowledgement(OutcomeUnknown):
    pass


def log_reply(reply):
    _logger.info("line_reply_diagnostic %s", json.dumps({
        "outcome": reply.status if reply.status in {"sent", "rejected", "uncertain", "expired"} else "unknown",
        "http_status": reply.http_status if type(reply.http_status) is int and 100 <= reply.http_status <= 599 else None,
        "error_category": reply.error_category if isinstance(reply.error_category, str) and reply.error_category in LINE_ERROR_CATEGORIES else None,
        **{k: getattr(reply, k) if type(getattr(reply, k)) is int and 0 <= getattr(reply, k) <= 86400000 else None
           for k in ("receipt_elapsed_ms", "attempt_duration_ms")},
    }, separators=(",", ":")))


def bounded_text(text):
    # LINE counts UTF-16 units; avoid splitting a surrogate pair in a long title.
    encoded = text.encode("utf-16-le")
    if len(encoded) <= 10000:
        return text
    return encoded[:9998].decode("utf-16-le", errors="ignore") + "…"

def rendered_message(result):
    return result.data.get('line_message') or {'type': 'text', 'text': bounded_text(result.message)}

def visible_text(result):
    """Text in the delivered message body, excluding hidden action metadata."""
    message = rendered_message(result)
    if message.get('type') == 'text':
        return message.get('text', '')
    def texts(node):
        if not isinstance(node, dict):
            return []
        if node.get('type') == 'text':
            return [node.get('text', '')]
        return [text for child in node.get('contents', []) for text in texts(child)]
    body = message.get('contents', {}).get('body', {})
    return '\n'.join(texts(body))

def message_hash(message):
    content = message['text'] if message.get('type') == 'text' else json.dumps(message, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


class LineReplyApi:
    def __init__(self, client: RestClient):
        if client.service != "line":
            raise ValueError("LINE transport required")
        self.client = client

    def send(self, reply_token, text):
        if not isinstance(reply_token, str) or not reply_token or len(reply_token) > 1000:
            raise ValueError("Fresh webhook reply token required")
        content = text if isinstance(text, dict) else {'type': 'text', 'text': bounded_text(text)}
        body = {"replyToken": reply_token, "messages": [content]}
        # Retry-key support for push does not authorize adding it to replies.
        return self.client.request("POST", "/v2/bot/message/reply", body=body)


class ReplyDispatcher:
    def __init__(self, api: LineReplyApi, assistant):
        self.api, self.assistant = api, assistant

    def deliver(self, event, result, received_at):
        settings, state = self.assistant.settings, self.assistant.state
        source = event.get("source", {})
        if (not isinstance(source, dict) or source.get("type") != "user" or source.get("userId") != settings.owner_id
                or not result.message or event.get("mode", "active") != "active"):
            return "skipped"
        token, event_id = event.get("replyToken"), event.get("webhookEventId")
        if not isinstance(token, str) or not token:
            return "no_token"
        fingerprint = event_fingerprint(event)
        rendered = rendered_message(result)
        content_hash = message_hash(rendered)
        with state.session():
            now = self.assistant.clock()
            state.expire(now)
            if state.workbook_id and state.event_workbooks.get(event_id) != state.workbook_id:
                return "skipped"
            if getattr(self.assistant, "production", False) and state.workbook_id != self.assistant.sheets.target.spreadsheet_id:
                return "skipped"
            receipt = state.receipts.get(event_id)
            if not receipt or receipt.fingerprint != fingerprint or receipt.result != result:
                return "skipped"  # A model/changed replay cannot manufacture a reply.
            reply = state.replies.get(event_id)
            if reply:
                if reply.status in {"executing", "uncertain"}:
                    reply.status = "uncertain"
                    state.checkpoint()
                    return "uncertain"
                if reply.status != "prepared":
                    return reply.status
                if reply.content_hash != content_hash:
                    return "content_changed"
            else:
                reply = Reply(content_hash, received_at + timedelta(seconds=settings.webhook_ttl_seconds),
                              token_deadline=received_at + timedelta(seconds=55))
                state.replies[event_id] = reply
                state.checkpoint()
            age = now.timestamp() - event["timestamp"] / 1000
            redelivery = event.get("deliveryContext", {}).get("isRedelivery", False)
            if now >= (reply.token_deadline or reply.expires_at) or (redelivery and age >= 20 * 60):
                reply.status = "expired"
                reply.error_category = "token_deadline_elapsed"
                state.checkpoint()
                log_reply(reply)
                return "expired"
            reply.status = "executing"
            reply.receipt_elapsed_ms = min(86400000, max(0, round((now - received_at).total_seconds() * 1000)))
            state.checkpoint()
            state.before_effect()
            started = monotonic()
            try:
                response = self.api.send(token, rendered if rendered['type'] == 'flex' else result.message)
                sent = response.get("sentMessages", [])
                if (not isinstance(sent, list) or len(sent) != 1 or not isinstance(sent[0], dict)
                        or type(sent[0].get("id")) not in {str, int} or sent[0]["id"] == ""):
                    raise InvalidReplyAcknowledgement("Invalid reply acknowledgement")
            except ApiError as error:
                reply.status = "rejected" if error.status in {400, 401, 403, 404} else "uncertain"
                reply.http_status = error.status
                reply.error_category = error.category or "provider_http_error"
            except InvalidReplyAcknowledgement:
                reply.status, reply.error_category = "uncertain", "invalid_acknowledgement"
            except OutcomeUnknown:
                reply.status, reply.error_category = "uncertain", "provider_outcome_unknown"
            except Exception:
                reply.status, reply.error_category = "uncertain", "unexpected_error"
            else:
                reply.status = "sent"
                if getattr(state,'raw_history_enabled',False):
                    state.mark_history_delivered(event_id,result,at=self.assistant.clock())
            reply.attempt_duration_ms = min(86400000, max(0, round((monotonic() - started) * 1000)))
            state.checkpoint()
            log_reply(reply)
            return reply.status
