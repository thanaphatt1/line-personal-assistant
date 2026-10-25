"""Backend parity through signed owner events and recreated production factories."""
from copy import deepcopy
from datetime import timedelta
import json
import socket

import pytest
from fastapi.testclient import TestClient

from line_assistant.history_cloud_run import HistoryConfig, create_history_cloud_run_app
from line_assistant.pilot_http import rpc_budget
from line_assistant.production_history_state import production_binding
from line_assistant.runtime_config import ConfigError
from line_assistant.transport import LiveDisabled
from expense_storage_hosted import StorageHosted
from test_previous_expense_prices import turn, prepare, accepted, offer
from test_calendar_all_day_v15 import proposal, event
from test_calendar_factory import prepare as calendar_prepare
from test_calendar_mvp import response
from test_read_queries import plan as query
from test_saved_record_workflow import prepare as edit_prepare
from test_historical_categories import preview as historical_preview, review as historical_review, command as historical_command


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):raise AssertionError('Storage parity cannot access an external network')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


@pytest.fixture(params=['sheets', 'firestore'])
def api(request):
    return StorageHosted(request.param)


def test_missing_price_selection_confirmation_and_duplicate_delivery(api):
    original = deepcopy(api.google.cells)
    result, _ = offer(api)
    assert result.status == 'clarification' and '169 บาท' in result.message
    assert api.expense_writes == 0
    result, _ = accepted(api)
    assert result.status == 'preview' and api.expense_writes == 0
    result, confirmation = turn(api, 'ยืนยัน')
    assert result.status == 'recorded' and api.expense_writes == 1, result
    count = len(api.model_bodies)
    with TestClient(api.app()) as web:assert api.send(web, deepcopy(confirmation)).status_code == 200
    assert api.expense_writes == 1 and len(api.model_bodies) == count
    assert next(iter(api.drafts().values()))['status'] == 'recorded'
    assert any('/assistant_raw_messages/' in k for k in api.google.documents)
    if api.backend == 'firestore':assert api.google.cells == original


def test_query_category_date_and_listing_are_complete_and_read_only(api):
    result, _ = turn(api, 'เดือนตุลาคม 2026 จ่ายอะไรไปบ้าง', query(start='2026-10-01', end='2026-10-31', listing=True))
    assert result.data['tool_result']['complete'] and result.data['tool_result']['expense'] == '169.00', result
    assert result.data['tool_result']['count'] == 1 and api.expense_writes == 0


def test_saved_expense_edit_uses_stable_id_cold_review_and_confirmation(api):
    result, _ = turn(api, 'Spotify 35 บาท', prepare(amounts={'Spotify': '35'}))
    assert result.status == 'preview', result
    assert turn(api, 'ยืนยัน')[0].status == 'recorded'
    def find_last(body):
        context = json.loads(body['input'][0]['content'])
        return response('find_saved_records', {'domain': 'expense', 'scope': 'last', 'start_date': None,
            'end_date': None, 'period_text': None, 'name': None, 'source_refs': [context['current_source_ref']]})
    result, _ = turn(api, 'แก้รายการที่บันทึกล่าสุดเป็น 45 บาท', find_last, edit_prepare(amount='45', amount_text='45'))
    assert result.status == 'preview', result
    result, event_ = turn(api, 'ยืนยัน')
    assert result.status == 'updated' and api.expense_writes == 2, result
    with TestClient(api.app()) as web:assert api.send(web, deepcopy(event_)).status_code == 200
    assert api.expense_writes == 2


def test_historical_mapping_is_confirmed_separately_and_preserves_expense_records(api):
    api.saved(9, '2026-09-04', 'Unknown Metro Ticket', 20)
    if api.backend == 'firestore':api.seed_business_fixture()
    before = deepcopy(api.google.cells)
    result, _ = historical_preview(api)
    assert result.status == 'preview' and result.data['tool_result']['total_names'] == 1, result
    result, _ = turn(api, 'ถูกหมด', historical_review(selection='all_clear'))
    assert result.status == 'preview' and api.expense_writes == 0, result
    result, _ = turn(api, historical_command(api))
    assert result.status == 'mapping_saved' and api.expense_writes == 1, result
    if api.backend == 'firestore':assert api.google.cells == before
    else:assert all(api.google.cells[k] == v for k, v in before.items() if k[0] == 'All Expenses')


@pytest.mark.parametrize('all_day', [False, True])
def test_calendar_creation_remains_available_with_both_expense_backends(api, all_day):
    answer = proposal([event(day='22 ตุลาคม 2026')]) if all_day else calendar_prepare
    text = 'เพิ่มนัดนิทรรศการทดสอบ 22 ตุลาคม 2026 ทั้งวัน' if all_day else 'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที'
    result, _ = turn(api, text, answer)
    assert result.status == 'preview' and api.expense_writes == 0, result
    result, confirmation = turn(api, 'ยืนยัน')
    assert result.status == 'created' and api.calendar.posts == 1 and api.expense_writes == 0, result
    with TestClient(api.app()) as web:assert api.send(web, deepcopy(confirmation)).status_code == 200
    assert api.calendar.posts == 1


def test_cancel_never_saves_any_backend(api):
    assert offer(api)[0].status == 'clarification'
    def cancel(body):
        c=json.loads(body['input'][0]['content']);d=c['available_drafts'][0]
        return response('control_expense_draft', {'operation':'cancel', 'draft_ref':d['draft_ref'], 'draft_version':d['version']})
    result, _ = turn(api, 'ยกเลิก', cancel)
    assert result.status in {'cancelled', 'clarification'} and api.expense_writes == 0 and api.calendar.posts == 0


def test_invalid_signature_and_other_owner_have_zero_provider_reads(api):
    before = len(api.google.calls), len(api.business.requests), len(api.model_bodies)
    with TestClient(api.app()) as web:
        assert web.post('/webhooks/line', content=b'{"events":[]}', headers={'x-line-signature': 'invalid'}).status_code == 401
        event_ = api.event('Spotify 1 บาท'); event_['source']['userId'] = 'U' + 'b' * 32
        assert api.send(web, event_).status_code == 200
    assert (len(api.google.calls), len(api.business.requests), len(api.model_bodies)) == before


def test_firestore_startup_and_transport_reject_arbitrary_commits_and_sheets_calls():
    api = StorageHosted(); count = len(api.business.requests), len(api.google.calls)
    app = api.app(); assistant = app.state.assistant; http = app.state.pilot_http
    assert (len(api.business.requests), len(api.google.calls)) == count
    assert production_binding(assistant.state, assistant.sheets, assistant.port)
    with rpc_budget(96), assistant.state.session():
        assistant.state.select_workbook(assistant.sheets.scope_id, legacy_workbook=api.configuration()['PILOT_LEGACY_SPREADSHEET_ID'])
        with pytest.raises(LiveDisabled):http.request('firestore', 'POST', '/v1/' + assistant.state.database + '/documents:commit', body={'writes': []})
        with pytest.raises(LiveDisabled):http.request('sheets', 'GET', '/v4/spreadsheets/foreign')
    http.close()


def test_backend_change_preserves_old_draft_history_and_rejects_old_confirmation():
    api = StorageHosted('sheets'); first, old_event = turn(api, 'Spotify 35 บาท', prepare(amounts={'Spotify': '35'}))
    assert first.status == 'preview'
    before = deepcopy(api.snapshot()); original_raw = {k: deepcopy(v) for k, v in api.google.documents.items() if '/assistant_raw_messages/' in k}
    api.backend = 'firestore'; api.seed_business_fixture()
    result, _ = turn(api, first.data.get('confirm_command', 'ยืนยัน'))
    assert result.status in {'stale', 'clarification', 'no_pending', 'answer'} and api.expense_writes == 0, result
    after = api.snapshot(); old_scope = api.configuration()['PILOT_LEGACY_SPREADSHEET_ID']
    assert after['history24']['workspaces'][old_scope]['drafts'] == before['history24']['current']['drafts']
    assert all(api.google.documents[k] == v for k, v in original_raw.items())
    assert after['model_budgets'] == before['model_budgets']


@pytest.mark.parametrize('changes', [
    {'PILOT_EXPENSE_BACKEND': 'other'}, {'PILOT_EXPENSE_DATASET_ID': '../foreign'},
    {'PILOT_EXPENSE_MAX_RECORDS': '0'}, {'PILOT_HISTORY_ENABLED': 'false'},
    {'PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY': 'reviewed_single_writer'},
])
def test_invalid_firestore_configuration_is_rejected_before_providers(changes):
    api = StorageHosted()
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**api.configuration(), **changes})


def test_state_document_change_during_business_transaction_aborts_expense_commit():
    api=StorageHosted();result,_=turn(api,'Spotify 35 บาท',prepare(amounts={'Spotify':'35'}))
    assert result.status=='preview'
    def changed(server):
        state=next(v for k,v in server.documents.items() if '/assistant_pilot_states/' in k)
        state['updateTime']=server.stamp()
    api.business.before_commit=changed
    result,_=turn(api,'ยืนยัน')
    assert result.status=='conflict' and api.business.commits==0,result
    assert not any('/operations/' in k for k in api.google.documents)


@pytest.mark.parametrize('feature',['expense_edit','mapping','price'])
def test_transport_action_gate_independently_rejects_disabled_features(feature):
    from line_assistant.state import Action
    from line_assistant.history_context import digest
    api=StorageHosted()
    if feature=='expense_edit':api.edit_flags=False
    elif feature=='mapping':api.mapping=False
    else:api.prices=False
    app=api.app();h=app.state.assistant;s=h.state
    with rpc_budget(96),s.session():
        s.select_workbook(h.sheets.scope_id,legacy_workbook=api.configuration()['PILOT_LEGACY_SPREADSHEET_ID'])
        key=s.owner_id+':gate-test';payload={'entries':[]}
        kind={'expense_edit':'edit_saved_expense','mapping':'save_proposed_mapping','price':'add_transactions'}[feature]
        metadata={'owner':s.owner_id,'workbook':h.sheets.scope_id,'payload_hash':digest(payload)}
        if feature=='price':metadata['previous_price_bases']=[{'synthetic':'proof'}]
        s.actions[key]=Action(kind,payload,metadata,s.clock()+timedelta(hours=1),s.clock()+timedelta(hours=1),status='executing')
        with pytest.raises(LiveDisabled):h.sheets._action(kind,payload,key)
    app.state.pilot_http.close()
    assert api.business.commits==0


def test_firestore_raw_history_cannot_be_disabled_as_a_factory_shortcut():
    api=StorageHosted()
    with pytest.raises(ConfigError):create_history_cloud_run_app(api.configuration(),raw_history_enabled=False)


def test_saved_calendar_reschedule_preserves_etag_guards_with_both_backends(api):
    from test_saved_record_workflow import preview
    original=deepcopy(api.saved_calendar.event)
    result,_=preview(api,'calendar');assert result.status=='preview',result
    result,event_=turn(api,'ยืนยัน')
    assert result.status=='updated' and api.saved_calendar.patches==1 and api.expense_writes==0,result
    assert api.saved_calendar.event['reminders']==original['reminders']
    with TestClient(api.app()) as web:assert api.send(web,deepcopy(event_)).status_code==200
    assert api.saved_calendar.patches==1


@pytest.mark.parametrize('committed',[True,False])
def test_firestore_cold_unknown_outcome_reconciles_with_price_feature_off(committed):
    api=StorageHosted();offer(api);accepted(api)
    api.business.lost_ack=committed;api.business.before_ack=not committed
    event_=api.event('ยืนยัน')
    with TestClient(api.app()) as web:assert api.send(web,event_).status_code==503
    assert api.business.commits==int(committed)
    assert any(a['status']=='uncertain' for a in api.snapshot()['actions'].values())
    api.business.lost_ack=api.business.before_ack=False;api.prices=False
    with TestClient(api.app()) as web:assert api.send(web,deepcopy(event_)).status_code==200
    actions=list(api.snapshot()['actions'].values())
    assert all(a['status']=='completed' for a in actions) if committed else any(a['status']=='uncertain' for a in actions)
    assert api.business.commits==int(committed)


def test_firestore_source_change_invalidates_accepted_price_without_write():
    from line_assistant.firestore_expenses import unfields
    api=StorageHosted();offer(api);accepted(api)
    name=api.business_target.parent+'/expenses/synthetic_price_3'
    value=unfields(api.google.documents[name]);value['amount_minor']=19900;api.business.put(name,value)
    result,_=turn(api,'ยืนยัน')
    assert result.status=='stale' and api.business.commits==0,result


def test_firestore_profile_works_without_a_spreadsheet_id_or_sheet_schema_flag():
    api=StorageHosted();configuration=api.configuration
    api.configuration=lambda enabled=True:{**configuration(enabled),'PILOT_SPREADSHEET_ID':'','PILOT_SHEET_SCHEMA_VERIFIED':'false'}
    result,_=offer(api)
    assert result.status=='clarification' and api.google.sheet_writes==0,result
    assert not any('sheets.googleapis.com' in url for method,url in api.observed)


@pytest.mark.parametrize('status',[403,500])
def test_transaction_read_rejection_is_setup_failure_before_any_business_write(status):
    import httpx
    api=StorageHosted();assert turn(api,'Spotify 35 บาท',prepare(amounts={'Spotify':'35'}))[0].status=='preview'
    original=api.business.handle
    def rejected(request):
        body=json.loads(request.content) if request.content else {}
        if request.url.path.endswith(':batchGet') and any('/assistant_pilot_states/' in name for name in body['documents']):
            return httpx.Response(status,json={'error':{'status':'PERMISSION_DENIED' if status==403 else 'INTERNAL'}})
        return original(request)
    api.business.handle=rejected
    result,_=turn(api,'ยืนยัน')
    assert result.status=='setup_required' and api.business.commits==0,result
