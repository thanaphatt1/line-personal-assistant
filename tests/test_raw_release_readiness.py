"""Offline release gates for legacy migration and journal-aware rollback."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest,httpx
from fastapi.testclient import TestClient
from line_assistant.history_cloud_run import create_history_cloud_run_app
from line_assistant.history_persistence import history_snapshot
from line_assistant.persistence import result_from
from test_production_history import Hosted
from test_conversation_transport import config
from test_cloud_run import fake_secrets
from test_history_first import response,data
from test_raw_history import Harness,context

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Readiness must not use live providers')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def app(api,*,enabled=True):
    return create_history_cloud_run_app({**config(),'PILOT_HISTORY_STATE_ENABLED':'true','PILOT_HISTORY_ENABLED':'true' if enabled else 'false'},
        transport=httpx.MockTransport(api.handle),secret_loader=fake_secrets,clock=lambda:api.now,raw_history_enabled=True)

def test_legacy_crepe_migration_preserves_delivered_question_and_owned_facts():
    api=Hosted();api.now+=timedelta(days=1)
    def prepare(body):
        c=context(body)
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[data(item='เครป',amount=60,date='2026-10-13',date_text='เมื่อวาน',source_refs=[c['current_source_ref']])]})
    with TestClient(api.app(raw_history_enabled=False)) as client:
        initial,_=api.send_text(client,'ค่าเครป 60 บาทเมื่อวาน',prepare,response(name='lookup_catalog',args={'kind':'item','query':'เครป'}))
    old=deepcopy(api.snapshot()['history24']['current']['drafts'])
    with TestClient(app(api)) as client:
        def begin(body):
            c=context(body);assert any(r['role']=='assistant' and r['text']==initial.message for r in c['history'])
            d=c['available_drafts'][0]
            return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':None,'category':None,'subcategory':None,'existing_item':None})
        result,_=api.send_text(client,'ใหม่',begin);assert result.data['tool_result']['item_enrollment']['field']=='category'
    draft=next(iter(api.snapshot()['history24']['current']['drafts'].values()))
    assert draft['entries'][0]['amount']=='60' and draft['entries'][0]['date']=='2026-10-13'
    assert api.snapshot()['history24']['current']['raw_history']['enabled'] and api.google.sheet_writes==0
    assert len(old)==1

def test_new_raw_preview_controls_and_journal_survive_compatible_rollback():
    api=Hosted()
    with TestClient(app(api)) as client:preview=api.create(client)
    event=api.event(text='synthetic save button');event.pop('message');event['type']='postback'
    event['postback']={'data':preview.data['line_message']['contents']['footer']['contents'][0]['action']['data']}
    models=api.models
    with TestClient(app(api,enabled=False)) as client:
        assert api.send(client,event).status_code==200
        assert result_from(api.snapshot()['receipts'][event['webhookEventId']]['result']).status=='recorded'
    with TestClient(app(api,enabled=False)) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.models==models and api.google.sheet_writes==1
    assert api.snapshot()['history24']['current']['raw_history']['enabled']

def test_migration_failure_retains_exact_legacy_backup_before_model_call(monkeypatch):
    from test_history_first import Harness as Legacy
    old=Legacy();old.send('  ข้อความเดิม  ',response(text='คำตอบเดิม'))
    state=old.state;original=deepcopy(state.history)
    h=Harness(state=state)
    h.counter=old.counter;h.now=old.now
    # Emulate a legacy snapshot restored after constructing the new factory.
    state.history=deepcopy(original);state.raw_history_enabled=False;state.raw_history_migrated=False
    original_put=state.raw_journal.put;calls=0
    def fail(row):
        nonlocal calls
        calls+=1
        if calls==2:raise RuntimeError('synthetic journal migration interruption')
        return original_put(row)
    monkeypatch.setattr(state.raw_journal,'put',fail)
    with pytest.raises(RuntimeError,match='migration interruption'):h.send('ข้อความใหม่',response(text='ไม่ควรถูกเรียก'))
    assert state.history==original and not state.raw_history_enabled
    assert history_snapshot(state)['history']==original and not h.port.requests
