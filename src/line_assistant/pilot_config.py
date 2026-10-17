"""Explicit deployment configuration with synthetic defaults. No dotenv/ADC discovery."""

from dataclasses import dataclass
from datetime import datetime
import re

from .model_api import MODEL_ID, RESERVATION_MICRO_USD
from .canary import CANARY_KEYS, CanarySpec
from .reply_check import REPLY_CHECK_KEYS, ReplyCheckSpec
from .readonly_preflight import PROJECT_ID, SERVICE_ACCOUNT, TEST_WORKBOOK_ID
from .runtime_config import ConfigError
from .sheets_api import SheetTarget
from .sheet_metadata import TransactionIdMetadata

MAIN_WORKBOOK_ID = "synthetic-main-workbook"


PILOT_KEYS = frozenset({"PILOT_MODE", "PILOT_LAUNCH_AUTHORIZED", "PILOT_CONVERSATION_ENABLED", "PILOT_DOMAIN_TOOL_LOOP_ENABLED", "PILOT_STATE_CODEC_ENABLED", "PILOT_LINE_OWNER_ID", "PILOT_MODEL_MONTHLY_MICRO_USD",
                        "PILOT_MODEL_MAX_CALLS_DAY", "PILOT_MODEL_MAX_CALLS_MONTH", "PILOT_MODEL_ID",
                        "PILOT_PROJECT_ID", "PILOT_REGION", "PILOT_SERVICE_ACCOUNT", "PILOT_SPREADSHEET_ID",
                        "PILOT_LINE_AUTH_CHECK_ID", "PILOT_LINE_AUTH_CHECK_EXPIRES_AT", "PILOT_LINE_AUTH_CHECK_EXPECTED_BOT",
                        "PILOT_LEGACY_SPREADSHEET_ID", "PILOT_EXPENSE_SHEET_ID", "PILOT_DATABASE_SHEET_ID", "PILOT_SHEETS_MAX_ROWS", "PILOT_SHEET_SCHEMA_VERIFIED"}) | CANARY_KEYS | REPLY_CHECK_KEYS
SECRET_PATHS = {
    "line-channel-secret": "/var/run/line-assistant-secrets/line-channel-secret/value",
    "line-channel-access-token": "/var/run/line-assistant-secrets/line-channel-access-token/value",
    "openai-api-key": "/var/run/line-assistant-secrets/openai-api-key/value",
}


@dataclass(frozen=True, repr=False)
class PilotConfig:
    launch_authorized: bool = False
    owner_id: str = ""
    monthly_micro_usd: int = 0
    max_calls_day: int = 20
    max_calls_month: int = 300
    canary: CanarySpec | None = None
    mode: str = "canary"
    spreadsheet_id: str = TEST_WORKBOOK_ID
    auth_check_id: str = ""
    auth_check_expires_at: datetime | None = None
    auth_check_expected_bot: str = ""
    reply_check: ReplyCheckSpec | None = None
    conversation_enabled: bool = False
    state_codec_enabled: bool = False
    domain_tool_loop_enabled: bool = False

    project_id: str = PROJECT_ID
    region: str = "asia-southeast3"
    service_account: str = SERVICE_ACCOUNT
    legacy_spreadsheet_id: str = TEST_WORKBOOK_ID
    expense_sheet_id: int = 101
    database_sheet_id: int = 102
    sheet_max_rows: int = 1001
    sheet_schema_verified: bool = False

    @property
    def approved_workbooks(self):
        return frozenset(book for book in (self.spreadsheet_id, self.legacy_spreadsheet_id) if book)

    def require_live_target(self):
        if (self.project_id == PROJECT_ID or self.spreadsheet_id in {MAIN_WORKBOOK_ID, TEST_WORKBOOK_ID}
                or not self.sheet_schema_verified):
            raise ConfigError("Live launch requires explicit deployment targets and reviewed Sheet schema")

    def sheet_target(self):
        return SheetTarget(spreadsheet_id=self.spreadsheet_id,
                           expense_sheet_id=self.expense_sheet_id,
                           database_sheet_id=self.database_sheet_id,
                           max_rows=self.sheet_max_rows,
                           writes_enabled=True, input_access_verified=True,
                           recovery="approved_id_column",
                           transaction_id_metadata=TransactionIdMetadata("E", schema_approved=True))

    def __repr__(self):
        return "PilotConfig(values=<redacted>)"

    @classmethod
    def from_mapping(cls, values):
        if any(key.startswith("PILOT_") and key not in PILOT_KEYS for key in values if isinstance(key, str)):
            raise ConfigError("Unknown pilot key")
        defaults = {"PILOT_MODE": "canary", "PILOT_LAUNCH_AUTHORIZED": "false", "PILOT_CONVERSATION_ENABLED": "false", "PILOT_DOMAIN_TOOL_LOOP_ENABLED": "false", "PILOT_STATE_CODEC_ENABLED": "false", "PILOT_LINE_OWNER_ID": "", "PILOT_MODEL_MONTHLY_MICRO_USD": "0",
                    "PILOT_MODEL_MAX_CALLS_DAY": "20", "PILOT_MODEL_MAX_CALLS_MONTH": "300", "PILOT_MODEL_ID": MODEL_ID,
                    "PILOT_PROJECT_ID": PROJECT_ID, "PILOT_REGION": "asia-southeast3", "PILOT_SERVICE_ACCOUNT": SERVICE_ACCOUNT,
                    "PILOT_SPREADSHEET_ID": TEST_WORKBOOK_ID,
                    "PILOT_LINE_AUTH_CHECK_ID": "", "PILOT_LINE_AUTH_CHECK_EXPIRES_AT": "", "PILOT_LINE_AUTH_CHECK_EXPECTED_BOT": ""}
        defaults.update({key: "" for key in CANARY_KEYS})
        defaults.update({key: "" for key in REPLY_CHECK_KEYS})
        defaults["PILOT_REPLY_CHECK_AUTHORIZED"] = "false"
        production = values.get("PILOT_MODE") == "production"
        defaults.update({"PILOT_LEGACY_SPREADSHEET_ID": TEST_WORKBOOK_ID if values.get("PILOT_PROJECT_ID", PROJECT_ID) == PROJECT_ID else "",
                         "PILOT_EXPENSE_SHEET_ID": "101" if production else "201",
                         "PILOT_DATABASE_SHEET_ID": "102" if production else "202",
                         "PILOT_SHEETS_MAX_ROWS": "1001" if production else "12", "PILOT_SHEET_SCHEMA_VERIFIED": "false"})
        v = {key: values.get(key, value) for key, value in defaults.items()}
        if any(not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value) for value in v.values()):
            raise ConfigError("Invalid pilot values")
        if v["PILOT_MODEL_ID"] != MODEL_ID:
            raise ConfigError("Unsupported model")
        if (not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", v["PILOT_PROJECT_ID"])
                or not re.fullmatch(r"[a-z]+-[a-z]+[0-9]", v["PILOT_REGION"])
                or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@" + re.escape(v["PILOT_PROJECT_ID"]) + r"\.iam\.gserviceaccount\.com", v["PILOT_SERVICE_ACCOUNT"])):
            raise ConfigError("Invalid deployment project, region or attached service account")
        for key in ("PILOT_SPREADSHEET_ID", "PILOT_LEGACY_SPREADSHEET_ID"):
            if (key == "PILOT_SPREADSHEET_ID" or v[key]) and not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", v[key]):
                raise ConfigError("Invalid deployment workbook")
        for key in ("PILOT_EXPENSE_SHEET_ID", "PILOT_DATABASE_SHEET_ID", "PILOT_SHEETS_MAX_ROWS"):
            if not re.fullmatch(r"[0-9]{1,10}", v[key]):
                raise ConfigError("Invalid Sheet numeric configuration")
        if (int(v["PILOT_EXPENSE_SHEET_ID"]) == int(v["PILOT_DATABASE_SHEET_ID"])
                or max(int(v["PILOT_EXPENSE_SHEET_ID"]), int(v["PILOT_DATABASE_SHEET_ID"])) > 2147483647
                or not 2 <= int(v["PILOT_SHEETS_MAX_ROWS"]) <= 1001
                or v["PILOT_SHEET_SCHEMA_VERIFIED"] not in {"true", "false"}):
            raise ConfigError("Invalid reviewed Sheet schema")
        if v["PILOT_LAUNCH_AUTHORIZED"] not in {"true", "false"}:
            raise ConfigError("Invalid pilot launch flag")
        if v["PILOT_MODE"] not in {"canary", "production"}:
            raise ConfigError("Explicit canary or production mode required")
        if v['PILOT_CONVERSATION_ENABLED'] not in {'true', 'false'} or (v['PILOT_CONVERSATION_ENABLED'] == 'true' and v['PILOT_MODE'] != 'production'):
            raise ConfigError('Conversation opt-in requires production mode and explicit boolean')
        loop_flag = v['PILOT_DOMAIN_TOOL_LOOP_ENABLED']
        if loop_flag not in {'true', 'false'} or (loop_flag == 'true' and v['PILOT_CONVERSATION_ENABLED'] != 'true'):
            raise ConfigError('Domain loop requires explicit conversation opt-in')
        codec_flag = v['PILOT_STATE_CODEC_ENABLED']
        if codec_flag not in {'true','false'} or (codec_flag == 'true' and v['PILOT_MODE'] != 'production'):
            raise ConfigError('State codec opt-in requires production mode and explicit boolean')
        target = v["PILOT_SPREADSHEET_ID"]
        if (v["PILOT_MODE"] == "production" and target == TEST_WORKBOOK_ID
                or v["PILOT_MODE"] == "canary" and target == MAIN_WORKBOOK_ID):
            raise ConfigError("Mode requires its explicit reviewed workbook")
        for key in ("PILOT_MODEL_MONTHLY_MICRO_USD", "PILOT_MODEL_MAX_CALLS_DAY", "PILOT_MODEL_MAX_CALLS_MONTH"):
            if not re.fullmatch(r"[0-9]{1,10}", v[key]):
                raise ConfigError("Invalid pilot budget/count")
        limit, daily, monthly = (int(v[key]) for key in ("PILOT_MODEL_MONTHLY_MICRO_USD", "PILOT_MODEL_MAX_CALLS_DAY", "PILOT_MODEL_MAX_CALLS_MONTH"))
        if not 1 <= daily <= (100 if v["PILOT_MODE"] == "production" else 20) or not 1 <= monthly <= 300:
            raise ConfigError("Pilot count limits exceed prepared bounds")
        enabled, owner = v["PILOT_LAUNCH_AUTHORIZED"] == "true", v["PILOT_LINE_OWNER_ID"]
        if owner and not re.fullmatch(r"U[a-f0-9]{32}", owner):
            raise ConfigError("Explicit one-to-one LINE owner ID required")
        if enabled and (not owner or limit < RESERVATION_MICRO_USD):
            raise ConfigError("Launch requires configured owner and separately approved model budget")
        try:
            canary = CanarySpec.from_mapping(v)
        except ValueError:
            raise ConfigError("Canary requires a complete explicit nonce/date/UTC window of at most ten minutes") from None
        if v["PILOT_MODE"] == "production" and (canary is not None or limit > 500000):
            raise ConfigError("Production requires no canary config and at most $0.50 model allocation")
        if enabled and canary and (limit != RESERVATION_MICRO_USD or daily != 1 or monthly != 1):
            raise ConfigError("Canary launch requires exactly one model reservation and 1/day, 1/month")
        check_id, check_end, expected_bot = (v[key] for key in ("PILOT_LINE_AUTH_CHECK_ID", "PILOT_LINE_AUTH_CHECK_EXPIRES_AT", "PILOT_LINE_AUTH_CHECK_EXPECTED_BOT"))
        expires = None
        if check_id or check_end or expected_bot:
            if (enabled or limit != 0 or not re.fullmatch(r"[a-f0-9]{32}", check_id)
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", check_end)
                    or (expected_bot and not re.fullmatch(r"U[a-f0-9]{32}", expected_bot))):
                raise ConfigError("Explicit disabled zero-budget bounded LINE auth check required")
            try:
                expires = datetime.fromisoformat(check_end.replace("Z", "+00:00"))
            except ValueError:
                raise ConfigError("Invalid auth-check deadline") from None
        try:
            reply_check = ReplyCheckSpec.from_mapping(v)
        except ValueError:
            raise ConfigError("Complete explicit reply-check authorization/ID/UTC window of at most five minutes required") from None
        if reply_check and (enabled or limit != 0 or v["PILOT_MODE"] != "canary" or check_id
                            or (reply_check.authorized and not owner)):
            raise ConfigError("Reply check requires disabled expense launch, zero model allocation and configured owner")
        return cls(enabled, owner, limit, daily, monthly, canary, v["PILOT_MODE"], target, check_id, expires, expected_bot, reply_check,
                   v['PILOT_CONVERSATION_ENABLED'] == 'true', codec_flag == 'true', loop_flag == 'true',
                   project_id=v["PILOT_PROJECT_ID"], region=v["PILOT_REGION"], service_account=v["PILOT_SERVICE_ACCOUNT"],
                   legacy_spreadsheet_id=v["PILOT_LEGACY_SPREADSHEET_ID"], expense_sheet_id=int(v["PILOT_EXPENSE_SHEET_ID"]),
                   database_sheet_id=int(v["PILOT_DATABASE_SHEET_ID"]), sheet_max_rows=int(v["PILOT_SHEETS_MAX_ROWS"]),
                   sheet_schema_verified=v["PILOT_SHEET_SCHEMA_VERIFIED"] == "true")
