"""Runnable expense API path, with synthetic HTTP providers only."""

from .calendar_api import CalendarApi
from .config import Settings
from .firestore_state import FirestoreState
from .line_api import LineReplyApi, ReplyDispatcher
from .mock_api import MockApiServices
from .service import Assistant
from .sheets_api import SheetTarget, SheetsApi
from .sheet_metadata import TransactionIdMetadata
from .transport import RestClient


def api_sandbox(*, services=None, clock=None, approved_mock_id_column=False, mock_id_column="E", google_tokens=None):
    services = services or MockApiServices(id_column=mock_id_column if approved_mock_id_column else None)
    settings = Settings()
    state = FirestoreState(RestClient("firestore", services.transport(),
                                      token=google_tokens.google_cloud if google_tokens else None), settings.owner_id,
                           project_id="synthetic-project", clock=clock)
    target = SheetTarget(spreadsheet_id="synthetic-sheet", writes_enabled=True, input_access_verified=True,
                         recovery="approved_id_column" if approved_mock_id_column else "manual_review",
                         transaction_id_metadata=TransactionIdMetadata(mock_id_column, schema_approved=True) if approved_mock_id_column else None,
                         row_allocation="server_append_cells" if approved_mock_id_column else "legacy_mock_update")
    sheets = SheetsApi(RestClient("sheets", services.transport(),
                                  token=google_tokens.google_workspace if google_tokens else None), target, effect_guard=state.before_effect)
    calendar = CalendarApi(RestClient("calendar", services.transport(),
                                      token=google_tokens.calendar if google_tokens else None), "synthetic-calendar", effect_guard=state.before_effect)
    assistant = Assistant(settings, sheets, calendar, sheets.catalog(), state=state, clock=clock)
    replies = ReplyDispatcher(LineReplyApi(RestClient("line", services.transport())), assistant)
    return assistant, replies, services
