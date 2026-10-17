from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # Deliberately public dummy values, usable only with mock integrations.
    owner_id: str = "mock-owner"
    channel_secret: str = field(default="mock-channel-secret", repr=False)
    mode: str = "mock"
    timezone: str = "Asia/Bangkok"
    pending_ttl_seconds: int = 1800
    last_action_ttl_seconds: int = 86400
    webhook_ttl_seconds: int = 30 * 86400
    max_new_event_age_seconds: int = 86400
    max_future_skew_seconds: int = 300

    def __post_init__(self):
        if self.mode not in {"mock", "synthetic_pilot", "production"}:
            raise ValueError("Unsupported runtime mode")
        if self.mode in {"synthetic_pilot", "production"}:
            import re
            if not re.fullmatch(r"U[a-f0-9]{32}", self.owner_id) or self.channel_secret == "mock-channel-secret":
                raise ValueError("Pilot requires an explicitly configured LINE owner/signature key")
        if not self.owner_id or not self.channel_secret:
            raise ValueError("Owner and signature key cannot be empty")
        if self.timezone != "Asia/Bangkok":
            raise ValueError("This initial slice requires Asia/Bangkok")
        if any(value <= 0 for value in (
            self.pending_ttl_seconds, self.last_action_ttl_seconds,
            self.webhook_ttl_seconds, self.max_new_event_age_seconds,
            self.max_future_skew_seconds,
        )):
            raise ValueError("Retention and freshness windows must be positive")
