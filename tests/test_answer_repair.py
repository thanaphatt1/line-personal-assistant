"""Validator feedback repairs presentation over unchanged facts, never effects."""
from copy import deepcopy
from datetime import timedelta
from threading import Event, enumerate as threads
from time import monotonic
from types import SimpleNamespace
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from line_assistant import answer_composition as composition, response_realization as voice
from line_assistant.model_api import DurableModelGuard
from line_assistant.persistence import state_snapshot, restore_state
from line_assistant.raw_history import reservation
from line_assistant.state import Claim, MemoryState
from line_assistant.read_queries import result as read_result
from test_response_realization import VoiceHosted, generated
from test_calendar_text_confirmation_v13 import turn
from test_calendar_batch_factory_v13 import text, proposed
from test_calendar_mvp import NOW
from test_expense_query_operations import seed
from test_read_queries import plan


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k): raise AssertionError('Synthetic answer repair only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def doc(*blocks): return {'blocks': list(blocks)}
def paragraph(template): return {'kind': 'text', 'template': template}
def rows(refs=('r1','r2'), template='{{c2}} {{c3}}'): return {
    'kind': 'rows', 'record_refs': list(refs), 'heading': '', 'template': template}
GOOD=doc(paragraph('{{f1}} ใช้ไป {{f2}} จาก {{f3}}ครับ'), rows())


@pytest.mark.parametrize('bad, code, details', [
    (doc(paragraph('เมื่อวาน (15 ต.ค. 2026) ใช้ไป {{f2}} จาก {{f3}}ครับ'), rows()),
     'unsupported_answer_literal', {}),
    (doc(paragraph('{{f1}} จาก {{f3}}ครับ'), rows()),
     'answer_fact_coverage', {'missing_facts':['f2']}),
    (doc(paragraph('{{f1}} {{f2}} {{f3}}'), rows(template='{{c2}}')),
     'answer_cell_coverage', {'missing_cells':[{'record_ref':'r1','field':'c3'},{'record_ref':'r2','field':'c3'}]}),
    (doc(paragraph('{{f1}} {{f2}} {{f3}}'), rows(('r2','r1'))),
     'answer_record_coverage_or_order', {'expected_record_order':['r1','r2'],'received_record_order':['r2','r1']}),
    (doc(paragraph('{{invented}} {{f1}} {{f2}} {{f3}}'), rows()),
     'unknown_answer_fact', {}),
    (doc(paragraph('REJECTED_SECRET_SENTINEL บันทึกสำเร็จแล้ว 999 บาท {{f1}} {{f2}} {{f3}}'), rows()),
     'unsupported_answer_literal', {}),
])
def test_received_failure_gets_precise_feedback_then_one_corrected_reply_and_exact_cold_replay(bad, code, details, capsys):
    api=VoiceHosted();seed(api,[('2026-10-15','ชา',20),('2026-10-15','ขนม',30)])
    original_cells=deepcopy(api.google.cells)
    def compose(body):
        assert voice.validate_body(body)
        payload=json.loads(body['input'][0]['content'])
        if 'answer_repair' not in payload:return generated(body,bad)
        previous=json.loads(api.voice_bodies[0]['input'][0]['content'])
        feedback=payload.pop('answer_repair')
        assert payload==previous and body['tools']==[] and body['tool_choice']=='none'
        assert feedback['validation_code']==code and feedback['details']==details
        assert feedback['previous_document']==bad and feedback['round']==1
        return generated(body,GOOD)
    api.voice=compose
    result,event=turn(api,'เมื่อวานจ่ายอะไรไปบ้าง',plan(start='2026-10-15',listing=True))
    meta=result.data['response_realization']
    assert meta['status']=='llm' and meta['repair_count']==1 and meta['repair_from']==code
    assert result.message=='เมื่อวาน (15 ต.ค. 2026) ใช้ไป 50.00 บาท จาก 2 รายการครับ\nขนม 30.00 บาท\nชา 20.00 บาท'
    assert len(api.voice_bodies)==2 and len(api.model_bodies)==3
    assert api.google.cells==original_cells and not api.google.sheet_writes and not api.calendar.posts
    snapshot=api.snapshot();assert not snapshot['actions']
    assert sum(b['calls'] for b in snapshot['model_budgets'].values())==3
    reply_runs=[r for k,r in snapshot['model_runs'].items() if k.startswith('realize01_')]
    assert len(reply_runs)==2 and all(r['status']=='completed' and r['decision'] is None for r in reply_runs)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert len(api.model_bodies)==3 and not api.google.sheet_writes
    trace=capsys.readouterr().out
    assert 'reply_validation' in trace and 'REJECTED_SECRET_SENTINEL' not in trace


def test_successful_first_document_does_not_reserve_or_call_a_repair():
    api=VoiceHosted();turn(api,'ยอดวันนี้',plan())
    assert len(api.voice_bodies)==1 and len(api.model_bodies)==2
    assert all('answer_repair' not in json.loads(b['input'][0]['content']) for b in api.voice_bodies)


@pytest.mark.parametrize('failure', ['transport','refusal','usage','incomplete','extra_tool','json'])
def test_unknown_or_invalid_provider_envelope_never_starts_a_repair(failure):
    api=VoiceHosted()
    def failed(body):
        if failure=='transport':raise httpx.ReadTimeout('synthetic unknown result')
        value=generated(body)
        if failure=='refusal':value['output'][0]['content']=[{'type':'refusal','refusal':'synthetic refusal'}]
        elif failure=='usage':value['usage']['input_tokens']=999999
        elif failure=='incomplete':value['status']='incomplete'
        elif failure=='extra_tool':value['output'].append({'type':'function_call','name':'save','arguments':'{}'})
        elif failure=='json':value['output'][0]['content'][0]['text']='{broken'
        return value
    api.voice=failed
    result,event=turn(api,'ยอดวันนี้',plan())
    assert result.data['response_realization']['status']=='fallback' and len(api.voice_bodies)==1
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert len(api.voice_bodies)==1 and not api.google.sheet_writes and not api.calendar.posts


def test_repair_budget_denial_retains_first_reservation_and_does_not_repeat_the_query():
    api=VoiceHosted();app=api.app()
    def invalid(body):
        app.state.assistant.guard.limit=0
        return generated(body,doc(paragraph('999 บาท')))
    api.voice=invalid
    with TestClient(app) as client:result,_=api.turn(client,'ยอดวันนี้',plan())
    meta=result.data['response_realization']
    assert meta['reason']=='quota' and meta['repair_count']==0 and len(api.voice_bodies)==1
    assert sum(b['calls'] for b in api.snapshot()['model_budgets'].values())==2


def test_repair_shares_the_original_foreground_deadline(monkeypatch):
    from line_assistant import pilot_http
    first_done=False
    def remaining():return 2.01 if first_done else None
    monkeypatch.setattr(pilot_http,'remaining_rpc_seconds',remaining)
    api=VoiceHosted()
    def invalid(body):
        nonlocal first_done
        first_done=True
        return generated(body,doc(paragraph('999 บาท')))
    api.voice=invalid
    result,_=turn(api,'ยอดวันนี้',plan())
    assert result.data['response_realization']['reason']=='deadline' and len(api.voice_bodies)==1
    assert result.data['response_realization']['repair_count']==0


def test_late_repair_never_blocks_past_shared_deadline_or_repeats_on_redelivery(monkeypatch):
    monkeypatch.setattr(composition,'TIMEOUT_SECONDS',0.15)
    release=Event();api=VoiceHosted()
    def slow(body):
        if len(api.voice_bodies)==1:return generated(body,doc(paragraph('999 บาท')))
        release.wait(2);return generated(body)
    api.voice=slow
    try:
        started=monotonic();result,event=turn(api,'ยอดวันนี้',plan())
        assert monotonic()-started<1 and result.data['response_realization']['reason']=='timeout'
        assert len(api.voice_bodies)==2
    finally:
        release.set()
        for thread in threads():
            if thread.name=='bounded-response-realization':thread.join(2)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert len(api.voice_bodies)==2 and not api.google.sheet_writes


def test_oversized_feedback_uses_fallback_without_clipping_facts_or_sending_another_request():
    api=VoiceHosted();api.voice=lambda body:generated(body,doc(paragraph('x'*40000)))
    result,_=turn(api,'ยอดวันนี้',plan())
    assert len(api.voice_bodies)==1 and result.data['response_realization']['reason']=='repair_body_bound'
    assert result.data['response_realization']['validation_code']=='answer_template'


def test_mutation_preview_does_not_enter_read_document_repair():
    api=VoiceHosted()
    def invalid(body):
        payload=json.loads(body['input'][0]['content'])
        assert not payload.get('answer_document') and payload.get('operation_document')
        return generated(body,[{'kind':'text','value':'999 บาท'}])
    api.voice=invalid
    result,_=turn(api,text(),proposed)
    # A Calendar review may repair its own operation document once; it never
    # becomes a read answer, and the readable fallback keeps the bound review.
    repair=json.loads(api.voice_bodies[-1]['input'][0]['content'])
    assert result.status=='preview' and len(api.voice_bodies)==2 and repair['answer_repair'] and not api.calendar.posts
    assert result.data['response_realization']['status']=='fallback' and '999' not in result.message


def unit_host(state=None, respond=None):
    state=state or MemoryState()
    state.claims.setdefault('synthetic_reply',Claim('synthetic_fingerprint',NOW+timedelta(hours=1)))
    bodies=[]
    def request(method,path,*,body):
        bodies.append(deepcopy(body));return respond(body) if respond else generated(body)
    host=SimpleNamespace(state=state,port=SimpleNamespace(request=request),response_realization_enabled=True,
        _current={'at':NOW.isoformat(),'text':'ยอดวันนี้'},_active_event_id='synthetic_reply')
    host.guard=DurableModelGuard(state,monthly_micro_usd=500000,max_calls_day=100,max_calls_month=300,
        clock=lambda:NOW,reservation_calculator=reservation)
    output={'domain':'expense','read_only':True,'complete':True,'outcome':'summary','expense':'0.00','count':0,
        'entries':[],'shown':0,'scope':{'domain':'expense','start':'2026-10-16','end':'2026-10-16','filter':None}}
    return host,read_result(output,NOW),bodies


def test_crash_after_received_validation_failure_recovers_only_the_reserved_repair():
    host,result,bodies=unit_host(respond=lambda body:generated(body,doc(paragraph('999 บาท'))))
    complete=host.guard.complete
    def crash(key,decision):
        complete(key,decision)
        raise RuntimeError('synthetic crash after checkpoint')
    host.guard.complete=crash
    with pytest.raises(RuntimeError):voice.realize(host,'synthetic_reply',result)
    assert len(bodies)==1
    restored=MemoryState();restore_state(restored,state_snapshot(host.state,'synthetic-owner'),'synthetic-owner')
    cold,again,new_bodies=unit_host(restored)
    repaired=voice.realize(cold,'synthetic_reply',again)
    assert repaired.data['response_realization']['status']=='llm' and len(new_bodies)==1
    assert 'answer_repair' in json.loads(new_bodies[0]['input'][0]['content'])
    assert sum(b.calls for b in cold.state.model_budgets.values())==2
    # A second cold recovery before receipt creation reuses both completed
    # decisions; it cannot transmit either model request again.
    latest=MemoryState();restore_state(latest,state_snapshot(cold.state,'synthetic-owner'),'synthetic-owner')
    resumed,third,third_bodies=unit_host(latest)
    assert voice.realize(resumed,'synthetic_reply',third).message==repaired.message and third_bodies==[]


def test_unknown_reserved_repair_cannot_be_retransmitted_after_cold_restore():
    host,result,bodies=unit_host(respond=lambda body:generated(body,doc(paragraph('999 บาท'))))
    before=host.guard.before_request
    def interrupt_reservation():
        if len(host.state.model_runs)==2:raise RuntimeError('synthetic unknown reserved request')
        return before()
    host.guard.before_request=interrupt_reservation
    first=voice.realize(host,'synthetic_reply',result)
    assert first.data['response_realization']['reason']=='reservation' and len(bodies)==1
    restored=MemoryState();restore_state(restored,state_snapshot(host.state,'synthetic-owner'),'synthetic-owner')
    cold,again,new_bodies=unit_host(restored)
    assert voice.realize(cold,'synthetic_reply',again).data['response_realization']['reason']=='quota'
    assert new_bodies==[] and sum(b.calls for b in cold.state.model_budgets.values())==2


def test_feedback_cannot_change_fact_bindings_or_acquire_tool_authority():
    host,result,_=unit_host()
    body=voice.body_for(host,result)
    failure={'validation_code':'answer_fact_coverage','validation_details':{'missing_facts':['f2']},
             'rejected_document':doc(paragraph('{{f1}}'))}
    repaired=composition.repair_body(body,failure);assert voice.validate_body(repaired)
    malformed=deepcopy(repaired);malformed['tools']=[{'type':'function','name':'save'}]
    assert not voice.validate_body(malformed)
    payload=json.loads(repaired['input'][0]['content']);payload['answer_repair']['details']['missing_facts']=['not_offered']
    repaired['input'][0]['content']=json.dumps(payload)
    assert not voice.validate_body(repaired)


def test_slow_reservation_cannot_start_model_io_after_foreground_deadline(monkeypatch):
    monkeypatch.setattr(composition,'TIMEOUT_SECONDS',0.08)
    host,result,bodies=unit_host()
    host.guard.before_request=lambda:Event().wait(0.1)
    answer=voice.realize(host,'synthetic_reply',result)
    assert answer.data['response_realization']['reason']=='deadline' and bodies==[]
    assert sum(b.calls for b in host.state.model_budgets.values())==1


def test_received_document_releases_model_slot_before_immediate_repair(monkeypatch):
    release=Event();Queue=voice.Queue
    class PublishingQueue(Queue):
        def put(self,item,*a,**k):
            super().put(item,*a,**k)
            release.wait(2)
    monkeypatch.setattr(voice,'Queue',PublishingQueue)
    def respond(body):
        return generated(body) if 'answer_repair' in json.loads(body['input'][0]['content']) else generated(body,doc(paragraph('999 บาท')))
    host,result,bodies=unit_host(respond=respond)
    try:
        answer=voice.realize(host,'synthetic_reply',result)
        assert answer.data['response_realization']['status']=='llm' and len(bodies)==2
    finally:
        release.set()
        for thread in threads():
            if thread.name=='bounded-response-realization':thread.join(2)
