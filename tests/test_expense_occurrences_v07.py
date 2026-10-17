"""Occurrence/field evidence and consumed controls; synthetic providers only."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json,socket
import pytest
from fastapi.testclient import TestClient
from test_raw_history import Harness,context
from test_history_first import response
from test_production_history import Hosted
from line_assistant.history_context import source_ref,HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.persistence import result_from
from line_assistant.model_api import object_schema

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No live provider access')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def harness(at='2026-10-15T16:24:00+00:00'):
    h=Harness();h.now=datetime.fromisoformat(at);h.sheets.rows.append(('รถตู้','Transportation',''));return h
def entry(amount=30,item='รถตู้',**kw):
    return {'entry_ref':None,'item':item,'amount':amount,'date_text':None,'date_ref':None,'note':None,'source_refs':[],**kw}
def prepare(h,text,entries,refs=None):
    def call(body):
        c=context(body);values=deepcopy(entries)
        for e in values:e['source_refs']=refs or [c['current_source_ref']]
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':values})
    return h.send(text,call,response(text='synthetic clarification'))
def change(h,text,**values):
    d=next(d for d in h.state.drafts.values() if d['status']!='recorded')
    def call(body):
        c=context(body);e=entry(item=None,amount=None,entry_ref=d['entries'][0]['entry_ref'],source_refs=[c['current_source_ref']],**values)
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[e]})
    return h.send(text,call,response(text='synthetic clarification'))

@pytest.mark.parametrize('text,values',[
    ('วันนี้มีค่ารถตู้ 2 รอบ 30 กับ 32',[30,32]),
    ('รถตู้ 30 บาท\nรถตู้ 32 บาท',[30,32]),
    ('รถตู้ 30 กับ 30 บาท',[30,30]),
    ('รถตู้ 2 เที่ยว เที่ยวละ 30 บาท',[30,30]),
    ('รถตู้ สองเที่ยว เที่ยวละ 30 บาท',[30,30]),
    ('รถตู้ ๓๐ กับ ๓๒ บาท',[30,32]),
])
def test_same_canonical_item_has_distinct_verified_occurrences_and_one_batch_replay(text,values):
    h=harness();r=prepare(h,text,[entry(amount=v) for v in values]);assert r.status=='preview',r
    d=next(iter(h.state.drafts.values()));assert [e['item'] for e in d['entries']]==['รถตู้']*len(values)
    assert len({e['entry_ref'] for e in d['entries']})==len(values)
    assert len({e['amount_occurrence']['occurrence_ref'] for e in d['entries']})==len(values)
    event=h.event(r.data['confirm_command']);saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==len(values)
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==len(values)

@pytest.mark.parametrize('text,values',[
    ('รถตู้ 30 บาท\nรถตู้ 32 บาท',[30,30]),
    ('รถตู้ 30 บาท',[30,30]),
    ('รถตู้ 2 รอบ รวม 60 บาท',[60,60]),
    ('รถตู้ 30 แต่แก้เป็น 32 บาท',[30,32]),
    ('รถตู้ 3 รอบ 30 กับ 32',[30,32]),
])
def test_source_amount_cannot_be_reused_or_counts_mistaken_for_money(text,values):
    h=harness();r=prepare(h,text,[entry(amount=v) for v in values])
    assert r.status!='preview' and not h.sheets.entries and not h.state.drafts

@pytest.mark.parametrize('text,item',[('เพิ่มรถตู้อีก 30','รถตู้'),('เพิ่มรถตู้อีก 30','ค่ารถตู้'),('เพิ่มค่ารถตู้ 30','รถตู้'),('ค่ากระดาษอีก 30','ค่ากระดาษ')])
def test_grounded_catalog_names_preserve_real_prefixes_and_accept_add_again(text,item):
    h=harness();r=prepare(h,text,[entry(item=item)])
    assert r.status=='preview' and next(iter(h.state.drafts.values()))['entries'][0]['item']==('ค่ากระดาษ' if item=='ค่ากระดาษ' else 'รถตู้')
    assert not h.sheets.entries

def test_exact_fee_prefixed_catalog_name_is_not_stripped():
    h=harness();h.sheets.rows.append(('ค่ารถตู้','Transportation',''))
    r=prepare(h,'ค่ารถตู้ 30',[entry(item='ค่ารถตู้')]);assert r.status=='preview'
    assert next(iter(h.state.drafts.values()))['entries'][0]['item']=='ค่ารถตู้'

def test_invalid_date_retains_verified_name_amount_and_short_repair_crosses_midnight():
    h=harness();r=prepare(h,'รถตู้ 30 วันนี้',[entry(date_text='เมื่อวาน')])
    assert r.status=='clarification';d=next(iter(h.state.drafts.values()))
    assert d['entries'][0]['item']=='รถตู้' and d['entries'][0]['amount']=='30' and d['entries'][0]['date'] is None and d['review'] is None
    h.now=datetime(2026,10,15,22,54,tzinfo=timezone.utc)
    repaired=change(h,'วันที่ 15',date_text='วันที่ 15')
    assert repaired.status=='preview';e=next(iter(h.state.drafts.values()))['entries'][0]
    assert e['date']=='2026-10-15' and e['amount']=='30' and e['fact_sources']['date']==[source_ref('history-2')]
    assert not h.sheets.entries

def test_explicit_field_sources_span_owner_messages_without_requiring_repeated_name():
    h=harness();h.send('รถตู้ 30 บาท',response(text='synthetic clarification'))
    refs={'item':[source_ref('history-1')],'amount':[source_ref('history-1')],'date':[source_ref('history-2')],'note':[]}
    r=prepare(h,'วันที่ 15',[entry(date_text='วันที่ 15',field_sources=refs)],refs=[source_ref('history-1'),source_ref('history-2')])
    assert r.status=='preview';e=next(iter(h.state.drafts.values()))['entries'][0]
    assert e['date']=='2026-10-15' and e['fact_sources']['amount']==[source_ref('history-1')]

def test_foreign_or_nonvisible_field_sources_never_create_a_draft():
    h=harness();refs={'item':[],'amount':['msg_foreign'],'date':[],'note':[]}
    r=prepare(h,'รถตู้ 30 บาท',[entry(field_sources=refs)])
    assert r.status!='preview' and not h.state.drafts and not h.sheets.entries

def test_new_undated_expense_after_midnight_does_not_copy_previous_day():
    h=harness();r=prepare(h,'รถตู้ 32 วันนี้',[entry(amount=32,date_text='วันนี้')]);assert h.send(r.data['confirm_command']).status=='recorded'
    h.now=datetime(2026,10,15,22,54,tzinfo=timezone.utc)
    later=prepare(h,'เพิ่มรถตู้อีก 30',[entry()]);assert later.status=='preview'
    assert later.data['tool_result']['entries'][0]['date']=='2026-10-16'

def test_cancelled_sources_are_not_resurrected_by_a_short_followup():
    h=harness();r=prepare(h,'รถตู้ 30 วันนี้',[entry(date_text='วันนี้')]);d=next(iter(h.state.drafts.values()))
    cancelled=h.send('ยกเลิกร่าง',response(name='control_expense_draft',args={'operation':'cancel','draft_ref':d['draft_ref'],'draft_version':d['version']}))
    assert cancelled.status=='cancelled' and not h.state.drafts
    r=prepare(h,'เอาเลย',[entry(date_text='วันนี้')],refs=[source_ref('history-1')])
    assert r.status!='preview' and not h.state.drafts and not h.sheets.entries
    fresh=prepare(h,'เพิ่มรถตู้ 30 บาท',[entry()]);assert fresh.status=='preview'

@pytest.mark.parametrize('order',[['save','cancel','save'],['cancel','save','cancel']])
def test_consumed_controls_explain_outcome_without_reversing_or_repeating_writes(order):
    api=Hosted();api.now=datetime(2026,10,15,16,27,tzinfo=timezone.utc)
    for col,v in enumerate(['รถตู้','Transportation',''],1):api.google.cells[('Database',25,col)]=v
    with TestClient(api.app(raw_history_enabled=True)) as client:
        preview=api.create(client,text='รถตู้ 32 บาท',item='รถตู้',amount=32)
        actions=preview.data['line_message']['contents']['footer']['contents'];payloads={'save':actions[0]['action']['data'],'cancel':actions[1]['action']['data']};events=[];results=[]
        for op in order:
            e=api.event(text='synthetic button');e.pop('message');e.update(type='postback',postback={'data':payloads[op]});events.append(e)
            assert api.send(client,e).status_code==200;results.append(result_from(api.snapshot()['receipts'][e['webhookEventId']]['result']))
    with TestClient(api.app(raw_history_enabled=True)) as client:assert api.send(client,deepcopy(events[0])).status_code==200
    assert api.google.sheet_writes==(1 if order[0]=='save' else 0)
    assert all(('บันทึกไปแล้ว' if order[0]=='save' else 'ถูกยกเลิกแล้ว') in r.message for r in results[1:])

def test_occurrence_and_field_proofs_survive_cold_restore():
    h=harness();r=prepare(h,'รถตู้ 30 กับ 32',[entry(amount=30),entry(amount=32)]);assert r.status=='preview'
    before=history_snapshot(h.state);restored=HistoryState();restored.clock=lambda:h.now;restored.enable_raw_history(journal=deepcopy(h.state.raw_journal));restore_history(restored,before,'mock-owner')
    assert restored.drafts==h.state.drafts

@pytest.mark.parametrize('answer',['15','วันที่ 15','ลงวันที่ 15','ลงันที่ 15'])
def test_bound_missing_date_question_accepts_day_answers_with_small_cue_typos(answer):
    h=harness();prepare(h,'รถตู้ 30 วันนี้',[entry(date_text='เมื่อวาน')])
    h.now=datetime(2026,10,15,22,54,tzinfo=timezone.utc)
    r=change(h,answer,date_text=answer);assert r.status=='preview'
    assert next(iter(h.state.drafts.values()))['entries'][0]['date']=='2026-10-15'

@pytest.mark.parametrize('answer',['ยอด 15 บาท','กล้วย 15','15 หรือ 16'])
def test_bound_date_question_does_not_turn_unrelated_or_ambiguous_numbers_into_a_date(answer):
    h=harness();prepare(h,'รถตู้ 30 วันนี้',[entry(date_text='เมื่อวาน')])
    r=change(h,answer,date_text=answer);assert r.status!='preview' and not h.sheets.entries

def test_explicit_field_refs_do_not_override_a_new_expenses_current_amount_or_date():
    for field in ['amount','date']:
        h=harness('2026-10-15T22:54:00+00:00');h.send('รถตู้ 100 บาท วันที่ 14 ตค',response(text='synthetic history'))
        refs={'item':[],'amount':[source_ref('history-1')] if field=='amount' else [],'date':[source_ref('history-1')] if field=='date' else [],'note':[]}
        r=prepare(h,'เพิ่มรถตู้ 30 บาท',[entry(amount=100 if field=='amount' else 30,date_text='วันที่ 14 ตค' if field=='date' else None,field_sources=refs)],refs=[source_ref('history-1'),source_ref('history-2')])
        assert r.status!='preview' and not h.sheets.entries

def test_named_occurrence_target_allows_only_the_referenced_row_to_change():
    h=harness();assert prepare(h,'รถตู้ 30 กับ 32',[entry(amount=30),entry(amount=32)]).status=='preview'
    d=next(iter(h.state.drafts.values()));before=deepcopy(d['entries'])
    def change_second(body):
        c=context(body)
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[entry(item=None,amount=35,entry_ref=before[1]['entry_ref'],source_refs=[c['current_source_ref']])]})
    r=h.send('แก้รอบที่สองเป็น 35 บาท',change_second,response(text='synthetic clarification'))
    assert r.status=='preview';rows=next(iter(h.state.drafts.values()))['entries'];assert rows[0]==before[0] and rows[1]['amount']=='35'

def test_a_shared_item_label_is_not_an_owner_selection_of_one_occurrence():
    h=harness();prepare(h,'รถตู้ 30 กับ 32',[entry(amount=30),entry(amount=32)]);d=next(iter(h.state.drafts.values()));before=deepcopy(d['entries'])
    def wrong_target(body):
        c=context(body)
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[entry(item=None,amount=35,entry_ref=before[1]['entry_ref'],source_refs=[c['current_source_ref']])]})
    r=h.send('แก้รถตู้เป็น 35 บาท',wrong_target,response(text='synthetic clarification'))
    assert r.status!='preview' and next(iter(h.state.drafts.values()))['entries']==before

@pytest.mark.parametrize('damage',['reused','foreign_fact_ref','occurrence_digest'])
def test_persisted_field_or_occurrence_misattribution_is_rejected_before_restore(damage):
    from line_assistant.history_context import draft_fingerprint
    h=harness();prepare(h,'รถตู้ 30 กับ 32',[entry(amount=30),entry(amount=32)]);snap=history_snapshot(h.state);d=next(iter(snap['drafts'].values()))
    if damage=='reused':d['entries'][1]['amount_occurrence']=deepcopy(d['entries'][0]['amount_occurrence'])
    if damage=='foreign_fact_ref':d['entries'][0]['fact_sources']['amount']=['msg_foreign']
    if damage=='occurrence_digest':d['entries'][0]['amount_occurrence']['span']=[0,1]
    snap['draft_proofs'][d['draft_ref']]['fingerprint']=draft_fingerprint(d)
    restored=HistoryState();restored.clock=lambda:h.now;restored.enable_raw_history(journal=deepcopy(h.state.raw_journal))
    with pytest.raises(ValueError):restore_history(restored,snap,'mock-owner')

@pytest.mark.parametrize('values',[[35,36],[36,35],[35,35]])
@pytest.mark.parametrize('text',['แก้รอบหนึ่งรถตู้ 35 บาท และรอบสองรถตู้ 36 บาท','รอบแรก 35 บาท รอบสอง 36 บาท'])
@pytest.mark.parametrize('field_refs',[False,True])
def test_bulk_occurrence_edits_bind_each_amount_to_its_owner_selected_row(values,text,field_refs):
    h=harness();prepare(h,'รถตู้ 30 กับ 32',[entry(amount=30),entry(amount=32)]);d=next(iter(h.state.drafts.values()));before=deepcopy(d['entries'])
    def update(body):
        c=context(body)
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
            entry(item=None,amount=amount,entry_ref=before[i]['entry_ref'],source_refs=[c['current_source_ref']],
                  **({'field_sources':{'item':[],'amount':[c['current_source_ref']],'date':[],'note':[]}} if field_refs else {})) for i,amount in enumerate(values)]})
    r=h.send(text,update,response(text='synthetic clarification'))
    if values==[35,36]:assert r.status=='preview' and [e['amount'] for e in next(iter(h.state.drafts.values()))['entries']]==['35','36']
    else:assert r.status!='preview' and next(iter(h.state.drafts.values()))['entries']==before


def test_receipt_aware_response_does_not_disclose_a_completed_action_to_forged_controls():
    api=Hosted()
    with TestClient(api.app(raw_history_enabled=True)) as client:
        preview=api.create(client);payload=preview.data['line_message']['contents']['footer']['contents'][0]['action']['data']
        def send(payload):
            e=api.event(text='synthetic button');e.pop('message');e.update(type='postback',postback={'data':payload});api.send(client,e);return result_from(api.snapshot()['receipts'][e['webhookEventId']]['result'])
        assert send(payload).status=='recorded'
        parts=payload.split('|');parts[3]='0'*16
        assert 'บันทึกไปแล้ว' not in send('|'.join(parts)).message
        parts=payload.split('|');parts[2]=str(int(parts[2])+1)
        assert 'บันทึกไปแล้ว' not in send('|'.join(parts)).message
        assert api.google.sheet_writes==1

def test_a_model_cannot_borrow_yesterdays_date_reference_for_a_new_undated_expense():
    h=harness();r=prepare(h,'รถตู้ 32 วันนี้',[entry(amount=32,date_text='วันนี้')]);h.send(r.data['confirm_command'])
    h.now=datetime(2026,10,15,22,54,tzinfo=timezone.utc)
    def call(body):
        c=context(body)
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[entry(date_ref=c['date_references'][0]['date_ref'],source_refs=[c['current_source_ref']])]})
    result=h.send('เพิ่มรถตู้อีก 30',call,response(text='synthetic clarification'))
    assert result.status!='preview' and len(h.sheets.entries)==1

def test_reordered_cancel_then_earlier_save_has_no_financial_effect():
    api=Hosted()
    with TestClient(api.app(raw_history_enabled=True)) as client:
        preview=api.create(client);buttons=preview.data['line_message']['contents']['footer']['contents']
        events=[]
        for button in buttons:
            e=api.event(text='synthetic button');e.pop('message');e.update(type='postback',postback={'data':button['action']['data']});events.append(e)
        for e in [events[1],events[0],deepcopy(events[1])]:assert api.send(client,e).status_code==200
        assert api.google.sheet_writes==0

def test_new_raw_occurrences_can_be_saved_by_model_disabled_bridge_without_calls():
    api=Hosted()
    for col,v in enumerate(['รถตู้','Transportation',''],1):api.google.cells[('Database',25,col)]=v
    def proposed(body):
        c=context(body)
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[entry(amount=n,source_refs=[c['current_source_ref']]) for n in [30,32]]})
    with TestClient(api.app(raw_history_enabled=True)) as client:
        preview,_=api.send_text(client,'รถตู้ 30 กับ 32',proposed)
    calls=api.models;payload=preview.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    e=api.event(text='synthetic save');e.pop('message');e.update(type='postback',postback={'data':payload})
    with TestClient(api.app(enabled=False,raw_history_enabled=True)) as client:assert api.send(client,e).status_code==200
    with TestClient(api.app(enabled=False,raw_history_enabled=True)) as client:assert api.send(client,deepcopy(e)).status_code==200
    assert api.models==calls and api.google.sheet_writes==1
    rows=next(iter(api.snapshot()['history24']['current']['drafts'].values()))['entries']
    assert [v['amount'] for v in rows]==['30','32'] and len({v['amount_occurrence']['occurrence_ref'] for v in rows})==2

@pytest.mark.parametrize('text,item',[('กาแฟ 30 บาท','รถตู้'),('ค่ากระดาษ 30 บาท','ยา')])
def test_prior_other_item_or_a_substring_label_cannot_replace_the_current_expense(text,item):
    h=harness();h.sheets.rows.append(('ยา','Work/Office',''));h.send(item+' 30 บาท',response(text='synthetic unrelated history'))
    refs={'item':[source_ref('history-1')],'amount':[source_ref('history-1')],'date':[],'note':[]}
    r=prepare(h,text,[entry(item=item,field_sources=refs)],refs=[source_ref('history-1'),source_ref('history-2')])
    assert r.status!='preview' and not h.state.drafts and not h.sheets.entries
