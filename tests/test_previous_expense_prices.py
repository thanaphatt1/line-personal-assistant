"""Signed missing-price choices over wholly synthetic Google/LINE/model APIs."""
from copy import deepcopy
from datetime import date,datetime,timedelta
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from line_assistant.history_cloud_run import create_history_cloud_run_app,HistoryConfig
from line_assistant.runtime_config import ConfigError
from line_assistant.sheet_dates import date_to_serial
from test_deployment_configuration import TARGET
from test_calendar_factory import values
from test_calendar_mvp import response
from test_historical_categories import FullHistoricalHosted,preview as historical_preview


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Previous-price tests cannot connect to a network')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


class PriceHosted(FullHistoricalHosted):
    def __init__(self):
        super().__init__();self.prices=True;self.price_lost_ack=False;self.price_before_ack=False;self.now=datetime.fromisoformat('2026-10-18T09:00:00+07:00')
        for key in list(self.google.cells):
            if key[0]=='All Expenses' and key[1]>1:del self.google.cells[key]
        for row,item in enumerate(['Spotify','YouTube','ค่าส่วนกลาง','Spotify Family'],4):
            for col,v in enumerate([item,'Subscriptions','Monthly'],1):self.google.cells[('Database',row,col)]=v
        for row,(day,item,amount) in enumerate([
            ('2026-09-01','Spotify',149),('2026-10-01','Spotify',169),
            ('2026-09-02','YouTube',179),('2026-09-03','ค่าส่วนกลาง',1200),
            ('2026-09-04','Spotify Family',299)],2):self.saved(row,day,item,amount)

    def saved(self,row,day,item,amount,note=''):
        for col,v in enumerate([date_to_serial(date.fromisoformat(day)),item,amount,note,'synthetic_price_'+str(row)],1):self.google.cells[('All Expenses',row,col)]=v

    def app(self,enabled=True):
        flags={'PILOT_PREVIOUS_EXPENSE_PRICES_ENABLED':str(self.prices).lower(),
            'PILOT_HISTORICAL_CATEGORIES_ENABLED':str(self.historical).lower(),
            'PILOT_PROVISIONAL_CATEGORIES_ENABLED':str(self.labels).lower(),
            'PILOT_PROVISIONAL_MAPPING_ENABLED':str(self.mapping).lower(),
            'PILOT_SAVED_EXPENSE_EDITS_ENABLED':str(self.edit_flags).lower(),
            'PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED':str(self.edit_flags).lower(),
            'PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY':'reviewed_single_writer',
            'PILOT_RESPONSE_REALIZATION_ENABLED':str(self.voice).lower()}
        return create_history_cloud_run_app({**values(enabled),**TARGET,**flags},transport=httpx.MockTransport(self.handle),secret_loader=lambda name:SecretStr('synthetic-'+name),clock=lambda:self.now)

    def drafts(self):return self.snapshot()['history24']['current']['drafts']

    def handle(self,request):
        if self.configured and request.url.host=='sheets.googleapis.com' and request.method=='POST':
            body=json.loads(request.content)
            if any(r.get('appendCells',{}).get('sheetId')==501 for r in body.get('requests',[])):
                if self.price_before_ack:raise httpx.ReadTimeout('Synthetic failure before append',request=request)
                result=super().handle(request)
                if self.price_lost_ack:raise httpx.ReadTimeout('Synthetic lost append acknowledgement',request=request)
                return result
        return super().handle(request)


def turn(api,text,*answers):
    with TestClient(api.app()) as web:return api.turn(web,text,*answers)


def prepare(items=('Spotify',),*,update=False,amounts=None,choose=False,date_text=None):
    def answer(body):
        c=json.loads(body['input'][0]['content']);draft=c['available_drafts'][0] if update else None;entries=[]
        for item in items:
            old=next(e for e in draft['entries'] if e['item']==item) if draft else None
            entries.append({'entry_ref':old['entry_ref'] if old else None,'item':None if old else item,'item_text':None,
                'amount':(amounts or {}).get(item),'date_text':date_text,'date_ref':None,'note':None,
                'source_refs':[c['current_source_ref']],'field_sources':None,
                'previous_price_ref':old['previous_price']['ref'] if choose and old else None})
        return response('prepare_expenses',{'draft_ref':draft['draft_ref'] if draft else None,'draft_version':draft['version'] if draft else None,'entries':entries})
    return answer


def offer(api,items=('Spotify',)):
    return turn(api,'บันทึกค่า '+ ' และ '.join(items),prepare(items))


def accepted(api,items=('Spotify',),text='ใช้เดิม'):
    return turn(api,text,prepare(items,update=True,choose=True))


def test_offer_is_grounded_latest_past_payment_and_selection_still_needs_final_review():
    api=PriceHosted();result,_=offer(api);entry=result.data['tool_result']['entries'][0]
    assert result.status=='clarification' and entry['amount'] is None
    assert entry['date']=='2026-10-18' and entry['previous_price']['amount']=='169.00'
    assert entry['previous_price']['source_date']=='2026-10-01' and entry['previous_price']['varied'] is True
    assert '169.00' in result.message and '2026-10-01' in result.message and 'หลายยอด' in result.message
    assert not api.google.sheet_writes and not next(iter(api.drafts().values()))['review']
    result,_=accepted(api);assert result.status=='preview',result
    entry=result.data['tool_result']['entries'][0]
    assert entry['amount']=='169' and entry['date']=='2026-10-18'
    assert entry['previous_price']['state']=='accepted' and '2026-10-01' in result.message
    assert not api.google.sheet_writes and not api.calendar.posts
    calls=len(api.model_bodies);result,event=turn(api,'ยืนยัน')
    assert result.status=='recorded' and api.google.sheet_writes==1 and len(api.model_bodies)==calls
    with TestClient(api.app()) as web:assert api.send(web,deepcopy(event)).status_code==200
    assert api.google.sheet_writes==1
    users=[json.loads(v['fields']['payload']['stringValue'])['text'] for k,v in api.google.documents.items() if '/assistant_raw_messages/' in k and json.loads(v['fields']['payload']['stringValue'])['role']=='user']
    assert users[:2]==['บันทึกค่า Spotify','ใช้เดิม']


@pytest.mark.parametrize('item',['YouTube','ค่าส่วนกลาง','Spotify Family'])
def test_exact_catalog_name_does_not_borrow_another_plan_or_item_price(item):
    api=PriceHosted();r,_=offer(api,(item,));e=r.data['tool_result']['entries'][0]
    assert e['previous_price']['amount']=={'YouTube':'179.00','ค่าส่วนกลาง':'1200.00','Spotify Family':'299.00'}[item]
    assert e['amount'] is None and not api.google.sheet_writes


@pytest.mark.parametrize('mutation',['none','annotated','same_day_different','zero','refund','formula','future_only'])
def test_no_reliable_source_means_ask_amount_without_guessing(mutation):
    api=PriceHosted()
    if mutation=='none':
        for row in [2,3]:api.google.cells[('All Expenses',row,2)]='Different subscription'
    elif mutation=='annotated':api.google.cells[('All Expenses',3,4)]='family plan, two months combined'
    elif mutation=='same_day_different':api.saved(7,'2026-10-01','Spotify',199)
    elif mutation in {'zero','refund','formula'}:api.google.cells[('All Expenses',3,3)]={'zero':0,'refund':-169,'formula':'=100+69'}[mutation]
    else:
        for row in [2,3]:api.google.cells[('All Expenses',row,1)]=date_to_serial(date(2026,10,20))
    r,_=offer(api);e=r.data['tool_result']['entries'][0]
    assert e['amount'] is None and not e.get('previous_price') and r.status=='clarification'
    assert not api.google.sheet_writes


@pytest.mark.parametrize('item',['Spotfy','Unknown subscription'])
def test_unresolved_or_unknown_name_has_no_price_offer(item):
    api=PriceHosted();r,_=offer(api,(item,))
    assert all(not e.get('previous_price') for e in r.data['tool_result'].get('entries',[]))
    assert not api.google.sheet_writes


def test_future_history_ignored_and_explicit_transaction_day_not_replaced_by_source_day():
    api=PriceHosted();api.saved(7,'2026-10-20','Spotify',999)
    r,_=turn(api,'บันทึกค่า Spotify วันที่ 15 ต.ค.',prepare(date_text='15 ต.ค.'))
    e=r.data['tool_result']['entries'][0]
    assert e['date']=='2026-10-15' and e['previous_price']['amount']=='169.00' and e['previous_price']['source_date']=='2026-10-01'
    r,_=accepted(api);assert r.status=='preview' and r.data['tool_result']['entries'][0]['date']=='2026-10-15'


@pytest.mark.parametrize('amount',[179,169])
def test_new_explicit_amount_overrides_offer_and_drops_history_dependency(amount):
    api=PriceHosted();offer(api)
    r,_=turn(api,'เดือนนี้'+str(amount),prepare(update=True,amounts={'Spotify':amount}))
    assert r.status=='preview' and r.data['tool_result']['entries'][0]['amount']==str(amount)
    assert not r.data['tool_result']['entries'][0].get('previous_price')
    api.google.cells[('All Expenses',3,3)]=999
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded' and api.google.sheet_writes==1


def test_multiple_missing_items_need_named_or_all_selection_then_one_final_confirmation():
    api=PriceHosted();r,_=offer(api,('Spotify','YouTube'))
    assert len([e for e in r.data['tool_result']['entries'] if e.get('previous_price')])==2
    r,_=accepted(api,('Spotify','YouTube'),'ใช้เดิมทั้งหมด')
    assert r.status=='preview' and {e['amount'] for e in r.data['tool_result']['entries']}=={'169','179'}
    assert not api.google.sheet_writes
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded' and api.google.sheet_writes==1


def test_bare_use_old_cannot_select_one_of_multiple_missing_prices():
    api=PriceHosted();offer(api,('Spotify','YouTube'));before=deepcopy(api.drafts())
    r,_=accepted(api,('Spotify',),'ใช้เดิม')
    assert r.status=='clarification' and not api.google.sheet_writes
    assert all(e['amount'] is None for d in api.drafts().values() for e in d['entries'])
    assert list(api.drafts())==list(before) and 'Spotify' in r.message and 'YouTube' in r.message


@pytest.mark.parametrize('stage',['before_choice','before_final'])
def test_changed_source_blocks_old_price_choice_or_final_write(stage):
    api=PriceHosted();offer(api)
    if stage=='before_final':accepted(api)
    api.google.cells[('All Expenses',3,3)]=199
    r,_=accepted(api) if stage=='before_choice' else turn(api,'ยืนยัน')
    assert r.status in {'clarification','stale'} and not api.google.sheet_writes


@pytest.mark.parametrize('obstacle',['undelivered','intervening','foreign_ref','disabled','expired'])
def test_old_or_unverified_offer_never_supplies_amount(obstacle):
    api=PriceHosted();api.reject_reply=obstacle=='undelivered';offer(api);api.reject_reply=False
    if obstacle=='intervening':turn(api,'ขอบคุณ',response(text='ครับ'))
    if obstacle=='disabled':api.prices=False
    if obstacle=='expired':api.now+=timedelta(hours=25)
    answer=prepare(update=True,choose=True)
    if obstacle=='foreign_ref':
        def answer(body):
            value=prepare(update=True,choose=True)(body);args=json.loads(value['output'][0]['arguments']);args['entries'][0]['previous_price_ref']='price_'+'0'*20;value['output'][0]['arguments']=json.dumps(args);return value
    r,_=turn(api,'ใช้เดิม',answer)
    assert r.status!='preview' and not api.google.sheet_writes


def test_flag_off_does_not_read_price_history_or_change_existing_expense_flow():
    api=PriceHosted();api.prices=False;r,_=offer(api)
    assert not r.data['tool_result']['entries'][0].get('previous_price')
    assert not any('previous_price_ref' in json.dumps(t) for t in api.model_bodies[0]['tools'])
    r,_=turn(api,'169 บาท',prepare(update=True,amounts={'Spotify':169}));assert r.status=='preview'
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded'


def test_explicit_same_amount_after_selection_removes_dependency_even_when_feature_disabled():
    api=PriceHosted();offer(api);accepted(api);api.prices=False
    r,_=turn(api,'เดือนนี้169',prepare(update=True,amounts={'Spotify':169}))
    assert r.status=='preview' and not r.data['tool_result']['entries'][0].get('previous_price')
    api.google.cells[('All Expenses',3,3)]=999
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded'


@pytest.mark.parametrize('phrase',['ใช้เดิมแต่เดือนนี้179','ไม่ใช้เดิม','ยกเลิก','ใช้เดิม 169 USD'])
def test_model_cannot_use_old_amount_against_new_or_negative_owner_evidence(phrase):
    api=PriceHosted();offer(api);r,_=turn(api,phrase,prepare(update=True,choose=True))
    assert r.status!='preview' and not api.google.sheet_writes


def test_model_cannot_invent_a_missing_price_without_the_owned_offer_reference():
    api=PriceHosted();offer(api)
    r,_=turn(api,'ใช้เดิม',prepare(update=True,amounts={'Spotify':169}))
    assert r.status=='clarification' and next(iter(api.drafts().values()))['entries'][0]['amount'] is None
    assert not api.google.sheet_writes


@pytest.mark.parametrize('text',['บันทึก Spotify 2 เดือน','บันทึกค่า Spotify USD'])
def test_count_or_foreign_currency_request_does_not_infer_a_unit_recurring_price(text):
    api=PriceHosted();r,_=turn(api,text,prepare())
    assert not any(e.get('previous_price') for e in r.data.get('tool_result',{}).get('entries',[]))
    assert not api.google.sheet_writes


def test_named_selection_then_one_remaining_price_can_use_a_natural_bare_answer():
    api=PriceHosted();offer(api,('Spotify','YouTube'))
    r,_=accepted(api,('Spotify',),'ใช้เดิมเฉพาะ Spotify')
    assert r.status=='clarification' and [e['amount'] for e in r.data['tool_result']['entries']]==['169',None]
    r,_=accepted(api,('YouTube',),'ใช้เดิม');assert r.status=='preview'
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded' and api.google.sheet_writes==1


def test_unknown_new_transaction_date_never_uses_the_historical_price_date_as_default():
    api=PriceHosted()
    r,_=turn(api,'บันทึกค่า Spotify วันที่ 31 เดือนหน้า',prepare(date_text='31 เดือนหน้า'))
    assert r.status!='preview' and not api.google.sheet_writes
    assert all(e.get('date')!='2026-10-01' for e in r.data.get('tool_result',{}).get('entries',[]))


def test_cancellation_removes_offer_and_old_reference_cannot_record():
    api=PriceHosted();r,_=offer(api);d=next(iter(api.drafts().values()))
    result,_=turn(api,'ยกเลิก '+d['draft_ref']);assert result.status=='cancelled'
    assert not api.drafts() and not api.google.sheet_writes


@pytest.mark.parametrize('stage',['choice','final'])
def test_row_relocation_preserves_price_fingerprint(stage):
    api=PriceHosted();offer(api)
    if stage=='final':accepted(api)
    for col in range(1,6):
        a=api.google.cells[('All Expenses',2,col)];b=api.google.cells[('All Expenses',3,col)]
        api.google.cells[('All Expenses',2,col)]=b;api.google.cells[('All Expenses',3,col)]=a
    if stage=='choice':assert accepted(api)[0].status=='preview'
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded' and api.google.sheet_writes==1


def test_price_source_change_after_confirmation_before_first_attempt_stays_read_only(monkeypatch):
    from line_assistant.history_first import HistoryFirstAssistant
    api=PriceHosted();offer(api);accepted(api);original=HistoryFirstAssistant._resume
    def crash(*args):raise RuntimeError('Synthetic restart after durable confirmation')
    monkeypatch.setattr(HistoryFirstAssistant,'_resume',crash)
    with TestClient(api.app()) as web:
        event=api.event('ยืนยัน');assert api.send(web,event).status_code==503
    assert all(a['status']=='prepared' for a in api.snapshot()['actions'].values())
    monkeypatch.setattr(HistoryFirstAssistant,'_resume',original)
    api.google.cells[('All Expenses',3,3)]=999
    with TestClient(api.app()) as web:assert api.send(web,deepcopy(event)).status_code==200
    assert all(a['status']=='completed' and a['result']['status']=='stale' for a in api.snapshot()['actions'].values())
    assert not api.google.sheet_writes


@pytest.mark.parametrize('uncertain',['committed','old_state'])
def test_uncertain_price_derived_action_reconciles_only_with_feature_off(uncertain):
    api=PriceHosted();offer(api);accepted(api)
    api.price_lost_ack=uncertain=='committed';api.price_before_ack=uncertain=='old_state'
    with TestClient(api.app()) as web:
        event=api.event('ยืนยัน');assert api.send(web,event).status_code==503
    assert any(a['status']=='uncertain' for a in api.snapshot()['actions'].values())
    before=api.google.sheet_writes;api.price_lost_ack=api.price_before_ack=False;api.prices=False
    with TestClient(api.app()) as web:assert api.send(web,deepcopy(event)).status_code==200
    assert api.google.sheet_writes==before
    actions=api.snapshot()['actions'].values()
    assert all(a['status']=='completed' for a in actions) if uncertain=='committed' else any(a['status']=='uncertain' for a in actions)


def test_feature_off_cannot_confirm_a_price_derived_uncommitted_review():
    api=PriceHosted();offer(api);accepted(api);api.prices=False
    r,_=turn(api,'ยืนยัน');assert r.status=='stale' and not api.google.sheet_writes


def test_stale_selected_price_can_be_replaced_with_an_explicit_owner_amount():
    api=PriceHosted();offer(api);accepted(api);api.google.cells[('All Expenses',3,3)]=199
    r,_=turn(api,'ยืนยัน');assert r.status=='stale' and 'ระบุยอดใหม่' in r.message
    r,_=turn(api,'เดือนนี้179',prepare(update=True,amounts={'Spotify':179}))
    assert r.status=='preview' and not r.data['tool_result']['entries'][0].get('previous_price')
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded' and api.google.sheet_writes==1


@pytest.mark.parametrize('field,bad',[('ref',None),('source_date',None),('offered_at',None),('accepted_at',None),('source_hash','forged')])
def test_invalid_persisted_price_proof_is_rejected_as_a_validation_error(field,bad):
    from line_assistant.previous_prices import validate
    api=PriceHosted();offer(api);accepted(api);entry=deepcopy(next(iter(api.drafts().values()))['entries'][0])
    entry['previous_price'][field]=bad
    with pytest.raises(ValueError):validate(entry)


def test_historical_review_is_preserved_through_price_choice_and_expense_confirmation():
    api=PriceHosted();api.saved(7,'2026-10-02','Unmapped train ticket',20)
    result,_=historical_preview(api);assert result.status=='preview'
    historical=deepcopy(api.snapshot()['pending']);offer(api);accepted(api)
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded' and api.google.sheet_writes==1
    assert api.snapshot()['pending']==historical and api.mapping_posts==0


def test_calendar_interruption_keeps_price_offer_but_requires_a_fresh_question_before_choice():
    from test_calendar_all_day_v15 import proposal
    api=PriceHosted();offer(api)
    r,_=turn(api,'เพิ่มนัดตรวจระบบ 22 ต.ค. 2026 ทั้งวัน',proposal([{'title':'ตรวจระบบ','date_text':'22 ต.ค. 2026','all_day':True}]))
    assert r.status=='preview'
    command=r.data['confirm_command']
    r,_=turn(api,'ยืนยัน');assert r.status=='clarification' and api.calendar.posts==0
    r,_=turn(api,command);assert r.status=='created' and api.calendar.posts==1 and not api.google.sheet_writes
    r,_=accepted(api);assert r.status=='clarification' and not api.google.sheet_writes
    d=next(iter(api.drafts().values()))
    r,_=turn(api,'ขอดูค่า Spotify ใหม่',response('read_expense_drafts',{'draft_ref':d['draft_ref']}));assert r.status=='clarification'
    r,_=accepted(api);assert r.status=='preview'
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded' and api.google.sheet_writes==api.calendar.posts==1


def test_ambiguous_choice_reasks_both_prices_and_named_answer_uses_only_current_owner_reply():
    api=PriceHosted();offer(api,('Spotify','YouTube'))
    r,_=accepted(api,('Spotify',),'ใช้เดิม');assert r.status=='clarification'
    r,_=accepted(api,('Spotify',),'ใช้เดิมเฉพาะ Spotify');assert r.status=='clarification'
    assert [e['amount'] for e in r.data['tool_result']['entries']]==['169',None]
    r,_=accepted(api,('YouTube',),'ใช้เดิม');assert r.status=='preview'


def test_reading_old_price_question_refreshes_source_without_selecting_or_writing():
    api=PriceHosted();offer(api);api.google.cells[('All Expenses',3,3)]=199
    d=next(iter(api.drafts().values()))
    r,_=turn(api,'ขอดูยอดเดิมของ Spotify ใหม่',response('read_expense_drafts',{'draft_ref':d['draft_ref']}))
    e=r.data['tool_result']['entries'][0]
    assert r.status=='clarification' and e['amount'] is None and e['previous_price']['amount']=='199.00'
    assert not api.google.sheet_writes
    r,_=accepted(api);assert r.status=='preview' and r.data['tool_result']['entries'][0]['amount']=='199'


@pytest.mark.parametrize('bad',['yes','1',''])
def test_previous_price_flag_requires_explicit_boolean_configuration(bad):
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**values(),**TARGET,'PILOT_PREVIOUS_EXPENSE_PRICES_ENABLED':bad})
