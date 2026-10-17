"""Fixed synthetic-target metadata preflight; no values, documents or writes.

Explicit clients/providers only. Both HTTP and auth gates remain mock-only.
No startup, ADC loading, login, IAM/API changes or subprocess execution here.
"""

from dataclasses import dataclass
from urllib.parse import quote

from .auth_ports import TokenProviders
from .google_auth import ImpersonatedTokenProvider
from .runtime_config import RuntimeConfig
from .transport import LiveDisabled, NotReady, RestClient


TEST_WORKBOOK_ID = "synthetic-test-workbook"
PROJECT_ID = "example-project"
SERVICE_ACCOUNT = "assistant@example-project.iam.gserviceaccount.com"
SHEET_FIELDS = "spreadsheetId,properties(timeZone),sheets(properties(sheetId,title,gridProperties))"


@dataclass(frozen=True, repr=False)
class PreflightTarget:
    spreadsheet_id: str
    project_id: str
    service_account_email: str
    database_id: str = "(default)"
    region: str = "asia-southeast3"

    def __post_init__(self):
        # This milestone is bound to the approved synthetic workbook/project.
        if (self.spreadsheet_id != TEST_WORKBOOK_ID or self.project_id != PROJECT_ID
                or self.service_account_email != SERVICE_ACCOUNT or self.database_id != "(default)"
                or self.region != "asia-southeast3"):
            raise NotReady("Preflight must use the selected synthetic target and identity")

    def __repr__(self):
        return "PreflightTarget(values=<redacted>, writes_enabled=False)"

    @classmethod
    def from_config(cls, config: RuntimeConfig):
        return cls(config.sheet_spreadsheet_id, config.project_id, config.runtime_service_account_email,
                   config.firestore_database_id, config.region)


def local_preflight_tokens(target: PreflightTarget, caller_token, *, transport=None, clock=None):
    """Explicit caller callback only. Does not load or discover local ADC."""
    if type(target) is not PreflightTarget:
        raise NotReady("Explicit selected preflight target required")
    return TokenProviders(
        google_workspace=ImpersonatedTokenProvider("sheets_readonly", target.service_account_email,
                                                  caller_token, transport=transport, clock=clock),
        google_cloud=ImpersonatedTokenProvider("firestore", target.service_account_email,
                                              caller_token, transport=transport, clock=clock),
    )


def local_adc_login_handoff(target: PreflightTarget):
    """Return a reviewable future user command; never run it or set global config.

    Login creates local credentials and needs separate action-time authorization.
    The returned ADC token must later use an SDK-scoped credential bridge, not be
    passed back as the source caller token for another impersonation request.
    """
    if type(target) is not PreflightTarget:
        raise NotReady("Explicit selected preflight target required")
    return ("gcloud", "auth", "application-default", "login",
            "--impersonate-service-account=" + target.service_account_email)


class ReadonlyPreflight:
    def __init__(self, target: PreflightTarget, sheets: RestClient, firestore: RestClient):
        if type(target) is not PreflightTarget:
            raise NotReady("Explicit selected preflight target required")
        if type(sheets) is not RestClient or type(firestore) is not RestClient:
            raise NotReady("Explicit Google preflight clients required")
        if sheets.service != "sheets" or firestore.service != "firestore":
            raise NotReady("Preflight clients must match their Google services")
        if not sheets.mocked or not firestore.mocked:
            raise LiveDisabled("Live preflight/credential use is not enabled")
        self.target, self.sheets, self.firestore = target, sheets, firestore

    def run(self):
        target = self.target
        sheet = self.sheets.request("GET", "/v4/spreadsheets/" + quote(target.spreadsheet_id, safe=""),
                                    params={"fields": SHEET_FIELDS, "includeGridData": "false"})
        tabs = sheet.get("sheets")
        if sheet.get("spreadsheetId") != target.spreadsheet_id or not isinstance(tabs, list) or not 1 <= len(tabs) <= 100:
            raise NotReady("Preflight Sheet metadata does not match the configured target")
        # Check safe metadata shape without exposing returned titles/values in reports.
        ids = set()
        for tab in tabs:
            props = tab.get("properties", {}) if isinstance(tab, dict) else {}
            if not isinstance(props, dict):
                raise NotReady("Preflight Sheet tab metadata is invalid")
            identifier = props.get("sheetId")
            if type(identifier) is not int or identifier < 0 or identifier in ids or not isinstance(props.get("title"), str):
                raise NotReady("Preflight Sheet tab metadata is invalid")
            ids.add(identifier)
        name = "projects/" + target.project_id + "/databases/" + target.database_id
        database = self.firestore.request("GET", "/v1/" + quote(name, safe="/"))
        if (database.get("name") != name or database.get("type") != "FIRESTORE_NATIVE"
                or database.get("locationId") != target.region):
            raise NotReady("Preflight Firestore metadata does not match configuration")
        return {"sheet_metadata_readable": True, "sheet_tab_count": len(tabs),
                "firestore_metadata_readable": True, "firestore_location_matches": True,
                "schema_verified": False, "firestore_data_iam_verified": False,
                "writes_enabled": False, "live_requests_enabled": False}
