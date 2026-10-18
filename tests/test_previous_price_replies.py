"""Public synthetic regression for the reported 'ค่า Spotify' wording failure.

All labels, prices, dates and providers below are invented fixtures. No private
catalog row, price history, owner identifier or live pending state is copied here.
The normal response-realization flag is ON unless explicitly varied.
"""
from copy import deepcopy
from datetime import datetime
import json,socket

import pytest

from line_assistant import response_realization as voice
from test_previous_expense_prices import PriceHosted,turn,prepare,accepted
from test_calendar_mvp import response
from test_response_realization import generated

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic reply regressions cannot connect')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


class ReplyHosted(PriceHosted):
    def __init__(self,names=('Spotify',),*,history=True):
        super().__init__();self.voice=True;self.now=datetime.fromisoformat('2026-10-18T20:14:00+07:00')
        for key in list(self.google.cells):
            if key[0]=='Database' and key[1]>=4:del self.google.cells[key]
        for row,name in enumerate(names,4):
            for col,v in enumerate([name,'Synthetic subscriptions','Monthly'],1):self.google.cells[('Database',row,col)]=v
        for row in [2,3]:
            self.google.cells[('All Expenses',row,2)]=names[0] if history else 'Different synthetic service'


def proposed(name='Spotify',*,literal=None,amount=None,date_text=None):
    def call(body):
        result=prepare((name,),amounts={name:amount} if amount is not None else None,date_text=date_text)(body)
        args=json.loads(result['output'][0]['arguments']);args['entries'][0]['item_text']=literal
        result['output'][0]['arguments']=json.dumps(args);return result
    return call


def choose_name(api,name,text='ใช่ครับ'):
    def call(body):
        c=json.loads(body['input'][0]['content']);d=c['available_drafts'][0]
        return response('prepare_item_enrollment',{'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':d['entries'][0]['entry_ref'],
            'category':None,'subcategory':None,'existing_item':name,'resolution_step':'use_existing_item'})
    return turn(api,text,call)


@pytest.mark.parametrize('text',['ค่า Spotify','ค่า spotify','ค่า  SPOTIFY','ค่า\tSpotify'])
@pytest.mark.parametrize('model_label',['Spotify','ค่า Spotify'])
def test_exact_reported_utterance_keeps_grounded_question_with_realization_ON(text,model_label):
    api=ReplyHosted();r,_=turn(api,text,proposed(model_label))
    e=r.data['tool_result']['entries'][0]
    assert r.status=='clarification' and e['item']=='Spotify' and e['amount'] is None
    assert e['date']=='2026-10-18' and e['previous_price']['amount']=='169.00'
    assert all(s in r.message for s in ['169.00','2026-10-01','หลายยอด','ใช้ยอดเดิม','ยืนยันอีกครั้ง'])
    assert r.data['response_realization']['status']=='llm' and len(api.model_bodies)==2
    assert not api.google.sheet_writes and not next(iter(api.drafts().values()))['review']


def test_exact_known_name_without_saved_history_asks_only_amount():
    api=ReplyHosted(history=False);r,_=turn(api,'ค่า Spotify',proposed())
    assert r.status=='clarification' and not r.data['tool_result']['entries'][0].get('previous_price')
    assert r.data['tool_result']['missing_fields']==['entries[0].amount']
    assert 'ชื่อรายการใน Database' not in r.message and '169' not in r.message and not api.google.sheet_writes


def test_descriptive_catalog_name_is_visible_then_explicit_choice_offers_price_then_final_consent():
    api=ReplyHosted(('Spotify service',));r,_=turn(api,'ค่า Spotify',proposed('Spotify service',literal='ค่า Spotify'))
    assert r.status=='clarification' and 'Spotify service' in r.message and 'เลือกชื่อก่อน' in r.message
    assert '169' not in r.message and not r.data['tool_result']['entries'][0].get('previous_price')
    r,_=choose_name(api,'Spotify service');assert r.status=='clarification'
    e=r.data['tool_result']['entries'][0]
    assert e['item']=='Spotify service' and e['amount'] is None and e['previous_price']['amount']=='169.00'
    assert '169.00' in r.message and '2026-10-01' in r.message and 'ใช้ยอดเดิม' in r.message
    r,_=accepted(api,('Spotify service',));assert r.status=='preview'
    assert '2026-10-01' in r.message and '2026-10-18' in r.message and not api.google.sheet_writes
    r,_=turn(api,'ยืนยัน');assert r.status=='recorded' and api.google.sheet_writes==1


@pytest.mark.parametrize('text',['ใช้เดิม','ใช่ครับ','อันนั้น','ไม่ใช่','ไม่เอา Spotify Solo','Spotify Solo หรือ Spotify Family','Spotify Solo และ Spotify Family'])
def test_multiple_real_plans_never_use_a_model_choice_as_owner_identity(text):
    api=ReplyHosted(('Spotify Solo','Spotify Family'));r,_=turn(api,'ค่า Spotify',proposed('Spotify Solo',literal='ค่า Spotify'))
    assert 'Spotify Solo' in r.message and 'Spotify Family' in r.message
    assert 'ชื่อไหน' in r.message and not any(e.get('previous_price') for e in r.data['tool_result']['entries'])
    before=deepcopy(api.drafts());r,_=choose_name(api,'Spotify Solo',text)
    assert r.status=='clarification' and api.drafts()==before and not api.google.sheet_writes


def test_multiple_plans_require_explicit_catalog_name_before_history_lookup():
    api=ReplyHosted(('Spotify Solo','Spotify Family'));turn(api,'ค่า Spotify',proposed('Spotify Solo',literal='ค่า Spotify'))
    r,_=choose_name(api,'Spotify Solo','เลือก Spotify Solo')
    assert r.status=='clarification' and r.data['tool_result']['entries'][0]['previous_price']['amount']=='169.00'
    assert '169.00' in r.message and not api.google.sheet_writes


def test_an_unknown_or_unmatched_name_never_borrows_another_items_price():
    api=ReplyHosted();r,_=turn(api,'ค่า Unknown Tune',proposed('Unknown Tune'))
    assert r.status=='clarification' and not any(e.get('previous_price') for e in r.data['tool_result']['entries'])
    assert '169' not in r.message and not api.google.sheet_writes


@pytest.mark.parametrize('text,day',[('ค่า Spotify 189 บาท','2026-10-18'),('ค่า Spotify 189 บาท วันที่ 15 ต.ค. 2026','2026-10-15')])
def test_explicit_amount_and_transaction_date_win_without_copying_saved_date(text,day):
    api=ReplyHosted();r,_=turn(api,text,proposed(amount=189,date_text='15 ต.ค. 2026' if day.endswith('15') else None))
    assert r.status=='preview';e=r.data['tool_result']['entries'][0]
    assert e['amount']=='189' and e['date']==day and not e.get('previous_price')
    assert not api.google.sheet_writes


def test_selected_price_review_keeps_historical_and_new_dates_in_optional_wording():
    api=ReplyHosted();turn(api,'ค่า Spotify วันที่ 15 ต.ค. 2026',proposed(date_text='15 ต.ค. 2026'))
    r,_=accepted(api)
    assert r.status=='preview' and r.data['tool_result']['entries'][0]['date']=='2026-10-15'
    assert 'เลือกใช้ยอดที่เคยบันทึกวันที่ 2026-10-01' in r.message
    assert 'วันที่รายจ่ายใหม่นี้คือ 2026-10-15' in r.message and not api.google.sheet_writes


def test_read_existing_unresolved_draft_redisplays_catalog_question_without_changing_pending_facts():
    api=ReplyHosted(('Spotify service',));turn(api,'ค่า Spotify',proposed('Spotify service',literal='ค่า Spotify'))
    before=deepcopy(api.drafts());d=next(iter(before.values()))
    r,_=turn(api,'ขอดูร่างค่า Spotify',response('read_expense_drafts',{'draft_ref':d['draft_ref']}))
    assert r.status=='clarification' and 'Spotify service' in r.message and 'เลือกชื่อก่อน' in r.message
    assert api.drafts()==before and not api.google.sheet_writes


def test_wording_cannot_drop_the_catalog_question_fact():
    api=ReplyHosted(('Spotify service',));turn(api,'ค่า Spotify',proposed('Spotify service',literal='ค่า Spotify'))
    body=api.model_bodies[-1];payload=json.loads(body['input'][0]['content'])
    assert 'Spotify service' in payload['facts']['ask0'] and 'จำนวนเงินของรายการ' not in payload['facts']['ask0']
    parts=[{'kind':'fact','value':key} for key in payload['fact_order'] if key!='ask0']
    with pytest.raises(ValueError,match='incomplete_facts'):voice.parse(generated(body,parts),body)


def test_optional_wording_failure_preserves_the_same_price_question():
    api=ReplyHosted();api.voice_failure=True;r,_=turn(api,'ค่า Spotify',proposed())
    assert r.data['response_realization']['status']=='fallback'
    assert '169.00' in r.message and '2026-10-01' in r.message and 'ใช้ยอดเดิม' in r.message
    assert not api.google.sheet_writes


@pytest.mark.parametrize('kind',['catalog','price'])
def test_old_invisible_offer_requires_a_fresh_question_and_preserves_the_pending_draft(monkeypatch,kind):
    api=ReplyHosted(('Spotify service',) if kind=='catalog' else ('Spotify',));original=voice.facts_for
    def old_projection(host,result):
        if result.status=='clarification':return {'status':'ยังขาดจำนวนเงินและชื่อรายการใน Database'}
        return original(host,result)
    monkeypatch.setattr(voice,'facts_for',old_projection)
    if kind=='catalog':turn(api,'ค่า Spotify',proposed('Spotify service',literal='ค่า Spotify'))
    else:turn(api,'ค่า Spotify',proposed())
    monkeypatch.setattr(voice,'facts_for',original);before=deepcopy(api.drafts())
    r,_=choose_name(api,'Spotify service') if kind=='catalog' else accepted(api)
    assert r.status=='clarification' and api.drafts()==before and not api.google.sheet_writes
    d=next(iter(before.values()));r,_=turn(api,'ขอดูร่างค่า Spotify',response('read_expense_drafts',{'draft_ref':d['draft_ref']}))
    assert 'Spotify service' in r.message if kind=='catalog' else '169.00' in r.message and '2026-10-01' in r.message
    r,_=choose_name(api,'Spotify service') if kind=='catalog' else accepted(api)
    assert r.status==('clarification' if kind=='catalog' else 'preview') and not api.google.sheet_writes


def test_visibility_uses_delivered_body_instead_of_alt_text_or_action_metadata():
    from line_assistant.conversation_ui import render_actions
    from line_assistant.line_api import visible_text
    from line_assistant.state import Result
    hidden='Spotify service 169.00 2026-10-01'
    payload=render_actions('ยังขาดข้อมูล', [{'type':'postback','label':'เลือก','data':hidden}])
    payload['altText']=hidden
    result=Result('clarification',hidden,{'line_message':payload})
    assert visible_text(result)=='ยังขาดข้อมูล'
    text='Spotify service\n'+'ข้อมูล '*600+'169.00 2026-10-01'
    assert visible_text(Result('clarification','unused',{'line_message':render_actions(text,[])})).replace('\n','')==text.replace('\n','')
