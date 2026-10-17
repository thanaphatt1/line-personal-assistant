"""Explicit, durable one-shot runtime bot-info read; no message or public route."""

from datetime import datetime, timedelta, timezone
import json
import logging
import re

import httpx
from pydantic import SecretStr

from .google_auth import SCOPES



IDENTITY_RESULTS = frozenset({"match", "mismatch", "not_configured", "not_checked",
                              "invalid_response", "outcome_unknown", "setup_unavailable", "window_closed"})
_logger = logging.getLogger("uvicorn.error.line_assistant.line_auth_check")
_logger.setLevel(logging.INFO)


def _safe_result(status=None, identity="not_checked"):
    return {"http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "identity_match": identity if isinstance(identity, str) and identity in IDENTITY_RESULTS else "setup_unavailable"}


def _stored_result(document):
    fields = document.get("fields", {})
    status = fields.get("http_status", {}).get("integerValue")
    status = int(status) if isinstance(status, str) and re.fullmatch(r"[1-5][0-9]{2}", status) else None
    identity = fields.get("identity_match", {}).get("stringValue", "not_checked")
    return _safe_result(status, identity)


def run_line_auth_check(config, secret_loader, *, transport=None, clock=None):
    """Call only in the authorized disabled startup branch. Never retry a claim."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    result = _safe_result(identity="setup_unavailable")
    try:
        now = clock()
        if (not config.auth_check_id or config.launch_authorized or config.monthly_micro_usd != 0
                or config.auth_check_expires_at is None):
            return result
        if not now < config.auth_check_expires_at <= now + timedelta(minutes=5):
            result = _safe_result(identity="window_closed")
            return result
        if transport is not None and type(transport) is not httpx.MockTransport:
            return result
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False, timeout=5) as client:
            def request(method, url, **kwargs):
                left = (config.auth_check_expires_at - clock()).total_seconds()
                if left <= 0:
                    raise TimeoutError("Diagnostic window ended")
                return client.request(method, url, timeout=min(5, left), **kwargs)

            base = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/"
            identity = request("GET", base + "email", headers={"Metadata-Flavor": "Google"})
            if (identity.status_code != 200 or identity.headers.get("Metadata-Flavor") != "Google"
                    or identity.text.strip() != config.service_account):
                return result
            response = request("GET", base + "token", params={"scopes": SCOPES["firestore"]},
                               headers={"Metadata-Flavor": "Google"})
            if (response.status_code != 200 or response.headers.get("Metadata-Flavor") != "Google"
                    or len(response.content) > 8192):
                return result
            token = response.json().get("access_token")
            if not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(not 33 <= ord(c) <= 126 for c in token):
                return result
            google_headers = {"Authorization": "Bearer " + token}
            # Separate marker in the existing collection: owner receipts/budgets stay byte-identical.
            database = f"projects/{config.project_id}/databases/(default)"
            name = database + "/documents/assistant_pilot_states/line-auth-" + config.auth_check_id
            doc_url = "https://firestore.googleapis.com/v1/" + name
            prior = request("GET", doc_url, headers=google_headers)
            if prior.status_code == 200:
                if len(prior.content) > 8192:
                    return result
                result = _stored_result(prior.json())
                return result
            if prior.status_code != 404:
                return result
            claim = {"claimed": {"booleanValue": True}, "identity_match": {"stringValue": "not_checked"}}
            commit_url = "https://firestore.googleapis.com/v1/" + database + "/documents:commit"
            response = request("POST", commit_url, headers=google_headers, json={"writes": [{
                "update": {"name": name, "fields": claim}, "currentDocument": {"exists": False}}]})
            if response.status_code in {409, 412}:
                result = _safe_result()
                return result  # Another instance claimed it; no LINE read.
            if response.status_code != 200 or len(response.content) > 8192:
                return result
            update_time = response.json()["writeResults"][0]["updateTime"]
            # Only the already-mounted LINE access token is loaded, solely inside runtime.
            credential = secret_loader("line-channel-access-token")
            if type(credential) is not SecretStr:
                return result
            access = credential.get_secret_value()
            if not 1 <= len(access) <= 8192 or any(not 33 <= ord(c) <= 126 for c in access):
                return result
            try:
                response = request("GET", "https://api.line.me/v2/bot/info",
                                   headers={"Authorization": "Bearer " + access})
                result = _safe_result(response.status_code)
                if response.status_code == 200:
                    if len(response.content) > 8192:
                        result = _safe_result(200, "invalid_response")
                    else:
                        try:
                            info = response.json()
                            bot = info.get("userId") if isinstance(info, dict) else None
                            if not isinstance(bot, str) or not re.fullmatch(r"U[a-f0-9]{32}", bot):
                                result = _safe_result(200, "invalid_response")
                            elif not config.auth_check_expected_bot:
                                result = _safe_result(200, "not_configured")
                            else:
                                result = _safe_result(200, "match" if bot == config.auth_check_expected_bot else "mismatch")
                        except ValueError:
                            result = _safe_result(200, "invalid_response")
            except Exception:
                result = _safe_result(identity="outcome_unknown")
            # No raw body/identity/token/header is retained, even for failures.
            fields = {"http_status": {"integerValue": str(result["http_status"])} if result["http_status"] is not None else {"nullValue": None},
                      "identity_match": {"stringValue": result["identity_match"]}}
            request("POST", commit_url, headers=google_headers, json={"writes": [{
                "update": {"name": name, "fields": fields}, "updateMask": {"fieldPaths": list(fields)},
                "currentDocument": {"updateTime": update_time}}]})
    except Exception:
        pass  # No provider exception/body/credential is copied into logs or startup tracebacks.
    finally:
        _logger.info("line_auth_check_result %s", json.dumps(result, separators=(",", ":")))
    return result
