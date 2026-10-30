"""Compact reviews and semantic repeat requests over synthetic providers only."""
from copy import deepcopy
from datetime import datetime
import json,socket

import pytest
from fastapi.testclient import TestClient

from expense_storage_hosted import StorageHosted
from test_previous_expense_prices import turn
from test_expense_proposals import group,record,proposal,call
from test_pending_records import patch
from test_read_queries import plan
from line_assistant import operation_composition as operation
from line_assistant.answer_composition import AnswerError,View
from line_assistant.line_api import rendered_message
from line_assistant.previous_prices import REPEAT_FORMAT
from line_assistant.state import Result


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No network or credentials in experience regressions')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def setup(backend='sheets',names=('BTS','MRT'),day='2026-10-30',*,records=False):
    from state_records_hosted import RecordsProtocol
    class External(RecordsProtocol,StorageHosted):pass
    api=(External if records else StorageHosted)(backend);api.voice=True;api.now=datetime.fromisoformat('2026-10-30T08:00:00+07:00')
    for row,(item,amount) in enumerate(zip(names,[30,25]),20):
        for col,value in enumerate([item,'Transportation',''],1):api.google.cells[('Database',row,col)]=value
        api.saved(row,day,item,amount)
    if backend=='firestore':api.seed_business_fixture()
    return api


def repeat(names=('BTS','MRT'),**kw):
    return proposal([group(str(i),name,name) for i,name in enumerate(names)],
        [record(str(i),None,reuse_previous_amount=True,**kw) for i in range(len(names))])


@pytest.mark.parametrize('backend',['sheets','firestore'])
@pytest.mark.parametrize('names',[('BTS','MRT'),('Metro Test','Shuttle Test')])
def test_repeat_uses_same_day_saved_amounts_without_original_owner_amounts_in_history(backend,names):
    api=setup(backend,names)
    result,_=turn(api,'ขากลับก็ '+' '.join(names)+' เหมือนเดิมเลย',call(repeat(names)))
    assert result.status=='preview',result
    rows=result.data['tool_result']['entries']
    assert [(r['item'],r['amount'],r['date']) for r in rows]==[(names[0],'30','2026-10-30'),(names[1],'25','2026-10-30')]
    assert all(r['previous_price']['format']==REPEAT_FORMAT and r['previous_price']['source_date']==r['date'] for r in rows)
    assert not api.expense_writes
    saved,event=turn(api,'บันทึก');assert saved.status=='recorded' and api.expense_writes==1
    assert 'record_actions' not in saved.data and rendered_message(saved)['type']=='text'
    calls=len(api.model_bodies)
    with TestClient(api.app()) as c:assert api.send(c,deepcopy(event)).status_code==200
    assert api.expense_writes==1 and len(api.model_bodies)==calls


def test_repeat_preserves_the_day_of_the_previous_query_instead_of_todays_day():
    api=setup(day='2026-10-29')
    api.saved(22,'2026-10-30','BTS',99)
    turn(api,'เมื่อวานมีรายจ่ายอะไรบ้าง',plan('expense','2026-10-29'))
    def choose(body,args):
        context=json.loads(body['input'][0]['content'])
        target=next(v['date_ref'] for v in context['date_references'] if v['date']=='2026-10-29')
        for row in args['entries']:row['date_ref']=target
    result,_=turn(api,'ขากลับก็ BTS MRT เหมือนเดิมเลย',call(repeat(),inspect=choose))
    assert result.status=='preview',result
    assert [(r['amount'],r['date']) for r in result.data['tool_result']['entries']]==[('30','2026-10-29'),('25','2026-10-29')]


@pytest.mark.parametrize('kind',['ambiguous','annotated','future_only','disabled'])
def test_repeat_never_guesses_an_ambiguous_unavailable_or_future_amount(kind):
    api=setup()
    if kind=='ambiguous':api.saved(22,'2026-10-30','BTS',45)
    if kind=='annotated':api.google.cells[('All Expenses',20,4)]='combined fare for several riders'
    if kind=='future_only':api.saved(20,'2026-10-31','BTS',30)
    if kind=='disabled':api.prices=False
    result,_=turn(api,'BTS MRT เหมือนเดิม',call(repeat()))
    assert result.status=='clarification',result
    row=result.data['tool_result']['entries'][0]
    assert row['amount'] is None and not row.get('previous_price')
    assert not any(d.get('review') for d in api.drafts().values()) and not api.expense_writes


def test_saved_price_change_after_review_requires_new_review_not_a_silent_new_amount():
    api=setup();review,_=turn(api,'BTS MRT ยอดเดิม',call(repeat()))
    assert review.status=='preview'
    api.google.cells[('All Expenses',20,3)]=99
    result,_=turn(api,'บันทึก')
    assert result.status!='recorded' and not api.expense_writes


@pytest.mark.parametrize('backend',['sheets','firestore'])
def test_repeat_proof_survives_external_evidence_storage_and_cold_confirmation(backend):
    api=setup(backend,records=True)
    review,_=turn(api,'BTS MRT เหมือนเดิม',call(repeat()))
    assert review.status=='preview'
    assert api.stored_snapshot()['state_records_v1']['phase']=='ready'
    result,event=turn(api,'บันทึก')
    assert result.status=='recorded' and api.expense_writes==1
    with TestClient(api.app()) as c:assert api.send(c,deepcopy(event)).status_code==200
    assert api.expense_writes==1


def test_repeat_request_retains_known_names_until_owner_resolves_the_day():
    api=setup(day='2026-10-29')
    args=repeat(date='วันนั้น',date_ambiguous=True)
    result,_=turn(api,'วันนั้น BTS MRT เหมือนเดิม',call(args))
    assert result.status=='clarification'
    result,_=turn(api,'เมื่อวาน',patch({0:[('date_text','เมื่อวาน','เมื่อวาน')],1:[('date_text','เมื่อวาน','เมื่อวาน')]}))
    assert result.status=='preview',result
    assert [(r['amount'],r['date']) for r in result.data['tool_result']['entries']]==[('30','2026-10-29'),('25','2026-10-29')]


def review_result():
    entries=[{'entry_ref':'a','item':'MRT','amount':'22','date':'2026-10-29'},
             {'entry_ref':'b','item':'BTS','amount':'59','date':'2026-10-29','note':'เที่ยวเย็น'},
             {'entry_ref':'c','item':'รถตู้','amount':'30','date':'2026-10-29'},
             {'entry_ref':'d','item':'วินมอไซ','amount':'29','date':'2026-10-29'},
             {'entry_ref':'e','item':'กาแฟ','amount':'61','date':'2026-10-30'}]
    matches=[{'entry_ref':r['entry_ref'],'item':r['item'],'date':r['date'],
              'same_item':1,'same_amount':int(r['entry_ref']!='c'),'saved_amounts':['32' if r['entry_ref']=='c' else r['amount']]} for r in entries[:3]]
    return Result('preview','old copy',{'confirm_command':'not-displayed','tool_result':{'ok':True,'status':'review','entries':entries},
        'review_matches':matches})


def test_review_groups_days_and_shows_only_proposed_payments_without_saved_history():
    view=operation.for_result(review_result());text=operation.fallback(view)
    assert text.count('29 ต.ค. 2026')==text.count('30 ต.ค. 2026')==1
    assert 'MRT 22 บาท' in text and 'รถตู้ 30 บาท' in text
    assert '32 บาท' not in text and 'บันทึกไว้แล้ว' not in text
    assert text.index('29 ต.ค. 2026')<text.index('วินมอไซ 29 บาท')<text.index('30 ต.ค. 2026')<text.index('กาแฟ 61 บาท')
    assert 'เที่ยวเย็น' in text and 'Legacy duplicate paragraph' not in text and '—' not in text
    assert '\n\n29 ต.ค.' in text and '\n\n30 ต.ค.' in text


def test_uniform_optional_columns_allow_a_shared_template_without_inventing_notes():
    view=operation.for_result(review_result())
    assert all(list(r['fields'])==list(view['records'][0]['fields']) for r in view['records'])
    blocks=[{'kind':'text','template':'{{f1}}'},
        {'kind':'rows','record_refs':['r1','r2','r3','r4'],'heading':'{{c1}}','template':'{{c2}} {{c3}}\n  {{c4}}'},
        {'kind':'rows','record_refs':['r5'],'heading':'{{c1}}','template':'{{c2}} {{c3}}'},
        {'kind':'text','template':'{{f2}}'}]
    text=operation.render({'blocks':blocks},view)
    assert 'เที่ยวเย็น' in text and text.count('29 ต.ค. 2026')==1
    assert 'c4' not in text and 'c5' not in text and 'ว่าง' not in text
    nonempty=deepcopy(view);nonempty['records'][1]['required_fields'].remove('c4')
    with pytest.raises(AnswerError):operation.render({'blocks':blocks},nonempty)


@pytest.mark.parametrize('backend',['sheets','firestore'])
def test_repeat_feature_off_restores_proof_but_cannot_confirm_an_unattempted_repeat(backend):
    api=setup(backend,records=True)
    review,_=turn(api,'BTS MRT เหมือนเดิม',call(repeat()))
    assert review.status=='preview'
    api.prices=False
    result,_=turn(api,'บันทึก')
    assert result.status=='stale' and not api.expense_writes


@pytest.mark.parametrize('committed',[False,True])
def test_repeat_unknown_effect_with_feature_off_reconciles_without_another_append(committed):
    api=setup(records=True)
    review,_=turn(api,'BTS MRT เหมือนเดิม',call(repeat()))
    assert review.status=='preview'
    api.price_lost_ack=committed;api.price_before_ack=not committed
    with TestClient(api.app()) as c:
        event=api.event('บันทึก');assert api.send(c,event).status_code==503
    writes=api.expense_writes;api.price_lost_ack=api.price_before_ack=False;api.prices=False
    with TestClient(api.app()) as c:assert api.send(c,deepcopy(event)).status_code==200
    assert api.expense_writes==writes
    actions=api.snapshot()['actions'].values()
    assert all(a['status']=='completed' for a in actions) if committed else any(a['status']=='uncertain' for a in actions)


@pytest.mark.parametrize('break_scope',['different_day','intervening_text'])
def test_a_heading_cannot_hide_another_days_date_or_cross_a_prose_block(break_scope):
    v=View();v.row([('วันที่','2026-10-29','date',None),('รายการ','One',None,None)])
    v.row([('วันที่','2026-10-30' if break_scope=='different_day' else '2026-10-29','date',None),('รายการ','Two',None,None)])
    blocks=[{'kind':'rows','record_refs':['r1'],'heading':'{{c1}}','template':'{{c2}}'}]
    if break_scope=='intervening_text':blocks.append({'kind':'text','template':'รายละเอียดต่อไป'})
    blocks.append({'kind':'rows','record_refs':['r2'],'heading':'','template':'{{c2}}'})
    with pytest.raises(AnswerError):operation.render({'blocks':blocks},v.data())


def test_em_dash_is_removed_only_from_generated_separators_not_owner_values():
    v=View();v.row([('ชื่อ','ชื่อ—ตามต้นทาง',None,None),('หมายเหตุ','ข้อความ—ต้นฉบับ',None,None)])
    doc={'blocks':[{'kind':'rows','record_refs':['r1'],'heading':'','template':'{{c1}} — {{c2}}'}]}
    assert operation.render(doc,v.data())=='ชื่อ—ตามต้นทาง ข้อความ—ต้นฉบับ'
