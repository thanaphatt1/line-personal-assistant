"""Signed fake adapters validate prose, receipts, budgets and completed effects."""
from copy import deepcopy
from datetime import timedelta
from threading import Event,enumerate as threads
from time import monotonic
import json,socket
import httpx,pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_calendar_factory import Hosted,values
from test_calendar_batch_factory_v13 import proposed,text
from test_calendar_text_confirmation_v13 import turn,old_expense
from test_calendar_mvp import response
from line_assistant.history_cloud_run import create_history_cloud_run_app,HistoryConfig
from line_assistant.line_api import message_hash,rendered_message
from line_assistant.persistence import result_from
from line_assistant.state import Result
from line_assistant import response_realization as voice

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Wholly synthetic integrations')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def generated(body,parts=None):
    payload=json.loads(body['input'][0]['content'])
    if payload.get('answer_document') or payload.get('operation_document'):
        if parts is None:
            view=payload['operation_facts'] if payload.get('operation_document') else payload['answer_facts']
            blocks=[{'kind':'text','template':'{{'+key+'}}'} for key in view['facts']]
            blocks += [{'kind':'rows','record_refs':[row['id']],'heading':'',
                        'template':'{{index}}. '+' — '.join('{{'+key+'}}' for key in row['fields'])}
                       for row in view['records']]
            parts={'blocks':blocks}
        return {'model':'gpt-6-luna','status':'completed','usage':{'input_tokens':1000,'output_tokens':100},
            'output':[{'type':'message','status':'completed','content':[{'type':'output_text','text':json.dumps(parts,ensure_ascii=False)}]}]}
    if payload.get('validation_reply'):
        return {'model':'gpt-6-luna','status':'completed','usage':{'input_tokens':1000,'output_tokens':100},
            'output':[{'type':'message','status':'completed','content':[{'type':'output_text','text':json.dumps({
                'reason_codes':payload['validation_feedback']['codes'],
                'explanation':'ผมส่งหมวดที่คุณยังไม่ได้เลือกไป จึงหยุดก่อนใช้หมวดนั้นครับ'},ensure_ascii=False)}]}]}
    if parts is None and payload.get('query_reply'):
        parts={key:{'before':voice.query_options(key,payload['fact_order'])[0][0],'after':voice.query_options(key,payload['fact_order'])[1][0]} for key in payload['fact_order']}
    if parts is None:
        parts=[]
        for ref in payload['fact_order']:
            if ref.startswith('ask'):parts.append({'kind':'text','value':'ขอ '})
            parts.append({'kind':'fact','value':ref});parts.append({'kind':'text','value':' อีกนิดครับ\n' if ref.startswith('ask') else '\n'})
    return {'model':'gpt-6-luna','status':'completed','usage':{'input_tokens':1000,'output_tokens':100},
        'output':[{'type':'message','status':'completed','content':[{'type':'output_text','text':json.dumps({'opening':'','parts':parts},ensure_ascii=False)}]}]}

class VoiceHosted(Hosted):
    def __init__(self):super().__init__();self.voice=generated;self.voice_bodies=[];self.realization_enabled=True;self.usage_gates_enabled=True
    def app(self,enabled=True):
        config={**values(enabled),'PILOT_RESPONSE_REALIZATION_ENABLED':'true' if self.realization_enabled else 'false','PILOT_MODEL_USAGE_GATES_ENABLED':'true' if self.usage_gates_enabled else 'false'}
        return create_history_cloud_run_app(config,transport=httpx.MockTransport(self.handle),secret_loader=lambda name:SecretStr('synthetic-'+name),clock=lambda:self.now)
    def handle(self,request):
        if request.url.host=='api.openai.com':
            body=json.loads(request.content)
            if body.get('instructions')==voice.INSTRUCTIONS:
                self.model_bodies.append(deepcopy(body));self.voice_bodies.append(deepcopy(body))
                return httpx.Response(200,json=self.voice(body))
        return super().handle(request)

def test_calendar_realization_keeps_all_facts_and_exact_delivered_confirmation_binding_after_cold_restore():
    api=VoiceHosted();r,event=turn(api,text(),proposed)
    assert r.status=='preview' and r.data['response_realization']['status']=='llm',r
    assert 'กิจกรรมทดสอบA' in r.message and 'กิจกรรมทดสอบB' in r.message and '22 ต.ค. 2026' in r.message and '23 ต.ค. 2026' in r.message and r.message.count('08:00–17:00')==2
    state=api.snapshot();receipt=result_from(state['receipts'][event['webhookEventId']]['result'])
    assert receipt==r and state['replies'][event['webhookEventId']]['content_hash']==message_hash(rendered_message(r))
    payload=r.data['line_message']['contents']['footer']['contents'][0]['action']['data'];assert payload.startswith('c2|confirm|')
    saved,confirmation=turn(api,'ยืนยันชุดนัดนี้');assert saved.status=='created' and api.calendar.posts==2 and saved.data['response_realization']['status']=='llm'
    calls=len(api.model_bodies)
    with TestClient(api.app()) as c:assert api.send(c,deepcopy(confirmation)).status_code==200
    assert api.calendar.posts==2 and len(api.model_bodies)==calls

def test_unknown_name_item_enrollment_and_all_multi_item_fields_are_retained():
    api=VoiceHosted();old_expense(api)
    assert len(api.voice_bodies)==1
    payload=json.loads(api.voice_bodies[-1]['input'][0]['content'])
    cells=[f['display'] for row in payload['operation_facts']['records'] for f in row['fields'].values()]
    assert any('23' in value for value in cells) and 'กาแฟ' in cells
    assert 'instruction' in payload['operation_facts']['fact_labels'].values()

@pytest.mark.parametrize('mutation',['drop','duplicate','unknown_ref','literal_amount','literal_created','literal_name','literal_date','extra_tool','incomplete','usage'])
def test_invalid_realization_falls_back_to_review_without_any_write(mutation):
    api=VoiceHosted()
    def bad(body):
        p=json.loads(body['input'][0]['content']);parts=[{'kind':'fact','value':ref} for ref in p['fact_order']]
        if mutation=='drop':parts.pop()
        if mutation=='duplicate':parts.append(parts[0])
        if mutation=='unknown_ref':parts[0]['value']='forged'
        if mutation.startswith('literal_'):parts.insert(0,{'kind':'text','value':{'literal_amount':'100 บาท','literal_created':'สร้างเรียบร้อยแล้ว','literal_name':'คุณสมชาย','literal_date':'พรุ่งนี้ 19:00'}[mutation]})
        value=generated(body,parts)
        if mutation=='extra_tool':value['output'].append({'type':'function_call','name':'create_event','arguments':'{}','status':'completed'})
        if mutation=='incomplete':value['status']='incomplete'
        if mutation=='usage':value['usage']['input_tokens']=999999
        return value
    api.voice=bad;r,_=turn(api,text(),proposed)
    assert r.status=='preview' and r.data['response_realization']['status']=='fallback' and not api.calendar.posts,r
    assert 'กิจกรรมทดสอบA' in r.message and 'กิจกรรมทดสอบB' in r.message

def test_generation_failure_after_actual_fake_effect_does_not_retry_tool_write():
    api=VoiceHosted();turn(api,text(),proposed)
    def unavailable(body):raise httpx.ReadTimeout('synthetic realization failure')
    api.voice=unavailable;saved,event=turn(api,'ยืนยันชุดนัดนี้')
    assert saved.status=='created' and api.calendar.posts==2 and saved.data['response_realization']['status']=='fallback'
    calls=len(api.model_bodies)
    with TestClient(api.app()) as c:assert api.send(c,deepcopy(event)).status_code==200
    assert api.calendar.posts==2 and len(api.model_bodies)==calls

def test_hard_foreground_timeout_returns_saved_status_and_never_retries_unknown_text_attempt(monkeypatch):
    api=VoiceHosted();turn(api,text(),proposed);release=Event();monkeypatch.setattr(voice,'TIMEOUT_SECONDS',0.08);monkeypatch.setattr(voice.answer_composition,'TIMEOUT_SECONDS',0.08)
    def slow(body):release.wait(1);return generated(body)
    api.voice=slow;started=monotonic();saved,event=turn(api,'ยืนยันชุดนัดนี้');elapsed=monotonic()-started
    assert elapsed<0.7 and saved.status=='created' and saved.data['response_realization']['reason']=='timeout' and api.calendar.posts==2
    release.set()
    for thread in threads():
        if thread.name=='bounded-response-realization':thread.join(1)
    calls=len(api.model_bodies)
    with TestClient(api.app()) as c:assert api.send(c,deepcopy(event)).status_code==200
    assert api.calendar.posts==2 and len(api.model_bodies)==calls

def test_optional_realization_never_bypasses_same_guard_or_changes_completed_status():
    api=VoiceHosted();app=api.app()
    with TestClient(app) as c:
        r,_=api.turn(c,text(),proposed);app.state.assistant.guard.limit=0
        saved,_=api.turn(c,'ยืนยันชุดนัดนี้')
    assert saved.status=='created' and api.calendar.posts==2 and saved.data['response_realization']['reason']=='quota'
    assert len(api.voice_bodies)==1

def test_deadline_fallback_makes_no_optional_paid_request(monkeypatch):
    from line_assistant import pilot_http
    monkeypatch.setattr(pilot_http,'remaining_rpc_seconds',lambda:0.01)
    api=VoiceHosted();r,_=turn(api,text(),proposed)
    assert r.status=='preview' and r.data['response_realization']['reason']=='deadline' and not api.voice_bodies

def test_off_flag_uses_existing_one_planner_call_only():
    api=VoiceHosted();api.realization_enabled=False;r,_=turn(api,text(),proposed)
    assert r.status=='preview' and 'response_realization' not in r.data and len(api.model_bodies)==1

@pytest.mark.parametrize('result',[Result('partial','สร้างแล้ว: กิจกรรมทดสอบA\nยังไม่ยืนยันผล: กิจกรรมทดสอบB',{'domain':'calendar','created_refs':['a'],'pending_refs':['b']}),
    Result('uncertain','ยังยืนยันผลไม่ได้ ต้องตรวจสอบก่อน',{'needs_reconciliation':True}),
    Result('income_unconfigured','ยังไม่รองรับรายรับ จึงยังไม่บันทึก',{}),
    Result('clarification','มีทั้งนัดและรายจ่าย ต้องการตรวจข้อมูลฝั่งใดก่อนครับ?',{'domain_selection_required':True})])
def test_partial_unknown_income_and_mixed_domain_outcomes_are_immutable_complete_facts(result):
    class Host:
        _current={};_active_event_id='fake'
        class state:history=[]
    body=voice.body_for(Host(),result);assert voice.validate_body(body)
    text=voice.parse(generated(body),body)
    assert result.message in text and json.loads(body['input'][0]['content'])['facts']['status']==result.message

def test_missing_calendar_fields_are_asked_without_asking_for_known_title_or_dates():
    api=VoiceHosted()
    def incomplete(body):
        value=proposed(body);args=json.loads(value['output'][0]['arguments'])
        for e in args['events']:e['end_time_text']=None
        return response('prepare_calendar_events',args)
    r,_=turn(api,text().replace('8:00-17:00','8:00'),incomplete)
    assert r.status=='clarification' and r.data['response_realization']['status']=='llm'
    p=json.loads(api.voice_bodies[-1]['input'][0]['content']);asked=[v for k,v in p['facts'].items() if k.startswith('ask')]
    assert asked and all('สิ้นสุด' in x for x in asked) and all('ชื่อเรื่อง' not in x and 'วันนัด' not in x for x in asked)

def test_realization_has_no_cross_event_claim_or_unreserved_model_authority():
    api=VoiceHosted();app=api.app()
    from line_assistant.transport import LiveDisabled
    with pytest.raises(LiveDisabled):app.state.pilot_http.allowed('model','POST','/v1/responses',{'instructions':voice.INSTRUCTIONS},None)
    app.state.pilot_http.close()

def test_unknown_signature_and_nonowner_never_trigger_realization():
    api=VoiceHosted()
    with TestClient(api.app()) as client:
        assert client.post('/webhooks/line',content=b'{"events":[]}',headers={'x-line-signature':'invalid'}).status_code==401
        event=api.event('ยืนยันชุดนัด');event['source']['userId']='U'+'b'*32;assert api.send(client,event).status_code==200
    assert not api.model_bodies and not api.calendar.posts

def test_nonce_context_is_redacted_and_title_dates_and_subminute_times_are_preserved():
    title='ทบทวน 2026-10-16 แบบเดิม'
    event={'facts':{'title':title},'start':'2026-10-22T08:00:30.123400+07:00','end':'2026-10-22T09:00:30+07:00','status':'review','missing_fields':[],'issues':[]}
    result=Result('preview','original',{'domain':'calendar','calendar_batch_view':{'events':[event]}})
    class Host:
        _current={'text':'ยืนยันชุดนัด calset_01234567890123456789 abcdef0123456789'};_active_event_id='one'
        class state:history=[];receipts={}
    body=voice.body_for(Host(),result);serialized=json.dumps(body,ensure_ascii=False)
    assert 'calset_01234567890123456789' not in serialized and 'abcdef0123456789' not in serialized
    rendered=voice.parse(generated(body),body)
    assert title in rendered and '08:00:30.1234' in rendered and '09:00:30' in rendered

def test_cancelled_member_is_retained_but_not_counted_as_pending_creation():
    events=[{'facts':{'title':'ทดสอบ'+str(i)},'start':'2026-10-22T08:00:00+07:00','end':'2026-10-22T09:00:00+07:00','status':s,'missing_fields':[],'issues':[]} for i,s in [(1,'review'),(2,'cancelled')]]
    class Host:
        _current={};_active_event_id='one'
        class state:history=[];receipts={}
    body=voice.body_for(Host(),Result('preview','original',{'calendar_batch_view':{'events':events}}));p=json.loads(body['input'][0]['content'])
    assert '1 นัดที่รอยืนยัน' in p['facts']['status'] and 'ทดสอบ2' in p['facts']['row2'] and 'ยกเลิกเฉพาะร่างแล้ว' in p['facts']['row2']

def test_realization_spends_existing_call_and_reservation_budget_without_refunds_or_new_caps():
    api=VoiceHosted();turn(api,text(),proposed);saved,_=turn(api,'ยืนยันชุดนัดนี้')
    budgets=api.snapshot()['model_budgets'];b=next(iter(budgets.values()))
    assert b['calls']==3 and b['reserved_micro_usd']>0 and len(api.model_bodies)==3 and saved.status=='created'

def test_commentary_is_not_joined_to_final_json_or_delivered_to_owner():
    api=VoiceHosted()
    def phased(body):
        value=generated(body);value['output'][0]['phase']='final_answer'
        value['output'].insert(0,{'type':'message','status':'completed','phase':'commentary','content':[{'type':'output_text','text':'untrusted commentary สร้างแล้ว 999 บาท'}]})
        return value
    api.voice=phased;result,_=turn(api,text(),proposed)
    assert result.data['response_realization']['status']=='llm' and '999' not in result.message and 'commentary' not in result.message

@pytest.mark.parametrize('mode',['duplicate_final','no_final','unknown_phase','junk_in_final'])
def test_ambiguous_or_invalid_final_is_not_salvaged(mode):
    api=VoiceHosted()
    def broken(body):
        value=generated(body);final=value['output'][0];final['phase']='final_answer'
        if mode=='duplicate_final':value['output'].append(deepcopy(final))
        if mode=='no_final':final['phase']='commentary'
        if mode=='unknown_phase':final['phase']='untrusted'
        if mode=='junk_in_final':final['content'][0]['text']+=' untrusted trailing text'
        return value
    api.voice=broken;result,_=turn(api,text(),proposed)
    assert result.data['response_realization']['status']=='fallback' and not api.calendar.posts

def test_read_then_prepare_can_realize_after_two_core_calls_without_turn_cap():
    from test_calendar_refresh_quota_v13 import rebuild
    from test_calendar_text_confirmation_v13 import retained
    api=VoiceHosted();retained(api);api.now+=timedelta(minutes=31)
    before=len(api.model_bodies);voices=len(api.voice_bodies)
    fresh,_=turn(api,'ช่วยตรวจร่างชุดนัดเดิมสองนัดอีกครั้ง',response('read_calendar_drafts',{'draft_ref':None}),rebuild)
    assert fresh.status=='preview' and len(api.model_bodies)==before+3 and len(api.voice_bodies)==voices+1
    assert fresh.data['response_realization']['status']=='llm' and not api.calendar.posts

def test_observational_config_allows_existing_exhausted_usage_without_reset_or_write():
    from line_assistant.state import ModelBudget
    from line_assistant.pilot_http import rpc_budget
    api=VoiceHosted();api.usage_gates_enabled=False;app=api.app();state=app.state.assistant.state
    with TestClient(app) as client:
        with rpc_budget(96),state.session():
            state.model_budgets['2026-10']=ModelBudget(900000,700,{'2026-10-16':250},api.now.isoformat());state.checkpoint()
        fresh,_=api.turn(client,text(),proposed)
    budget=api.snapshot()['model_budgets']['2026-10']
    assert fresh.status=='preview' and fresh.data['response_realization']['status']=='llm'
    assert budget['reserved_micro_usd']>900000 and budget['calls']==702 and budget['day_calls']['2026-10-16']==252
    assert len(api.model_bodies)==2 and not api.calendar.posts and not api.google.sheet_writes
