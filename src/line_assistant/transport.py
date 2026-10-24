"""HTTP request transport with a hard network gate for this authorized milestone.

Uses verified REST contracts, not guessed SDK/model names. Credentials are injected
explicitly; no environment, ADC, keychain, file, or metadata credential discovery.
Only httpx.MockTransport can be injected today. No live opt-in is exposed.
"""

from typing import Callable, Literal
import json
import logging
from time import monotonic

import httpx


LINE_ERROR_CATEGORIES = frozenset({
    "invalid_reply_token", "authentication_failed", "permission_denied", "not_found",
    "invalid_message", "bad_request", "rate_limited", "provider_server_error",
    "provider_http_error", "invalid_acknowledgement", "provider_outcome_unknown",
    "unexpected_error", "token_deadline_elapsed",
})
_line_logger = logging.getLogger("uvicorn.error.line_assistant.line_reply")
_line_logger.setLevel(logging.INFO)


def line_error_category(response):
    """Map bounded LINE errors to fixed labels; never retain provider prose."""
    status = response.status_code
    if 200 <= status < 300:
        return None
    message, details = None, []
    if len(response.content) <= 8192:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message, details = payload.get("message"), payload.get("details", [])
        except ValueError:
            pass
    if message == "Invalid reply token":
        return "invalid_reply_token"  # Does not distinguish expired vs already used.
    if isinstance(message, str) and message.startswith("Authentication failed due to the following reason:"):
        return "authentication_failed"
    if status == 400 and isinstance(details, list) and any(
        isinstance(item, dict) and isinstance(item.get("property"), str)
        and item["property"].startswith("messages") for item in details
    ):
        return "invalid_message"
    return {400: "bad_request", 401: "authentication_failed", 403: "permission_denied",
            404: "not_found", 429: "rate_limited"}.get(
        status, "provider_server_error" if 500 <= status < 600 else "provider_http_error")


def log_line_response(response, elapsed_ms):
    category = line_error_category(response)
    _line_logger.info("line_http_diagnostic %s", json.dumps({
        "http_status": response.status_code, "error_category": category,
        "duration_ms": min(86400000, max(0, elapsed_ms)),
    }, separators=(",", ":")))
    return category


class LiveDisabled(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str | None = None, *, category: str | None = None):
        if type(status) is not int or not 100 <= status <= 599:
            raise ValueError("Invalid provider HTTP status")
        super().__init__(f"Provider HTTP {status}")  # Never include bodies/tokens.
        self.status = status
        self.code = code
        self.category = category if isinstance(category, str) and category in LINE_ERROR_CATEGORIES else None


class OutcomeUnknown(RuntimeError):
    def __init__(self,message,*,reason=None):
        super().__init__(message)
        self.reason=reason if reason in {'timeout','transport','response_too_large','invalid_response'} else None


class NotReady(RuntimeError):
    pass


class ReadCapacity(NotReady):
    """A complete read would leave too little of the owner turn to finish and reply."""


class WriteBlocked(RuntimeError):
    """Adapter setup rejected the operation before any write RPC was sent."""


class DenyNetwork(httpx.BaseTransport):
    def handle_request(self, request):
        raise LiveDisabled("Live network is disabled; inject a MockTransport")


ORIGINS = {"line": "https://api.line.me", "sheets": "https://sheets.googleapis.com",
           "calendar": "https://www.googleapis.com", "firestore": "https://firestore.googleapis.com"}


class RestClient:
    def __init__(self, service: Literal["line", "sheets", "calendar", "firestore"],
                 transport: httpx.MockTransport | None = None,
                 token: Callable[[], str] | None = None):
        if service not in ORIGINS:
            raise ValueError("Server-owned provider required")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise LiveDisabled("Only a mocked HTTP transport is authorized")
        self.service = service
        self.mocked = transport is not None
        self.token = token or (lambda: "placeholder-not-a-credential")
        self.client = httpx.Client(base_url=ORIGINS[service], transport=transport or DenyNetwork(),
                                   timeout=10, follow_redirects=False, trust_env=False)

    def request(self, method: str, path: str, *, body=None, params=None, headers=None):
        from .action_trace import span
        with span('provider_'+self.service):
            return self._request(method,path,body=body,params=params,headers=headers)

    def _request(self, method, path, *, body=None, params=None, headers=None):
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError("Relative provider path required")
        # Deny before even consulting an injected token provider.
        if not self.mocked:
            raise LiveDisabled("Live network/credential use is disabled")
        token = self.token()
        if not isinstance(token, str) or not token or any(ord(char) < 32 for char in token):
            raise NotReady("Explicit credential provider returned an invalid token")
        outgoing = dict(headers or {})
        if any(key.lower() in {"authorization", "host"} for key in outgoing):
            raise ValueError("Transport owns authorization and host headers")
        outgoing["Authorization"] = f"Bearer {token}"
        started = monotonic()
        try:
            response = self.client.request(method, path, json=body, params=params, headers=outgoing)
        except httpx.TransportError:
            raise OutcomeUnknown("Provider acknowledgement unavailable") from None
        category = log_line_response(response, round((monotonic() - started) * 1000)) if self.service == "line" else None
        if not 200 <= response.status_code < 300:
            try:
                code = response.json().get("error", {}).get("status")
            except (ValueError, AttributeError):
                code = None
            if code not in {"FAILED_PRECONDITION", "ABORTED", "ALREADY_EXISTS"}:
                code = None
            raise ApiError(response.status_code, code, category=category) from None
        if response.status_code == 204 or not response.content:
            return {}
        try:
            value = response.json()
        except ValueError:
            raise OutcomeUnknown("Provider returned invalid acknowledgement") from None
        if self.service=='firestore' and method=='POST' and path.endswith(':runQuery') and isinstance(value,list):return value
        if not isinstance(value, dict):
            raise OutcomeUnknown("Provider returned invalid acknowledgement")
        return value

    def close(self):
        self.client.close()
