"""Typed consent through signed production routing and cold durable adapters."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest
from fastapi.testclient import TestClient
from test_calendar_factory import Hosted
from test_calendar_batch_factory_v13 import proposed,text
from test_calendar_mvp import response
from line_assistant.pilot_http import rpc_budget
from line_assistant.state import Pending

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Only injected synthetic services')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def turn(api,value,*answers,postback=False,enabled=True):
    with TestClient(api.app(enabled)) as client:return api.turn(client,value,*answers,postback=postback)

def mutate(api,change):
    app=api.app();state=app.state.assistant.state
    with rpc_budget(96),state.session():change(state);state.checkpoint()
    app.state.pilot_http.close()

def pending(api,*,expired=False):
    def put(state):
        state.pending=Pending('add_transactions','synthetic-legacy','old-legacy',int((api.now-timedelta(hours=8)).timestamp()*1000),
            api.now+timedelta(seconds=-1 if expired else 1200),
            {'entries':[{'date':'2026-10-16','item':'กาแฟ','amount':'23','note':'','kind':'expense'}]})
    mutate(api,put)

def old_expense(api):
    def proposal(body):
        c=json.loads(body['input'][0]['content'])
        return response('prepare_expenses',{'draft_ref':None,'draft_version':None,'entries':[
            {'entry_ref':None,'item':'กาแฟ','amount':'23','date':None,'date_text':None,'date_context':None,'note':'','category':None,'subcategory':None,'source_refs':[c['current_source_ref']]}]})
    got,_=turn(api,'กาแฟ 23 บาท',proposal);assert got.status=='preview',got

def retained(api):
    old_expense(api)
    turn(api,text(),response(text='รับคำขอไว้ ยังไม่ได้สร้างนัด'))
    api.now+=timedelta(hours=9,minutes=34)
    old,_=turn(api,text(),proposed)
    api.now+=timedelta(seconds=10)
    latest,event=turn(api,text(),proposed)
    return old,latest,event

@pytest.mark.parametrize('phrase',['ยืนยันชุดนัด','ยืนยันชุดนัดนี้ครับ','ขอยืนยันสร้างชุดนัดล่าสุดค่ะ','ยืนยันนัดทั้งชุด','ยืนยันนัดชุดนี้นะครับ','สร้างชุดนัดนี้เลย','ยืนยัน ชุดนัด นี้ ครับ'])
def test_typed_variants_select_latest_delivered_set_with_24h_history_old_expense_and_cold_restore(phrase):
    api=Hosted();old,latest,event=retained(api);before=api.snapshot();calls=len(api.model_bodies)
    assert before['history24']['current']['drafts']
    pending(api)
    result,confirmation=turn(api,phrase)
    assert result.status=='created' and api.calendar.posts==2 and len(api.model_bodies)==calls,result
    state=api.snapshot()['history24']['current'];sets=state['calendar_batches_v1']['batches']
    assert sets[latest.data['calendar_batch_view']['batch_ref']]['status']=='recorded'
    assert sets[old.data['calendar_batch_view']['batch_ref']]['status']=='review'
    assert state['drafts']==before['history24']['current']['drafts'] and api.snapshot()['pending'] is not None
    assert api.google.sheet_writes==0
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(confirmation)).status_code==200
    assert api.calendar.posts==2

def test_real_c2_postback_uses_calendar_router_with_legacy_pending_and_cold_restore():
    api=Hosted();r,_=turn(api,text(),proposed);pending(api)
    value=r.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    got,_=turn(api,value,postback=True)
    assert got.status=='created' and api.calendar.posts==2 and len(api.model_bodies)==1 and api.snapshot()['pending'] is not None

def test_review_survives_delivered_button_error_then_named_text_consent():
    api=Hosted();r,_=turn(api,text(),proposed)
    data=r.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    wrong=data[:-16]+'0'*16
    bad,_=turn(api,wrong,postback=True);assert bad.status=='stale' and not api.calendar.posts
    vague,_=turn(api,'ยืนยัน');assert vague.status=='clarification' and not api.calendar.posts
    good,_=turn(api,'ยืนยันชุดนัดนี้')
    assert good.status=='created' and api.calendar.posts==2 and len(api.model_bodies)==1

@pytest.mark.parametrize('fault',['delivery_failed','content_hash','wrong_workbook','expired'])
def test_typed_consent_never_uses_unverified_or_expired_delivery(fault):
    api=Hosted();api.reject_reply=fault=='delivery_failed';r,event=turn(api,text(),proposed);api.reject_reply=False
    if fault=='content_hash':mutate(api,lambda state:setattr(state.replies[event['webhookEventId']],'content_hash','0'*64))
    if fault=='wrong_workbook':mutate(api,lambda state:state.event_workbooks.__setitem__(event['webhookEventId'],'unrelated-workbook'))
    if fault=='expired':api.now+=timedelta(hours=25)
    got,_=turn(api,'ยืนยันชุดนัด')
    assert got.status in {'stale','clarification'} and not api.calendar.posts and len(api.model_bodies)==1,got

def revise(body):
    c=json.loads(body['input'][0]['content']);b=c['calendar_batches'][-1];d=b['events'][1]
    return response('prepare_calendar_events',{'batch_ref':b['batch_ref'],'batch_version':b['version'],'events':[
        {'draft_ref':d['draft_ref'],'draft_version':d['version'],'title':None,'date_text':None,'time_text':None,'end_date_text':None,'end_time_text':'18:00','duration_text':None,'source_refs':[c['current_source_ref']]}]})

def test_failed_delivery_of_updated_review_never_falls_back_to_older_set_or_old_nonce():
    api=Hosted();old,_=turn(api,text(),proposed);latest,_=turn(api,text(),proposed)
    api.reject_reply=True;fresh,_=turn(api,'แก้นัดที่สองสิ้นสุด 18:00',revise);api.reject_reply=False
    assert fresh.status=='preview'
    got,_=turn(api,'ยืนยันชุดนัดนี้');assert got.status=='stale' and not api.calendar.posts,got
    got,_=turn(api,latest.data['confirm_command']);assert got.status=='stale' and not api.calendar.posts

def test_successfully_delivered_update_binds_new_version_and_payload():
    api=Hosted();old,_=turn(api,text(),proposed);fresh,_=turn(api,'แก้นัดที่สองสิ้นสุด 18:00',revise)
    got,_=turn(api,old.data['confirm_command']);assert got.status=='stale' and not api.calendar.posts
    got,_=turn(api,'ยืนยันนัดทั้งชุด');assert got.status=='created' and api.calendar.posts==2
    assert sorted(e['end']['dateTime'] for e in api.calendar.events.values())==['2026-10-22T17:00:00+07:00','2026-10-23T18:00:00+07:00']

@pytest.mark.parametrize('proposal_error',[False,True])
def test_newer_unreviewed_calendar_work_requires_natural_clarification(proposal_error):
    api=Hosted();turn(api,text(),proposed)
    def partial(body):
        value=proposed(body);args=json.loads(value['output'][0]['arguments'])
        for event in args['events'][1 if proposal_error else 0:]:event['end_time_text']=None
        return response('prepare_calendar_events',args)
    incomplete,_=turn(api,text().replace('8:00-17:00','8:00'),partial)
    assert incomplete.status=='clarification'
    got,_=turn(api,'ยืนยันชุดนัด');assert got.status in {'stale','clarification'} and not api.calendar.posts
    assert 'ปุ่ม' not in got.message

def test_plain_confirmation_uses_immediately_delivered_calendar_review_despite_old_expense():
    api=Hosted();old_expense(api);api.now+=timedelta(hours=2);turn(api,text(),proposed);pending(api,expired=True)
    got,_=turn(api,'ยืนยัน');assert got.status=='created' and api.calendar.posts==2 and api.google.sheet_writes==0,got

@pytest.mark.parametrize('fault',['nonce','version','text_postback','label_postback','wrong_owner'])
def test_bound_postback_and_owner_safeguards_remain_enforced(fault):
    api=Hosted();r,_=turn(api,text(),proposed);data=r.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    if fault=='nonce':data=data[:-16]+'0'*16
    if fault=='version':data=data.replace('|1|','|99|')
    if fault=='label_postback':data='ยืนยันชุดนัด'
    if fault=='wrong_owner':
        e=api.event('ยืนยันชุดนัด');e['source']['userId']='U'+'b'*32
        with TestClient(api.app()) as client:assert api.send(client,e).status_code==200
    else:
        got,_=turn(api,data,postback=fault!='text_postback');assert got.status=='stale',got
    assert not api.calendar.posts and len(api.model_bodies)==1

def test_off_bridge_retains_batch_and_does_not_confirm_typed_text():
    api=Hosted();turn(api,text(),proposed);before=api.snapshot()['history24']['current']['calendar_batches_v1']
    got,_=turn(api,'ยืนยันชุดนัดนี้',enabled=False)
    assert got.status=='stale' and not api.calendar.posts and api.snapshot()['history24']['current']['calendar_batches_v1']==before

def test_two_reviews_with_equal_owner_timestamp_require_natural_target_clarification():
    api=Hosted();turn(api,text(),proposed);api.now-=timedelta(seconds=1)
    turn(api,text(),proposed)
    got,_=turn(api,'ยืนยันชุดนัดนี้')
    assert got.status=='clarification' and 'ใด' in got.message and 'ปุ่ม' not in got.message and not api.calendar.posts

def test_older_owner_text_cannot_confirm_a_newer_review():
    api=Hosted();r,review=turn(api,text(),proposed);event=api.event('ยืนยันชุดนัด');event['timestamp']=review['timestamp']-1
    with TestClient(api.app()) as client:assert api.send(client,event).status_code==200
    assert not api.calendar.posts and len(api.model_bodies)==1

@pytest.mark.parametrize('phrase',['อย่ายืนยันชุดนัด','ยืนยันชุดนัดไหม','ยืนยันชุดนัดแต่แก้นัดที่สองเป็น 19:00','หมายเหตุ ยืนยันชุดนัด','โอเค'])
def test_negation_question_correction_and_unrelated_acknowledgement_never_grant_consent(phrase):
    api=Hosted();turn(api,text(),proposed)
    got,_=turn(api,phrase,response(text='กรุณาตรวจร่างก่อนยืนยัน'))
    assert got.status!='created' and not api.calendar.posts
