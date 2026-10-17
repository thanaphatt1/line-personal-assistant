"""Explicit operator targets stay bound through signed, synthetic HTTP flows."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr
import pytest

from line_assistant.firestore_state import FirestoreState
from line_assistant.history_cloud_run import create_history_cloud_run_app, HistoryConfig
from line_assistant.pilot_http import rpc_budget
from line_assistant.runtime_config import ConfigError
from line_assistant.transport import LiveDisabled, RestClient
from test_calendar_factory import Hosted, OWNER, values
from test_calendar_mvp import response


TARGET = {
    'PILOT_PROJECT_ID': 'configured-project',
    'PILOT_SERVICE_ACCOUNT': 'assistant@configured-project.iam.gserviceaccount.com',
    'PILOT_SPREADSHEET_ID': 'configured-workbook-00001',
    'PILOT_LEGACY_SPREADSHEET_ID': '',
    'PILOT_EXPENSE_SHEET_ID': '501',
    'PILOT_DATABASE_SHEET_ID': '502',
    'PILOT_SHEETS_MAX_ROWS': '1001',
    'PILOT_SHEET_SCHEMA_VERIFIED': 'true',
}


def deployment_config(values):
    return HistoryConfig.from_mapping(values).legacy


class ConfiguredHosted(Hosted):
    def __init__(self):
        self.configured = False
        self.observed = []
        super().__init__()
        self.configured = True
        self.google.documents.clear()
        state = FirestoreState(RestClient('firestore', httpx.MockTransport(self.handle)), OWNER,
                               project_id=TARGET['PILOT_PROJECT_ID'], collection_id='assistant_pilot_states',
                               clock=lambda: self.now, max_snapshot_bytes=800000, compact_snapshots=True)
        with state.session():
            state.production_ready = True
            state.workbook_id = TARGET['PILOT_SPREADSHEET_ID']
            state.checkpoint()

    def app(self, enabled=True):
        return create_history_cloud_run_app({**values(enabled), **TARGET},
            transport=httpx.MockTransport(self.handle),
            secret_loader=lambda name: SecretStr('synthetic-' + name), clock=lambda: self.now)

    def handle(self, request):
        if not self.configured:
            return super().handle(request)
        self.observed.append((request.method, str(request.url)))
        if request.url.host == 'metadata.google.internal' and request.url.path.endswith('/email'):
            return httpx.Response(200, text=TARGET['PILOT_SERVICE_ACCOUNT'], headers={'Metadata-Flavor': 'Google'})
        if request.url.host == 'sheets.googleapis.com':
            # Reuse the wholly synthetic protocol fake, with its tab IDs mapped
            # to this test's independently configured deployment.
            from line_assistant.pilot_config import MAIN_WORKBOOK_ID
            changed = str(request.url).replace(TARGET['PILOT_SPREADSHEET_ID'], MAIN_WORKBOOK_ID)
            translated = httpx.Request(request.method, changed, headers=request.headers, content=request.content)
            result = super().handle(translated)
            body = deepcopy(result.json())
            if 'spreadsheetId' in body:
                body['spreadsheetId'] = TARGET['PILOT_SPREADSHEET_ID']
            for sheet in body.get('sheets', []):
                props = sheet.get('properties', {})
                props['sheetId'] = {101: 501, 102: 502}.get(props.get('sheetId'), props.get('sheetId'))
            return httpx.Response(result.status_code, json=body)
        return super().handle(request)


def prepare(body):
    context = json.loads(body['input'][0]['content'])
    return response('prepare_calendar_event', {
        'draft_ref': None, 'draft_version': None, 'title': 'คุยงานทดสอบ', 'date_text': '22 Oct',
        'time_text': '9:00', 'end_time_text': '10:00', 'end_date_text': None,
        'duration_text': None, 'all_day': False, 'source_refs': [context['current_source_ref']],
    })


def test_generic_target_survives_signed_review_cold_restore_confirmation_and_replay():
    api = ConfiguredHosted()
    with TestClient(api.app()) as client:
        preview, _ = api.turn(client, 'เพิ่มนัดคุยงานทดสอบ 22 Oct 9:00-10:00', prepare)
        assert preview.status == 'preview' and api.calendar.posts == 0
    with TestClient(api.app()) as client:
        saved, event = api.turn(client, 'ยืนยัน')
        assert saved.status == 'created' and api.calendar.posts == 1
    with TestClient(api.app()) as client:
        assert api.send(client, deepcopy(event)).status_code == 200
    assert api.calendar.posts == 1
    assert all('/projects/configured-project/' in path for _, path in api.observed
               if 'firestore.googleapis.com' in path)
    config = deployment_config({**values(), **TARGET})
    assert config.sheet_target().expense_sheet_id == 501
    assert config.sheet_target().database_sheet_id == 502
    assert config.approved_workbooks == {TARGET['PILOT_SPREADSHEET_ID']}


def test_unconfigured_resources_are_rejected_before_metadata_or_provider_calls():
    api = ConfiguredHosted()
    app = api.app()
    http = app.state.pilot_http
    count = len(api.observed)
    requests = [('sheets', '/v4/spreadsheets/foreign-workbook-00001'),
                ('firestore', '/v1/projects/foreign-project/databases/(default)/documents/other')]
    for service, path in requests:
        with rpc_budget(5), pytest.raises(LiveDisabled):
            http.request(service, 'GET', path)
    assert len(api.observed) == count
    http.close()


@pytest.mark.parametrize('change', [
    {'PILOT_PROJECT_ID': '../outside'},
    {'PILOT_SERVICE_ACCOUNT': 'assistant@foreign-project.iam.gserviceaccount.com'},
    {'PILOT_SPREADSHEET_ID': 'https://example.invalid/workbook'},
    {'PILOT_LEGACY_SPREADSHEET_ID': '../outside'},
    {'PILOT_EXPENSE_SHEET_ID': '-1'},
    {'PILOT_DATABASE_SHEET_ID': '501'},
    {'PILOT_SHEETS_MAX_ROWS': '1002'},
    {'PILOT_SHEET_SCHEMA_VERIFIED': 'yes'},
])
def test_invalid_targets_and_schema_fail_closed(change):
    with pytest.raises(ConfigError):
        deployment_config({**values(), **TARGET, **change})


def test_live_target_requires_explicit_nonexample_targets_and_reviewed_schema():
    with pytest.raises(ConfigError):
        deployment_config(values()).require_live_target()
    with pytest.raises(ConfigError):
        deployment_config({**values(), **TARGET, 'PILOT_SHEET_SCHEMA_VERIFIED': 'false'}).require_live_target()
    deployment_config({**values(), **TARGET}).require_live_target()
