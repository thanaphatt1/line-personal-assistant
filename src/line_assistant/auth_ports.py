"""Explicit token-provider interfaces. No implicit credential discovery."""

from dataclasses import dataclass, field
from typing import Protocol

from .transport import LiveDisabled


class AccessTokenProvider(Protocol):
    def __call__(self) -> str:
        """Return a refreshed purpose-scoped token without logging it."""
        ...


class DisabledTokenProvider:
    def __call__(self) -> str:
        raise LiveDisabled("Authentication/token loading is not implemented or enabled")

    def __repr__(self):
        return "DisabledTokenProvider()"


@dataclass(frozen=True, repr=False)
class TokenProviders:
    google_cloud: AccessTokenProvider = field(default_factory=DisabledTokenProvider)
    # Workspace port is Sheets-only. Calendar needs separate user authorization.
    google_workspace: AccessTokenProvider = field(default_factory=DisabledTokenProvider)
    calendar: AccessTokenProvider = field(default_factory=DisabledTokenProvider)
    line: AccessTokenProvider = field(default_factory=DisabledTokenProvider)
    model: AccessTokenProvider = field(default_factory=DisabledTokenProvider)

    def __repr__(self):
        return "TokenProviders(values=<redacted>)"
