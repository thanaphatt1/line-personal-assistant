"""Production composition, signed LINE delivery, cold restore and RPC scope."""
from copy import deepcopy
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from line_assistant.history_cloud_run import create_history_cloud_run_app, HistoryConfig
from line_assistant.runtime_config import ConfigError
from line_assistant.transport import LiveDisabled
from line_assistant.table_storage import parse_registry
from test_calendar_factory import Hosted, values
from test_table_workflow import prepare, call
from test_table_storage import SheetServer, FirestoreServer
from test_tables import FIXTURE


class TableHosted(Hosted):
    def __init__(self):
        entries = parse_registry(FIXTURE.read_text())
        self.table_sheets = SheetServer(entries[0].table, entries[0].storage)
        self.table_firestore = FirestoreServer()
        self.registry_json = FIXTURE.read_text()
        super().__init__()

    def handle(self, request):
        if request.url.host == 'sheets.googleapis.com' and 'synthetic-people-workbook' in request.url.path:
            return self.table_sheets.handle(request)
        if request.url.host == 'firestore.googleapis.com' and (request.url.path.endswith((':beginTransaction', ':rollback')) or any(p in request.url.path for p in ('/family_income', '/shopping', '/assistant_table_operations/'))):
            return self.table_firestore.handle(request)
        if request.url.host == 'firestore.googleapis.com' and request.url.path.endswith(':commit'):
            body = json.loads(request.content)
            writes = body.get('writes', [])
            if any('/assistant_table_operations/' in r.get('update', {}).get('name', '') for r in writes):
                return self.table_firestore.handle(request)
        return super().handle(request)

    def app(self, enabled=True):
        config = {**values(enabled), 'PILOT_TABLE_REGISTRY_JSON': self.registry_json}
        return create_history_cloud_run_app(config, transport=httpx.MockTransport(self.handle), secret_loader=lambda name: SecretStr('synthetic-' + name), clock=lambda: self.now)


@pytest.mark.parametrize('table,text,fields', [('people', 'เพิ่มคุณจำลอง เบอร์ 00001', {'name': ('คุณจำลอง', 'คุณจำลอง'), 'phone': ('00001', '00001')}),
    ('family_income', 'วันนี้รายได้ 1000', {'amount': ('1000', '1000')})])
def test_signed_factory_schema_proposal_cold_confirm_and_replay(table, text, fields):
    api = TableHosted()
    app = api.app()
    assert not api.table_sheets.calls and not api.table_firestore.calls
    with TestClient(app) as client:
        review, _ = api.turn(client, text, call('describe_table', table_id=table), prepare(table, **fields))
        assert review.status == 'preview'
    with TestClient(api.app()) as client:
        result, event = api.turn(client, 'ยืนยัน')
        assert result.status == 'recorded'
    with TestClient(api.app()) as client:
        assert api.send(client, deepcopy(event)).status_code == 200
    assert len(api.table_sheets.writes) + len(api.table_firestore.commits) == 1
    assert api.calendar.posts == api.google.sheet_writes == 0


def test_undelivered_review_and_changed_registry_cannot_authorize_write():
    api = TableHosted(); api.reject_reply = True
    with TestClient(api.app()) as client:
        review, _ = api.turn(client, 'เพิ่ม A', prepare(name=('A', 'A')))
        assert review.status == 'preview'
    api.reject_reply = False
    with TestClient(api.app()) as client:
        result, _ = api.turn(client, 'ยืนยัน')
        assert result.status != 'recorded'
    assert not api.table_sheets.writes


def test_factory_provider_allowlist_accepts_only_one_exact_adapter_request():
    api = TableHosted(); app = api.app(); http = app.state.pilot_http
    with pytest.raises(LiveDisabled):
        http.allowed('sheets', 'GET', '/v4/spreadsheets/synthetic-people-workbook', None, None)
    with pytest.raises(LiveDisabled):
        http.allowed('firestore', 'POST', '/v1/projects/synthetic-project/databases/(default)/documents:commit', {'writes': []}, None)


def test_config_rejects_unbound_or_internal_tables_and_requires_raw_history():
    raw = json.loads(FIXTURE.read_text())
    with pytest.raises(ConfigError): HistoryConfig.from_mapping({'PILOT_TABLE_REGISTRY_JSON': FIXTURE.read_text()})
    for change in [lambda r: r[0]['storage'].update(spreadsheet_id=values()['PILOT_SPREADSHEET_ID'], sheet_id=101),
                   lambda r: r[1]['storage'].update(collection_path='assistant_expense_owners/x/ledgers')]:
        modified = deepcopy(raw); change(modified)
        with pytest.raises(ConfigError): HistoryConfig.from_mapping({**values(), 'PILOT_TABLE_REGISTRY_JSON': json.dumps(modified)})
    with pytest.raises(ConfigError):
        create_history_cloud_run_app({**values(), 'PILOT_TABLE_REGISTRY_JSON': FIXTURE.read_text()}, raw_history_enabled=False)
