"""Semantic dates and repair replies share their existing grounded validators."""
from copy import deepcopy
from datetime import timedelta
import json
import socket

import pytest
from fastapi.testclient import TestClient

from state_records_hosted import RecordsHosted
from test_expense_proposals import harness, proposal, group, record, call, send
from test_response_realization import generated
from test_history_first import response
from line_assistant.history_persistence import history_snapshot, restore_history


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


@pytest.mark.parametrize('word,day',[('สองวันก่อน','2026-10-14'),('เมื่อวันจันทร์ที่ผ่านมา','2026-10-12'),('ต้นเดือนนี้วันที่หนึ่ง','2026-10-01')])
def test_interpreted_literal_date_is_checked_in_the_same_prepare_call(word,day,monkeypatch):
    h=harness(('Metro',))
    def deny(*a,**k):raise AssertionError('No duplicate date grammar on a semantic proposal')
    monkeypatch.setattr('line_assistant.history_dates.resolve',deny)
    args=proposal([group('g','Metro','Metro 61')],[record('g',61,date=word,date_value=day)])
    result=send(h,word+' Metro 61',args)
    assert result.status=='preview',result
    e=h.draft()['entries'][0]
    assert e['date']==day and e['date_basis']['policy']=='semantic-temporal-v1'
    assert e['date_basis']['temporal']['source_text']==word and len(h.port.requests)==1
    before=deepcopy(h.draft());restore_history(h.state,history_snapshot(h.state),h.service.settings.owner_id)
    assert h.draft()==before and not h.sheets.entries
    confirmed=h.service.handle(h.event(result.data['confirm_command']))
    assert confirmed.status=='recorded' and len(h.sheets.entries)==1
    assert h.sheets.entries[0].date.isoformat()==day


@pytest.mark.parametrize('damage',['missing_source','invented_quote','invalid_day','future','conflicting_ref'])
def test_invalid_inline_date_never_previews_or_writes(damage):
    h=harness(('Metro',));r=record('g',61,date='สองวันก่อน',date_value='2026-10-14')
    if damage=='missing_source':r['date_source']=None
    if damage=='invented_quote':r['date_source']['text']='คำที่ไม่มีจริง'
    if damage=='invalid_day':r['date_value']='2026-02-30'
    if damage=='future':r['date_value']='2026-11-01'
    if damage=='conflicting_ref':r['date_ref']='date_forged'
    result=send(h,'สองวันก่อน Metro 61',proposal([group('g','Metro','Metro 61')],[r]))
    assert result.status!='preview' and not h.sheets.entries
    assert not any(d.get('review') for d in h.state.drafts.values())


def test_relative_inline_date_uses_original_message_evidence_across_midnight():
    h=harness(('Metro',));h.send('สองวันก่อน Metro 61',response(text='synthetic context'))
    ref=h.service._current['source_ref'];h.now+=timedelta(hours=20)
    args=proposal([group('g','Metro','Metro 61',source=ref)],[record('g',61,source=ref,date='สองวันก่อน',date_value='2026-10-14')])
    result=send(h,'ลงรายการข้างบนให้หน่อย',args)
    assert result.status=='preview' and h.draft()['entries'][0]['date']=='2026-10-14'
    assert h.draft()['entries'][0]['date_basis']['temporal']['source_ref']==ref


def bad_date():
    return proposal([group('g','กาแฟ','กาแฟ 61')],[record('g',61,date='วันนั้น')])


def finish_reply(*,damage=None,text_exit=False):
    def decide(body):
        feedback=next(json.loads(v['output'])['repair_feedback'] for v in reversed(body['input'])
            if v.get('type')=='function_call_output' and 'final_reply' in json.loads(v['output']).get('repair_feedback',{}))
        view=feedback['final_reply'];payload=view['reply_context']
        fake={'input':[{'content':json.dumps(payload,ensure_ascii=False)}]}
        value=generated(fake);document=json.loads(value['output'][0]['content'][0]['text'])
        if damage=='missing_fact':document['parts']=[p for p in document['parts'] if p!={'kind':'fact','value':payload['fact_order'][-1]}]
        if damage=='success':document={'opening':'บันทึกแล้วครับ','parts':document['parts']}
        args={'response_ref':view['response_ref'],'document':document}
        if damage=='foreign_ref':args['response_ref']='reply_foreign'
        if text_exit:return response(text=json.dumps(document,ensure_ascii=False))
        value=response(name='finish_preparation_reply',args=args)
        value['output'][0]['call_id']='finish_reply_'+str(len(body['input']))
        return value
    return decide


@pytest.mark.parametrize('text_exit',[False,True])
def test_repair_planner_finishes_a_checked_reply_without_third_model_request(text_exit):
    api=RecordsHosted()
    with TestClient(api.app()) as client:
        result,event=api.turn(client,'วันนั้น กาแฟ 61',call(bad_date()),finish_reply(text_exit=text_exit))
    assert result.status=='clarification',result
    assert result.data['response_realization']['status']=='planner'
    assert len(api.model_bodies)==2 and not api.google.sheet_writes
    assert api.snapshot()['replies'][event['webhookEventId']]['status']=='sent'
    assert '61' in result.message and 'กาแฟ' in result.message
    count=len(api.google.line_replies)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert len(api.model_bodies)==2 and len(api.google.line_replies)==count


@pytest.mark.parametrize('damage',['missing_fact','success','foreign_ref'])
def test_bad_repair_exit_is_rejected_then_the_same_planner_can_correct_it(damage):
    api=RecordsHosted()
    with TestClient(api.app()) as client:
        result,_=api.turn(client,'วันนั้น กาแฟ 61',call(bad_date()),finish_reply(damage=damage),finish_reply())
    assert result.status=='clarification' and result.data['response_realization']['status']=='planner'
    assert len(api.model_bodies)==3 and not api.google.sheet_writes


def test_unstructured_repair_answer_uses_core_fallback_without_an_extra_composer():
    api=RecordsHosted()
    with TestClient(api.app()) as client:
        result,_=api.turn(client,'วันนั้น กาแฟ 61',call(bad_date()),response(text='บันทึกแล้ว 999 บาท'))
    assert result.status=='clarification' and '999' not in result.message and 'บันทึกแล้ว' not in result.message
    assert result.data['response_realization']['reason']=='planner_reply_invalid'
    assert len(api.model_bodies)==2 and not api.google.sheet_writes


def test_successful_repair_revokes_the_old_reply_exit_contract():
    api=RecordsHosted()
    def repair(body):
        args=proposal([group('g','กาแฟ','กาแฟ 61')],[record('g',61,date='สองวันก่อน',date_value='2026-10-21')])
        return call(args)(body)
    with TestClient(api.app()) as client:
        bad=proposal([group('g','กาแฟ','กาแฟ 61')],[record('g',61,date='สองวันก่อน')])
        result,_=api.turn(client,'สองวันก่อน กาแฟ 61',call(bad),repair)
    assert result.status=='preview' and len(api.model_bodies)==3
    assert result.data['response_realization']['status']=='llm' and not api.google.sheet_writes


def test_genuine_date_ambiguity_keeps_known_facts_without_a_planning_repair():
    api=RecordsHosted()
    args=proposal([group('g','กาแฟ','กาแฟ 61')],[record('g',61,date='วันนั้น',date_ambiguous=True)])
    with TestClient(api.app()) as client:result,_=api.turn(client,'วันนั้น กาแฟ 61',call(args))
    assert result.status=='clarification' and len(api.model_bodies)==2
    assert not any(v.get('tools') and 'finish_preparation_reply' in {t['name'] for t in v['tools']} for v in api.model_bodies)
    e=result.data['tool_result']['entries'][0]
    assert e['amount']=='61' and e['date'] is None and e['date_issue']=='date_clarification_required'
    assert not api.google.sheet_writes


def test_ambiguous_date_can_be_completed_after_cold_restore_without_losing_facts():
    from test_pending_records import patch
    api=RecordsHosted()
    args=proposal([group('g','กาแฟ','กาแฟ 61')],[record('g',61,date='วันนั้น',date_ambiguous=True)])
    with TestClient(api.app()) as client:result,_=api.turn(client,'วันนั้น กาแฟ 61',call(args))
    assert result.status=='clarification'
    with TestClient(api.app()) as client:
        preview,event=api.turn(client,'เมื่อวาน',patch({0:[('date_text','เมื่อวาน','เมื่อวาน')]}))
    assert preview.status=='preview' and not api.google.sheet_writes
    e=preview.data['tool_result']['entries'][0]
    assert (e['item'],e['amount'],e['date'])==('กาแฟ','61','2026-10-22')
    calls=len(api.model_bodies)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert not api.google.sheet_writes and len(api.model_bodies)==calls
