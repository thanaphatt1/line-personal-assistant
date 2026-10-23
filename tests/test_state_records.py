"""Cold starts, old evidence replay and conditional storage migration."""
from copy import deepcopy
from datetime import timedelta
from contextlib import contextmanager
import hashlib
import json
import socket

import pytest
from fastapi.testclient import TestClient

from state_records_hosted import RecordsHosted
from calendar_reschedule_hosted import find_last, move
from test_calendar_created_reschedule import created
from test_calendar_text_confirmation_v13 import turn, mutate
from test_calendar_mvp import response
from test_history_snapshot_capacity import retained_runs, size
from line_assistant.pilot_http import rpc_budget
from line_assistant.state import Claim
from line_assistant.state import ModelRun
from line_assistant.history_context import digest
from line_assistant.firestore_state import LeaseLost
from line_assistant.model_api import BudgetBlocked
from line_assistant.transport import OutcomeUnknown,LiveDisabled
from line_assistant import state_record_codec as codec
from line_assistant.persistence import restore_state
from line_assistant.state import MemoryState
from line_assistant.runtime_config import ConfigError
from line_assistant.history_cloud_run import HistoryConfig
from test_deployment_configuration import TARGET


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Only injected synthetic services')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def test_migration_calendar_create_typo_refresh_confirmation_and_replay():
    api = RecordsHosted()
    saved = created(api)
    stored = api.stored_snapshot()
    assert stored['version'] == 2 and stored['state_records_v1']['phase'] == 'ready'
    assert not any(stored[k] for k in ('claims', 'receipts', 'replies', 'actions', 'model_runs', 'model_budgets', 'event_workbooks'))
    assert not any('/migrations/' in n for n in api.google.documents)
    review, _ = turn(api, 'เปลี่ยนเป็นวันที่ 14', find_last, move())
    assert review.status == 'preview'
    refreshed, _ = turn(api, 'ยืนยะน', response('review_saved_record_change', {}))
    assert refreshed.status == 'preview' and api.patches == 0
    api.now += timedelta(minutes=12)
    updated, confirmation = turn(api, 'ยืนยัน')
    assert updated.status == 'updated' and updated.data['id'] == saved.data['event_id']
    assert updated.data['after']['start'] == '2026-11-14T16:00:00+07:00'
    with TestClient(api.app()) as client:
        assert api.send(client, deepcopy(confirmation)).status_code == 200
    assert api.patches == 1 and api.calendar.posts == 1


def test_large_legacy_history_moves_out_of_owner_snapshot_without_changing_budget():
    api = RecordsHosted()
    api.records_enabled = False
    saved = created(api)
    mutate(api, lambda state: retained_runs(state, api.now))
    before = api.snapshot()
    assert size(before) > 800000
    api.records_enabled = True
    app = api.app()
    state = app.state.assistant.state
    with rpc_budget(96), state.session():
        assert state.actions
    app.state.pilot_http.close()
    stored = api.stored_snapshot()
    after = api.snapshot()
    assert size(stored) < size(before) // 5
    for kind in ('claims', 'receipts', 'replies', 'actions', 'model_runs', 'model_budgets', 'event_workbooks'):
        assert after[kind] == before[kind]
    assert api.calendar.posts == 1 and api.patches == 0


def test_storage_flag_off_continues_reading_the_new_format():
    api = RecordsHosted()
    saved = created(api)
    api.records_enabled = False
    result, _ = turn(api, 'เปลี่ยนเป็นวันที่ 14', find_last, move())
    assert result.status == 'preview'
    result, _ = turn(api, 'ยืนยัน')
    assert result.status == 'updated' and result.data['id'] == saved.data['event_id']


@contextmanager
def session(api):
    app=api.app()
    try:
        with rpc_budget(512),app.state.assistant.state.session():
            yield app.state.assistant.state
    finally:app.state.pilot_http.close()


@pytest.mark.parametrize('phase',['record_source_detached','record_copy_checkpoint','record_migration_completed'])
def test_migration_resumes_after_crash_without_recopy_or_business_effect(phase):
    api=RecordsHosted();api.records_enabled=False;created(api)
    mutate(api,lambda state:retained_runs(state,api.now))
    before=api.snapshot();api.records_enabled=True
    app=api.app();state=app.state.assistant.state
    class Crash(BaseException):pass
    def stop(current,cursor):
        if current==phase:raise Crash()
    state.record_phase_hook=stop
    with pytest.raises(Crash),rpc_budget(512),state.session():
        pytest.fail('Migration should stop before yielding business work')
    app.state.pilot_http.close()
    api.records_enabled=False
    with session(api):pass
    after=api.snapshot()
    for kind in ('claims','receipts','replies','actions','model_runs','model_budgets','event_workbooks'):
        assert after[kind]==before[kind]
    assert api.stored_snapshot()['state_records_v1']['phase']=='ready'
    assert not any('/migrations/' in n for n in api.google.documents)
    assert api.calendar.posts==1 and api.patches==0


@pytest.mark.parametrize('fault',['before','after'])
def test_migration_commit_acknowledgement_loss_is_verified_or_resumable(fault):
    api=RecordsHosted();api.records_enabled=False;created(api);before=api.snapshot()
    api.records_enabled=True;api.record_commit_fault=fault
    if fault=='before':
        with pytest.raises(OutcomeUnknown),session(api):pass
    else:
        with session(api):pass
    with session(api):pass
    after=api.snapshot()
    for kind in ('claims','receipts','replies','actions','model_runs','model_budgets','event_workbooks'):
        assert after[kind]==before[kind]
    assert api.calendar.posts==1 and api.patches==0


def test_cold_start_does_not_fetch_old_model_results_but_exact_lookup_recovers_them():
    api=RecordsHosted();api.records_enabled=False;created(api)
    mutate(api,lambda state:retained_runs(state,api.now));api.records_enabled=True
    with session(api):pass
    api.evidence_requests.clear()
    with session(api) as state:
        assert not list(state.model_runs)
        assert not any('/groups/models/' in path for _,path,_ in api.evidence_requests)
        old=state.model_runs['retained-model-42']
        assert old.status=='completed' and old.decision['message'].startswith('synthetic retained result')
        assert sum(method=='GET' and '/groups/models/records/' in path for method,path,_ in api.evidence_requests)==1


def test_old_delivered_event_replay_after_history_window_makes_no_model_or_calendar_request():
    from calendar_reschedule_hosted import create
    api=RecordsHosted()
    review,_=turn(api,'เพิ่มนัดกินข้าว 4 พย 16:00-22:00',create)
    saved,event=turn(api,review.data['confirm_command'])
    before=len(api.model_bodies);api.now+=timedelta(days=2)
    with TestClient(api.app()) as client:
        assert api.send(client,deepcopy(event)).status_code==200
    assert len(api.model_bodies)==before and api.calendar.posts==1 and api.patches==0
    assert api.snapshot()['receipts'][event['webhookEventId']]['result']['status']=='created'


def test_unknown_calendar_effect_survives_a_week_and_reconciles_with_no_second_patch():
    api=RecordsHosted();created(api)
    turn(api,'เปลี่ยนเป็นวันที่ 14',find_last,move());api.patch_lost_ack=True
    unknown,_=turn(api,'ยืนยัน');assert unknown.status=='uncertain' and api.patches==1
    api.now+=timedelta(days=7)
    result,_=turn(api,'ตรวจผล',response('check_saved_record_change',{}))
    assert result.data['tool_result']['status']=='saved_edit_status'
    assert api.patches==1 and api.calendar.posts==1
    assert all(a['status']=='completed' for a in api.snapshot()['actions'].values())


def test_archived_model_count_and_unresolved_reservation_preserve_usage_guards():
    api=RecordsHosted();created(api)
    body={'synthetic':'same request'}
    fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with session(api) as state:
        state.claims['old-unknown']=Claim('synthetic',api.now+timedelta(days=30))
        state.model_runs['old-unknown']=ModelRun(fingerprint,'2026-10',1,api.now+timedelta(days=30))
        state.checkpoint()
        assert state.model_record_count()==len(api.snapshot()['model_runs'])
    api.now+=timedelta(days=31)
    app=api.app();state=app.state.assistant.state
    try:
        with rpc_budget(512),state.session():
            assert state.model_record_count()==1
            assert state.model_runs['old-unknown'].status=='reserved'
            # Expired ingress claims do not grant a new paid attempt.
            state.claims['old-unknown']=Claim('synthetic',api.now+timedelta(days=30))
            with pytest.raises(BudgetBlocked):app.state.assistant.guard.reserve('old-unknown',body)
    finally:app.state.pilot_http.close()


@pytest.mark.parametrize('fault',['mac','owner','workbook','payload','version'])
def test_tampered_or_foreign_action_evidence_is_rejected_before_effect(fault):
    api=RecordsHosted();created(api)
    name=next(n for n in api.google.documents if '/groups/actions/records/' in n)
    doc=api.google.documents[name]
    if fault=='mac':doc['fields']['auth_mac']['stringValue']='0'*64
    elif fault=='owner':doc['name']=name.replace('/assistant_pilot_states/','/foreign_owner/')
    elif fault=='payload':doc['fields']['payload']['stringValue']='corrupt'
    elif fault=='version':doc.pop('updateTime')
    else:
        row=codec.decode(doc,name,'synthetic-line-channel-secret');row['workbook']='foreign-workbook'
        doc['fields']=codec.fields(name,row,'synthetic-line-channel-secret')
    with pytest.raises(OutcomeUnknown),session(api):pass
    assert api.calendar.posts==1 and api.patches==0


def test_record_precondition_conflict_never_partially_commits_owner_checkpoint():
    api=RecordsHosted();created(api)
    with pytest.raises(LeaseLost),session(api) as state:
        event=next(k for k in state.claims if k.startswith('calendar-hosted-'))
        state.claims[event].fingerprint='changed-local'
        name=state.record_store.name('events',event)
        api.google.documents[name]['updateTime']='externally-replaced'
        before=deepcopy(api.google.documents[name]['fields'])
        state.checkpoint()
    assert api.google.documents[name]['fields']==before
    assert api.calendar.posts==1 and api.patches==0


def test_workspace_switch_keeps_global_usage_and_separate_ingress_bindings():
    api=RecordsHosted();created(api)
    with session(api) as state:
        month=next(iter(state.model_budgets))
        budget=state.model_budgets[month]
        budget.calls+=1
        state.select_workbook('retained-workbook-00002',legacy_workbook=TARGET['PILOT_SPREADSHEET_ID'])
        assert state.model_budgets[month].calls==budget.calls
        state.claims['other-workspace-event']=Claim('other',api.now+timedelta(days=30))
        state.event_workbooks['other-workspace-event']='retained-workbook-00002'
        state.checkpoint()
    with session(api) as state:
        state.select_workbook(TARGET['PILOT_SPREADSHEET_ID'],legacy_workbook=TARGET['PILOT_SPREADSHEET_ID'])
        assert state.claims.get('other-workspace-event') is None
        assert state.event_workbooks['other-workspace-event']=='retained-workbook-00002'
        assert state.model_budgets[month].calls==budget.calls
        assert state.record_store.marker['last_action_keys'][TARGET['PILOT_SPREADSHEET_ID']]
    assert api.calendar.posts==1 and api.patches==0


def test_old_runtime_cannot_silently_discard_external_records():
    api=RecordsHosted();created(api)
    with pytest.raises(ValueError,match='version/owner'):
        restore_state(MemoryState(),api.stored_snapshot(),api.snapshot()['owner_id'])


def test_unbound_record_requests_cannot_use_the_live_transport_allowlist():
    api=RecordsHosted();created(api)
    app=api.app();state=app.state.assistant.state
    try:
        with rpc_budget(512),state.session():
            with pytest.raises(LiveDisabled):
                app.state.pilot_http.allowed('firestore','GET','/v1/'+state.record_store.name('events','forged'),None,None)
    finally:app.state.pilot_http.close()


@pytest.mark.parametrize('value',['yes','',1,True])
def test_records_configuration_requires_explicit_boolean(value):
    api=RecordsHosted()
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**api.configuration(),'PILOT_STATE_RECORDS_ENABLED':value})


def test_evidence_growth_beyond_old_expanded_cap_does_not_grow_the_owner_snapshot():
    api=RecordsHosted();created(api)
    initial=size(api.stored_snapshot())
    for batch in range(15):
        with session(api) as state:
            for index in range(100):
                key=f'synthetic-archived-model-{batch}-{index}'
                state.model_runs[key]=ModelRun(digest(key),'2026-10',1,api.now+timedelta(days=30),
                    'completed',{'message':'synthetic retained result '*250})
            state.checkpoint()
    final=size(api.stored_snapshot())
    assert 1500*len('synthetic retained result '*250)>8000000
    assert final<initial+2000
    api.evidence_requests.clear()
    with session(api) as state:
        assert not list(state.model_runs)
        assert state.model_record_count()>=1500
        assert not any(method=='GET' and '/groups/models/records/' in path for method,path,_ in api.evidence_requests)
    assert api.calendar.posts==1 and api.patches==0


def test_expired_cleanup_pages_keep_unknown_attempts_and_protected_calendar_children():
    from line_assistant.state import Action,Result
    api=RecordsHosted();created(api)
    with session(api) as state:
        old=api.now-timedelta(days=1)
        for i in range(7):
            state.model_runs['expired-'+str(i)]=ModelRun(digest(i),'2026-10',1,old,'completed',{'retained':'old'})
        state.model_runs['unresolved-old']=ModelRun(digest('unknown'),'2026-10',1,old)
        state.actions['protected-child']=Action('create_event',{}, {},old,old,'completed',Result('created','synthetic'))
        state.actions['unresolved-parent']=Action('calendar_batch',{'members':[{'key':'protected-child'}]},
            {'calendar_batch':True},old,old,'uncertain')
        state.claims['unresolved-confirmation']=Claim('synthetic',old,'unresolved-parent')
        state.event_workbooks['unresolved-confirmation']=state.workbook_id
        state.checkpoint()
    api.now+=timedelta(minutes=16)
    with session(api):pass
    def models():
        return [codec.decode(d,n,'synthetic-line-channel-secret')['key'] for n,d in api.google.documents.items()
                if '/groups/models/records/' in n]
    assert sum(k.startswith('expired-') for k in models())==3
    api.now+=timedelta(minutes=16)
    with session(api) as state:
        assert state.claims['unresolved-confirmation'].action_key=='unresolved-parent'
        assert state.actions['protected-child'].status=='completed'
    assert not any(k.startswith('expired-') for k in models()) and 'unresolved-old' in models()
    assert api.calendar.posts==1 and api.patches==0


def test_internal_evidence_permit_still_cannot_access_business_or_foreign_paths():
    api=RecordsHosted();created(api)
    with session(api) as state:
        with pytest.raises(LiveDisabled):state.record_store._request('GET','/v1/'+state.database+'/documents/private_business/foreign')
        with pytest.raises(LiveDisabled):state.record_store._request('POST','/v1/'+state.database+'/documents:commit',
            {'writes':[{'delete':state.name+'/foreign/record','currentDocument':{'exists':False}}]})


@pytest.mark.parametrize('backend',['sheets','firestore'])
def test_expense_receipt_buttons_and_aliases_survive_external_evidence_storage(backend):
    from state_records_hosted import RecordsProtocol
    from owner_features_hosted import OwnerHosted,button,alias
    from test_calendar_text_confirmation_v13 import old_expense
    class OwnerRecords(RecordsProtocol,OwnerHosted):pass
    api=OwnerRecords(backend);old_expense(api)
    saved,_=turn(api,'ยืนยัน')
    assert saved.status=='recorded' and saved.data['record_actions']
    calls=len(api.model_bodies)
    source,_=turn(api,button(saved,'source'),postback=True)
    assert source.data['domain']=='record_source' and len(api.model_bodies)==calls
    preview,_=turn(api,'จำชื่อ กาแฟ เป็น coffee',alias(label='coffee',item='กาแฟ'))
    assert preview.status=='preview'
    changed,_=turn(api,'ยืนยัน')
    assert changed.status=='recorded'
    assert api.stored_snapshot()['history24']['current']['format']=='local-history-state-v2'
    assert api.snapshot()['history24']['current']['owner_features_v1']['aliases']


def test_migration_operator_preview_is_read_only_and_apply_has_no_business_authority():
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('state_migration_operator',Path(__file__).resolve().parents[1]/'scripts/migrate_state_records.py')
    operator=importlib.util.module_from_spec(spec);spec.loader.exec_module(operator)
    api=RecordsHosted();api.records_enabled=False;created(api)
    before=deepcopy(api.google.documents);api.records_enabled=True
    app=api.app()
    try:
        preview=operator.run(app)
        assert preview['status']=='preview' and preview['phase']=='inline'
        assert api.google.documents==before
        def ensure_blocked(phase,cursor):
            with pytest.raises(LiveDisabled):app.state.pilot_http.allowed('model','POST','/v1/responses',{},None)
        app.state.assistant.state.record_phase_hook=ensure_blocked
        done=operator.run(app,apply=True)
        assert done['status']=='ready' and done['copied_records']>0 and done['business_requests']==0
        assert operator.run(app,apply=True)['copied_records']==0
        current=deepcopy(api.google.documents)
        verified=operator.run(app,verify=True)
        assert verified=={'status':'verified','version':2,'phase':'ready','business_requests':0}
        assert api.google.documents==current
    finally:app.state.pilot_http.close()
    assert api.calendar.posts==1 and api.patches==0


def test_separate_process_resumes_copy_confirms_once_then_replays_without_new_requests(tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    api=RecordsHosted();api.records_enabled=False;created(api)
    turn(api,'เปลี่ยนเป็นวันที่ 14',find_last,move())
    api.records_enabled=True;app=api.app()
    class Crash(BaseException):pass
    def stop(phase,cursor):
        if phase=='record_copy_checkpoint':raise Crash()
    app.state.assistant.state.record_phase_hook=stop
    try:
        with pytest.raises(Crash),rpc_budget(512),app.state.assistant.state.session():pass
    finally:app.state.pilot_http.close()
    assert api.stored_snapshot()['state_records_v1']['phase']=='copy'
    source=tmp_path/'before.json';first=tmp_path/'first.json';second=tmp_path/'second.json'
    source.write_text(json.dumps({'now':api.now.isoformat(),'counter':api.counter,'documents':api.google.documents,
        'version':api.google.version,'calendar':api.calendar.events,'posts':api.calendar.posts,'patches':api.patches}))
    root=Path(__file__).resolve().parents[1]
    env={**os.environ,'PYTHONPATH':str(root/'src')+':'+str(root/'tests'),'PYTHONDONTWRITEBYTECODE':'1'}
    command=[sys.executable,str(root/'tests/state_records_worker.py')]
    completed=subprocess.run(command+[str(source),str(first)],env=env,capture_output=True,text=True,timeout=30)
    assert completed.returncode==0,completed.stderr
    report=json.loads(completed.stdout.splitlines()[-1])
    assert report['status']=='updated' and report['patches']==1 and report['posts']==1
    replay=subprocess.run(command+[str(first),str(second)],env=env,capture_output=True,text=True,timeout=30)
    assert replay.returncode==0,replay.stderr
    report=json.loads(replay.stdout.splitlines()[-1])
    assert report=={'status':200,'posts':1,'patches':1,'model_requests':0}


def test_calendar_batch_parent_proofs_restore_from_records_through_cold_delivery_and_chunks():
    from test_calendar_batch_factory_v13 import text,proposed
    api=RecordsHosted()
    review,_=turn(api,text(4),lambda body:proposed(body,4))
    assert review.status=='preview'
    first,_=turn(api,review.data['confirm_command'])
    assert first.status=='in_progress' and api.calendar.posts==2
    final,event=turn(api,'ดำเนินชุดนัดต่อ')
    assert final.status=='created' and api.calendar.posts==4
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.calendar.posts==4
    with session(api) as state:
        state.select_workbook('retained-workbook-00002',legacy_workbook=TARGET['PILOT_SPREADSHEET_ID'])
        state.checkpoint()
    with session(api) as state:
        state.select_workbook(TARGET['PILOT_SPREADSHEET_ID'],legacy_workbook=TARGET['PILOT_SPREADSHEET_ID'])
        assert state.calendar_batches
    api.now+=timedelta(days=31)
    with session(api):pass
    with session(api):pass
    assert api.calendar.posts==4
