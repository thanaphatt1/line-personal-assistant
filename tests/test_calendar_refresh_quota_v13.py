"""Expired review recovery follows real read-first routing; quota stays intact."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest
from fastapi.testclient import TestClient
from test_calendar_factory import Hosted
from test_calendar_batch_factory_v13 import text,proposed
from test_calendar_text_confirmation_v13 import turn,pending,retained
from test_calendar_mvp import response,NOW
from line_assistant.history_context import HistoryState
from line_assistant.model_api import DurableModelGuard,BudgetBlocked,budget_notice,MAX_MODEL_RECORDS
from line_assistant.state import Claim,ModelBudget,ModelRun

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Injected fake services only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def rebuild(body):
    c=json.loads(body['input'][0]['content']);assert not c['calendar_batches']
    assert json.loads(body['input'][-1]['output'])['review_refresh']['write_authorized'] is False
    ref=next(r['source_ref'] for r in reversed(c['history']) if r['role']=='user' and 'กิจกรรมทดสอบA' in r['text'] and '8:00-17:00' in r['text'])
    value=proposed(body);args=json.loads(value['output'][0]['arguments'])
    for e in args['events']:e['source_refs']=[ref]
    return response('prepare_calendar_events',args)

@pytest.mark.parametrize('phrase',['ช่วยตรวจร่างชุดนัดเดิมสองนัดอีกครั้ง','ขอดูร่างนัดเดิมอีกครั้ง','ทบทวนนัดก่อนหน้านี้ใหม่'])
def test_expired_read_first_refresh_recovers_retained_sources_then_requires_new_typed_consent(phrase):
    api=Hosted();_,old,_=retained(api);api.now+=timedelta(minutes=31);pending(api)
    before=api.snapshot()['pending'];calls=len(api.model_bodies)
    fresh,event=turn(api,phrase,response('read_calendar_drafts',{'draft_ref':None}),rebuild)
    assert fresh.status=='preview' and len(api.model_bodies)==calls+2 and api.calendar.posts==0,fresh
    assert len(fresh.data['calendar_batch_view']['events'])==2
    assert fresh.data['calendar_batch_view']['batch_ref']!=old.data['calendar_batch_view']['batch_ref']
    assert api.snapshot()['pending']==before
    denied,_=turn(api,old.data['confirm_command']);assert denied.status=='stale' and not api.calendar.posts
    saved,_=turn(api,'ยืนยันชุดนัดนี้');assert saved.status=='created' and api.calendar.posts==2 and api.google.sheet_writes==0

def test_unexpired_read_first_refresh_delivers_new_bound_review_without_creating_duplicate_set():
    api=Hosted();old,_=turn(api,text(),proposed)
    def review(body):
        c=json.loads(body['input'][0]['content']);b=c['calendar_batches'][-1]
        return response('control_calendar_events',{'batch_ref':b['batch_ref'],'batch_version':b['version'],'operation':'review','event_refs':[]})
    fresh,_=turn(api,'ช่วยตรวจร่างชุดนัดเดิมอีกครั้ง',response('read_calendar_drafts',{'draft_ref':None}),review)
    assert fresh.status=='preview' and fresh.data['calendar_batch_view']['batch_ref']==old.data['calendar_batch_view']['batch_ref']
    assert fresh.data['confirm_command']!=old.data['confirm_command'] and not api.calendar.posts
    old_result,_=turn(api,old.data['confirm_command']);assert old_result.status=='stale'
    got,_=turn(api,'ยืนยันนัดทั้งชุด');assert got.status=='created' and api.calendar.posts==2

def test_expired_historical_review_reference_uses_existing_second_step_for_fresh_review():
    api=Hosted();_,old,_=retained(api);api.now+=timedelta(minutes=31)
    stale=response('control_calendar_events',{'batch_ref':old.data['calendar_batch_view']['batch_ref'],'batch_version':old.data['calendar_batch_view']['version'],'operation':'review','event_refs':[]})
    got,_=turn(api,'ช่วยตรวจร่างชุดนัดเดิมสองนัดอีกครั้ง',stale,rebuild)
    assert got.status=='preview' and len(got.data['calendar_batch_view']['events'])==2 and not api.calendar.posts

def test_empty_calendar_inspection_does_not_automatically_recreate_any_draft():
    api=Hosted();got,_=turn(api,'มีร่างนัดอะไรค้างอยู่บ้าง',response('read_calendar_drafts',{'draft_ref':None}))
    assert got.status=='answer' and len(api.model_bodies)==1 and not api.calendar.posts

def test_no_original_history_available_refresh_asks_for_missing_facts_without_writes():
    api=Hosted();turn(api,text(),proposed);api.now+=timedelta(hours=25)
    got,_=turn(api,'ช่วยตรวจร่างชุดนัดเดิมอีกครั้ง',response('read_calendar_drafts',{'draft_ref':None}),response(text='ขอชื่อนัด วัน และเวลาเริ่มกับสิ้นสุดอีกครั้งครับ'))
    assert got.status=='clarification' and 'ไหน' in got.message and not api.calendar.posts

def test_refresh_quota_exhaustion_after_read_never_retries_or_creates():
    api=Hosted();app=api.app();guard=app.state.assistant.guard
    def first(body):
        guard.limit=next(iter(guard.state.model_budgets.values())).reserved_micro_usd
        return response('read_calendar_drafts',{'draft_ref':None})
    with TestClient(app) as client:got,_=api.turn(client,'ช่วยตรวจร่างชุดนัดเดิมอีกครั้ง',first)
    assert got.status=='model_budget_exhausted' and got.data['quota']['reason']=='monthly_reservation'
    assert got.data['quota']['calls_month']==1 and len(api.model_bodies)==1 and not api.calendar.posts
    assert 'ไม่ใช่ยอดบิลจริง' in got.message

@pytest.mark.parametrize('reason',['monthly_reservation','daily_calls','monthly_calls','model_halted','record_capacity'])
def test_quota_rejections_expose_only_sanitized_reason_and_do_not_change_ledger(reason,caplog):
    state=HistoryState();state.claims['quota-test']=Claim('fingerprint',NOW+timedelta(days=1))
    month=NOW.strftime('%Y-%m');day=NOW.date().isoformat();budget=ModelBudget()
    if reason=='monthly_reservation':budget.reserved_micro_usd=499950
    if reason=='daily_calls':budget.day_calls[day]=100
    if reason=='monthly_calls':budget.calls=300
    if reason=='model_halted':state.model_halted=True
    if reason=='record_capacity':state.model_runs={f'prior{i}':True for i in range(MAX_MODEL_RECORDS)}
    state.model_budgets[month]=budget;before=deepcopy(state.model_budgets)
    guard=DurableModelGuard(state,monthly_micro_usd=500000,max_calls_day=100,max_calls_month=300,clock=lambda:NOW,before_request=lambda:None,reservation_calculator=lambda b:100)
    with pytest.raises(BudgetBlocked) as caught:guard.reserve('quota-test',{})
    d=caught.value.diagnostic
    assert d['reason']==reason and d['actual_billed_spend_known'] is False and state.model_budgets==before
    assert all(word not in caplog.text for word in ['mock-owner','fingerprint','source_ref','snapshot'])
    assert budget_notice(caught.value)

def test_observational_policy_preserves_and_increments_exhausted_ledger_without_counter_gates():
    state=HistoryState();month=NOW.strftime('%Y-%m');day=NOW.date().isoformat()
    state.model_budgets[month]=ModelBudget(900000,700,{day:250},NOW.isoformat())
    state.model_runs={f'prior{i}':ModelRun(str(i),month,100,NOW+timedelta(days=1),'completed',{'prior':i}) for i in range(MAX_MODEL_RECORDS+1)}
    previous=deepcopy(state.model_runs);state.claims['new-turn']=Claim('owner-bound',NOW+timedelta(days=1))
    guard=DurableModelGuard(state,monthly_micro_usd=0,max_calls_day=1,max_calls_month=1,clock=lambda:NOW,
        before_request=lambda:None,reservation_calculator=lambda body:100,enforce_usage_limits=False)
    assert guard.reserve('new-turn',{}) is None
    budget=state.model_budgets[month]
    assert (budget.reserved_micro_usd,budget.calls,budget.day_calls[day])==(900100,701,251)
    assert all(state.model_runs[k]==v for k,v in previous.items()) and len(state.model_runs)==MAX_MODEL_RECORDS+2
    with pytest.raises(BudgetBlocked):guard.reserve('new-turn',{})
    with pytest.raises(BudgetBlocked):guard.reserve('unclaimed',{})
    guard.complete('new-turn',{'safe':'cached'})
    assert guard.reserve('new-turn',{})=={'safe':'cached'}
    with pytest.raises(BudgetBlocked):guard.reserve('new-turn',{'changed':True})
    assert budget.calls==701

def test_observational_policy_keeps_provider_usage_halt():
    state=HistoryState();state.model_halted=True;state.claims['halted']=Claim('owner-bound',NOW+timedelta(days=1))
    guard=DurableModelGuard(state,clock=lambda:NOW,before_request=lambda:None,enforce_usage_limits=False)
    with pytest.raises(BudgetBlocked) as caught:guard.reserve('halted',{})
    assert caught.value.diagnostic['reason']=='model_halted' and not state.model_budgets
