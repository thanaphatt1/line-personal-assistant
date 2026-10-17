"""Keyless Google token contracts, exercised only through injected MockTransport.

The Cloud Run provider implements the attached-service-account metadata branch
used by ADC, not ADC discovery. Local impersonation requires an explicit caller
token callback; it never reads local ADC, environment variables or key files.
No live authentication transport or opt-in exists in this milestone.
"""

from datetime import datetime, timedelta, timezone
import re
from threading import RLock
from typing import Callable, Literal
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from .auth_ports import AccessTokenProvider, TokenProviders
from .transport import DenyNetwork, LiveDisabled


SCOPES = {
    "calendar_events": "https://www.googleapis.com/auth/calendar.events",
    "sheets": "https://www.googleapis.com/auth/spreadsheets",
    "sheets_readonly": "https://www.googleapis.com/auth/spreadsheets.readonly",
    "firestore": "https://www.googleapis.com/auth/datastore",
}
Purpose = Literal["sheets", "sheets_readonly", "firestore", "calendar_events"]
_ACCOUNT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com")
_PREFIX = "/computeMetadata/v1/instance/service-accounts/default"
_REFRESH_MARGIN = timedelta(seconds=60)


class AuthError(RuntimeError):
    """Fixed diagnostic text only; provider bodies and credentials are never copied."""


def _token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 16384 or any(not 33 <= ord(c) <= 126 for c in value):
        raise AuthError("Authentication returned an invalid access token")
    return value


class _CachedTokenProvider:
    def __init__(self, purpose: Purpose, account: str, origin: str,
                 transport: httpx.MockTransport | None, clock: Callable[[], datetime] | None):
        if purpose not in SCOPES:
            raise AuthError("Only Sheets or Firestore authentication is prepared")
        if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
            raise AuthError("Explicit dedicated service-account identity required")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise LiveDisabled("Only mocked authentication transport is authorized")
        self._scope = SCOPES[purpose]
        self._account = account
        self._mocked = transport is not None
        self._client = httpx.Client(base_url=origin, transport=transport or DenyNetwork(),
                                    timeout=10, follow_redirects=False, trust_env=False)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._cached: SecretStr | None = None
        self._expires: datetime | None = None
        self._last_now: datetime | None = None

    def __repr__(self):
        return f"{type(self).__name__}(values=<redacted>, live_enabled=False)"

    def _now(self):
        try:
            value = self._clock()
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError
            return value.astimezone(timezone.utc)
        except Exception:
            raise AuthError("Authentication requires a valid aware clock") from None

    def __call__(self):
        # Gate before consulting caller credentials, the clock or even a cached token.
        if not self._mocked:
            raise LiveDisabled("Live authentication/credential use is disabled")
        with self._lock:
            try:
                now = self._now()
                if self._last_now is not None and now < self._last_now:
                    raise AuthError("Authentication clock moved backwards")
                self._last_now = now
                if self._cached is not None and self._expires is not None and now + _REFRESH_MARGIN < self._expires:
                    return self._cached.get_secret_value()
                # No stale token fallback when a refresh or identity check fails.
                self._cached = self._expires = None
                token, expires = self._refresh(now)
                completed = self._now()
                if completed < now or expires <= completed + _REFRESH_MARGIN:
                    raise AuthError("Authentication token lifetime is insufficient")
                self._last_now = completed
                self._cached, self._expires = SecretStr(_token(token)), expires
                return self._cached.get_secret_value()
            except Exception:
                self._cached = self._expires = None
                raise

    def _request(self, method, path, *, params=None, body=None, headers=None, metadata=False):
        try:
            response = self._client.request(method, path, params=params, json=body, headers=headers)
        except Exception:
            raise AuthError("Authentication response unavailable") from None
        if response.status_code != 200:
            raise AuthError(f"Authentication HTTP {response.status_code}")
        if metadata and response.headers.get("Metadata-Flavor") != "Google":
            raise AuthError("Authentication metadata response was not verified")
        return response

    @staticmethod
    def _json(response):
        try:
            value = response.json()
        except ValueError:
            raise AuthError("Authentication returned an invalid response") from None
        if not isinstance(value, dict):
            raise AuthError("Authentication returned an invalid response")
        return value

    def close(self):
        with self._lock:
            self._cached = self._expires = None
            self._client.close()


class CloudRunTokenProvider(_CachedTokenProvider):
    """Cloud Run attached-SA metadata flow with fixed purpose-specific scope.

    Cloud Run honours requested metadata scopes; Compute Engine does not. Do not
    reuse this contract as a generic GCE/ADC credential discovery implementation.
    """

    def __init__(self, purpose: Purpose, expected_service_account_email: str, *,
                 transport: httpx.MockTransport | None = None, clock=None):
        super().__init__(purpose, expected_service_account_email, "http://metadata.google.internal", transport, clock)

    def _refresh(self, now):
        headers = {"Metadata-Flavor": "Google"}
        identity = self._request("GET", _PREFIX + "/email", headers=headers, metadata=True)
        if identity.text.strip() != self._account:
            raise AuthError("Attached service-account identity does not match configuration")
        response = self._request("GET", _PREFIX + "/token", params={"scopes": self._scope},
                                 headers=headers, metadata=True)
        value = self._json(response)
        lifetime = value.get("expires_in")
        if type(lifetime) is not int or not 60 < lifetime <= 3600 or value.get("token_type") != "Bearer":
            raise AuthError("Authentication returned an invalid token lifetime or type")
        return _token(value.get("access_token")), now + timedelta(seconds=lifetime)


class ImpersonatedTokenProvider(_CachedTokenProvider):
    """Local IAM impersonation contract with an explicit caller token provider.

    Caller credentials need IAM/cloud-platform scope and target getAccessToken
    permission. No local ADC login, discovery or credential loading occurs here.
    """

    def __init__(self, purpose: Purpose, target_service_account_email: str, caller_token: AccessTokenProvider, *,
                 transport: httpx.MockTransport | None = None, clock=None):
        if not callable(caller_token):
            raise AuthError("Explicit impersonation caller provider required")
        super().__init__(purpose, target_service_account_email, "https://iamcredentials.googleapis.com", transport, clock)
        self._caller_token = caller_token

    def _refresh(self, now):
        try:
            caller = _token(self._caller_token())
        except Exception:
            raise AuthError("Impersonation caller credentials unavailable") from None
        response = self._request("POST", "/v1/projects/-/serviceAccounts/" + quote(self._account, safe="") + ":generateAccessToken",
                                 body={"scope": [self._scope], "lifetime": "3600s"},
                                 headers={"Authorization": "Bearer " + caller})
        value = self._json(response)
        try:
            expiry = value.get("expireTime")
            if not isinstance(expiry, str) or len(expiry) > 64 or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", expiry
            ):
                raise ValueError
            expires = datetime.fromisoformat(expiry.replace("Z", "+00:00")).astimezone(timezone.utc)
            if not now + _REFRESH_MARGIN < expires <= now + timedelta(seconds=3600):
                raise ValueError
        except (ValueError, OverflowError):
            raise AuthError("Authentication returned an invalid token lifetime") from None
        return _token(value.get("accessToken")), expires


def cloud_run_token_providers(expected_service_account_email: str, *, transport=None, clock=None):
    return TokenProviders(
        google_cloud=CloudRunTokenProvider("firestore", expected_service_account_email, transport=transport, clock=clock),
        google_workspace=CloudRunTokenProvider("sheets", expected_service_account_email, transport=transport, clock=clock),
    )


def local_impersonated_token_providers(target_service_account_email: str, caller_token: AccessTokenProvider, *, transport=None, clock=None):
    return TokenProviders(
        google_cloud=ImpersonatedTokenProvider("firestore", target_service_account_email, caller_token, transport=transport, clock=clock),
        google_workspace=ImpersonatedTokenProvider("sheets", target_service_account_email, caller_token, transport=transport, clock=clock),
    )
