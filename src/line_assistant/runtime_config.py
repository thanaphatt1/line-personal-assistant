"""Explicit, redacted configuration staging. Never reads the process environment.

Live is a configuration profile, not an executable mode. Nothing here authenticates,
constructs providers, grants access or changes the transport's hard network gate.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re

from pydantic import SecretStr


class ConfigError(ValueError):
    """Messages contain only fixed text and recognized key names, never values."""


ENV_KEYS = frozenset({
    "APP_MODE", "APP_ENVIRONMENT", "LIVE_REQUESTS_ENABLED", "APP_TIMEZONE",
    "LINE_OWNER_ID", "LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN",
    "GCP_PROJECT_ID", "GCP_REGION", "GCP_RUNTIME_SERVICE_ACCOUNT_EMAIL", "FIRESTORE_DATABASE_ID",
    "SHEETS_SPREADSHEET_ID", "SHEETS_EXPENSE_TAB", "SHEETS_DATABASE_TAB",
    "SHEETS_EXPENSE_SHEET_ID", "SHEETS_DATABASE_SHEET_ID", "SHEETS_MAX_ROWS",
    "CALENDAR_ID", "MODEL_PROVIDER", "MODEL_ID", "MODEL_API_KEY",
})


@dataclass(frozen=True, repr=False)
class RuntimeConfig:
    mode: str
    environment: str
    timezone: str
    owner_id: str | None
    channel_secret: SecretStr | None = field(repr=False)
    channel_access_token: SecretStr | None = field(repr=False)
    project_id: str | None
    region: str | None
    runtime_service_account_email: str | None
    firestore_database_id: str
    sheet_spreadsheet_id: str | None
    sheet_expense_tab: str
    sheet_database_tab: str
    sheet_expense_sheet_id: int | None
    sheet_database_sheet_id: int | None
    sheet_max_rows: int
    calendar_id: str | None
    model_provider: str | None
    model_id: str | None
    model_api_key: SecretStr | None = field(repr=False)
    # Not an init argument; no configuration can authorize provider calls.
    live_requests_enabled: bool = field(default=False, init=False)

    def __repr__(self):
        return "RuntimeConfig(values=<redacted>, live_requests_enabled=False)"

    def safe_summary(self):
        # Do not log IDs, paths, identity strings or any supplied values.
        return {"mode": self.mode, "environment": self.environment, "live_requests_enabled": False,
                "line_configured": bool(self.owner_id and self.channel_secret and self.channel_access_token),
                "gcp_configured": bool(self.project_id), "sheet_configured": bool(self.sheet_spreadsheet_id),
                "calendar_configured": bool(self.calendar_id), "model_configured": bool(self.model_provider and self.model_id)}


def _text(values, key, default=None):
    value = values.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigError(f"Invalid {key}")
    return value.strip() or None


def _choice(values, key, choices, default):
    value = _text(values, key, default)
    if value not in choices:
        raise ConfigError(f"Invalid {key}")
    return value


def _pattern(values, key, pattern, default=None):
    value = _text(values, key, default)
    if value is not None and not re.fullmatch(pattern, value):
        raise ConfigError(f"Invalid {key}")
    return value


def _number(values, key, minimum, maximum, default=None):
    value = _text(values, key, default)
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]{1,10}", value) or not minimum <= int(value) <= maximum:
        raise ConfigError(f"Invalid {key}")
    return int(value)


def _secret(values, key):
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Invalid {key}")
    if value == "":
        return None
    if (not 1 <= len(value) <= 8192
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ConfigError(f"Invalid {key}")
    return SecretStr(value)


def load_config(values: Mapping[str, str]) -> RuntimeConfig:
    """Read only caller-supplied values. Unrelated process keys are ignored.

Recognized-prefix typos fail closed. No os.environ, dotenv discovery, credentials,
metadata server, file imports or model/provider lookup is performed.
"""
    if not isinstance(values, Mapping):
        raise ConfigError("Explicit configuration mapping required")
    prefixes = ("APP_", "LIVE_", "LINE_", "GCP_", "FIRESTORE_", "SHEETS_", "CALENDAR_", "MODEL_")
    if any(isinstance(key, str) and key.startswith(prefixes) and key not in ENV_KEYS for key in values):
        raise ConfigError("Unknown application configuration key")
    mode = _choice(values, "APP_MODE", {"mock", "live"}, "mock")
    environment = _choice(values, "APP_ENVIRONMENT", {"local", "cloud_run"}, "local")
    _choice(values, "LIVE_REQUESTS_ENABLED", {"false"}, "false")
    timezone = _choice(values, "APP_TIMEZONE", {"Asia/Bangkok"}, "Asia/Bangkok")
    owner = _pattern(values, "LINE_OWNER_ID", r"[A-Za-z0-9_-]{1,128}")
    secret, token = _secret(values, "LINE_CHANNEL_SECRET"), _secret(values, "LINE_CHANNEL_ACCESS_TOKEN")
    project = _pattern(values, "GCP_PROJECT_ID", r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
    region = _pattern(values, "GCP_REGION", r"[a-z]+-[a-z]+[0-9]")
    service_account = _pattern(values, "GCP_RUNTIME_SERVICE_ACCOUNT_EMAIL",
                               r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com")
    database = _pattern(values, "FIRESTORE_DATABASE_ID", r"(?:\(default\)|[a-z][a-z0-9-]{2,61}[a-z0-9])", "(default)")
    if database is None:
        raise ConfigError("Invalid FIRESTORE_DATABASE_ID")
    spreadsheet = _pattern(values, "SHEETS_SPREADSHEET_ID", r"[A-Za-z0-9_-]{20,128}")
    expense_tab = _text(values, "SHEETS_EXPENSE_TAB", "All Expenses")
    database_tab = _text(values, "SHEETS_DATABASE_TAB", "Database")
    if not expense_tab or not database_tab or max(len(expense_tab), len(database_tab)) > 100 or expense_tab == database_tab:
        raise ConfigError("Invalid SHEETS_EXPENSE_TAB or SHEETS_DATABASE_TAB")
    expense_id = _number(values, "SHEETS_EXPENSE_SHEET_ID", 0, 2_147_483_647)
    database_id = _number(values, "SHEETS_DATABASE_SHEET_ID", 0, 2_147_483_647)
    if expense_id is not None and database_id is not None and expense_id == database_id:
        raise ConfigError("Sheet tab identities must differ")
    max_rows = _number(values, "SHEETS_MAX_ROWS", 2, 10_000, "1000")
    if max_rows is None:
        raise ConfigError("Invalid SHEETS_MAX_ROWS")
    calendar = _pattern(values, "CALENDAR_ID", r"[A-Za-z0-9_.@+-]{1,512}")
    provider = _pattern(values, "MODEL_PROVIDER", r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    model = _pattern(values, "MODEL_ID", r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}")
    model_key = _secret(values, "MODEL_API_KEY")
    if bool(provider) != bool(model) or (model_key is not None and not (provider and model)):
        raise ConfigError("MODEL_PROVIDER and MODEL_ID must be supplied together before MODEL_API_KEY")
    if mode == "live":
        required = {"LINE_OWNER_ID": owner, "LINE_CHANNEL_SECRET": secret, "LINE_CHANNEL_ACCESS_TOKEN": token,
                    "GCP_PROJECT_ID": project, "SHEETS_SPREADSHEET_ID": spreadsheet,
                    "SHEETS_EXPENSE_SHEET_ID": expense_id, "SHEETS_DATABASE_SHEET_ID": database_id}
        for key, value in required.items():
            if value is None:
                raise ConfigError(f"Missing {key} for live configuration")
        if owner == "mock-owner" or secret.get_secret_value() == "mock-channel-secret" or token.get_secret_value() == "placeholder-not-a-credential":
            raise ConfigError("Public mock LINE values are invalid for live configuration")
        if environment == "cloud_run" and (not region or not service_account):
            raise ConfigError("Cloud Run configuration requires GCP_REGION and GCP_RUNTIME_SERVICE_ACCOUNT_EMAIL")
    return RuntimeConfig(mode, environment, timezone, owner, secret, token, project, region, service_account,
                         database, spreadsheet, expense_tab, database_tab, expense_id, database_id, max_rows,
                         calendar, provider, model, model_key)


def parse_env_text(text: str) -> dict[str, str]:
    """Strict one-line KEY=value format; no expansion, escapes, imports or execution."""
    if not isinstance(text, str):
        raise ConfigError("Configuration text is invalid or too large")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise ConfigError("Invalid configuration text encoding") from None
    if size > 65_536:
        raise ConfigError("Configuration text is invalid or too large")
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in ENV_KEYS:
            raise ConfigError("Unknown or malformed configuration key")
        if key in values:
            raise ConfigError("Duplicate configuration key")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError("Malformed quoted configuration value")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def load_config_file(path: Path) -> RuntimeConfig:
    """Read only the explicitly named file; never searches for/defaults to .env."""
    try:
        with Path(path).open("rb") as file:
            raw = file.read(65_537)
        if len(raw) > 65_536:
            raise ConfigError("Configuration file is too large")
        text = raw.decode("utf-8")
    except (OSError, UnicodeError, TypeError, ValueError):
        raise ConfigError("Cannot read explicit configuration file") from None
    return load_config(parse_env_text(text))
