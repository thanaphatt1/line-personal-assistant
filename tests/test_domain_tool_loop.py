"""Offline candidate controls and domain behavior; every socket is refused."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest
from line_assistant.adapters import SheetLayout,MockCalendar
from line_assistant.catalog import ItemCatalog
from line_assistant.config import Settings
from line_assistant.conversation_mocks import ConversationSheets
from line_assistant.domain_runtime import ToolLoopAssistant
from line_assistant.domain_tools import tools
from line_assistant.model_api import DurableModelGuard,MODEL_ID,RESERVATION_MICRO_USD
from line_assistant.persistence import SQLiteState,state_snapshot,restore_state
from line_assistant.state import MemoryState
from line_assistant.tool_loop import ToolLoop,body,step_key,LoopBlocked,MAX_CALLS
from test_conversation import NOW,Harness as Current

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
 def deny(*a,**k):raise AssertionError('Offline domain candidate cannot use sockets')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

ROWS=[('TEST รถเมล์','Transportation',''),('TEST กาแฟ','Food/Grocery','')]
ROWS += [(f'TEST item{i}',c,'') for i,c in enumerate(['Entertainment','Utilities','Gadget','Work/Education','Gift','Financial','Household','Lifestyle','Sport','Work/Office'])]

def response(name,args,index=1):
 return {'model':MODEL_ID,'status':'completed','usage':{'input_tokens':700,'output_tokens':100},
  'output':[{'type':'function_call','status':'completed','call_id':f'call_{index}','name':name,'arguments':json.dumps(args,ensure_ascii=False)}]}

def edit(index=0,**values):return {'entry_id':f'@draft:{index}',**values}
def create(item='TEST รถเมล์',amount='42',**values):return ('draft_edit',{'mode':'create','entries':[edit(item=item,amount=amount,**values)]})
def update(**values):return ('draft_edit',{'mode':'update','entries':[edit(**values)]})
PREVIEW=('preview_draft',{})

class Port:
 mocked=True
 def __init__(self):self.scripts=[];self.requests=[]
 def request(self,method,path,*,body):
  assert method=='POST' and path=='/v1/responses';self.requests.append(deepcopy(body))
  value=self.scripts.pop(0)
  if isinstance(value,Exception):raise value
  if isinstance(value,dict):return value
  value=deepcopy(value)
  if value[0]=='draft_edit':
   ctx=json.loads(body['input'][0]['content'])['context']
   for entry in value[1]['entries']:
    if str(entry.get('entry_id','')).startswith('@draft:'):
     ordinal=int(entry['entry_id'].split(':')[1])
     entry['entry_id']=None if value[1]['mode']=='create' else ctx['entry_ids'][ordinal]
  return response(*value,len(self.requests))

class Harness(Current):
 def __init__(self,state=None,sheets=None,limit=500000):
  self.now=NOW;self.counter=0;self.state=state or MemoryState()
  self.sheets=sheets or ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),ROWS)
  self.state.workbook_id=self.sheets.target.spreadsheet_id
  self.service=ToolLoopAssistant(Settings(owner_id='mock-owner'),self.sheets,MockCalendar(),ItemCatalog([],{}),
   state=self.state,clock=lambda:self.now,candidate_enabled=True)
  self.port=Port();self.guard=DurableModelGuard(self.state,monthly_micro_usd=limit,max_calls_day=100,clock=lambda:self.now)
  self.service.loop=ToolLoop(self.port,self.guard,capture_trace=True);self.service.production=False
 def send(self,text,*calls):
  self.port.scripts=list(calls);return self.service.handle(self.event(text))
 def draft(self,text='TEST รถเมล์ 42 บาท',**values):return self.send(text,create(**values),PREVIEW)

def test_flag_is_explicit_defaults_off_and_live_is_blocked_before_credential_loading():
 from line_assistant.pilot_config import PilotConfig
 from line_assistant.cloud_run import create_cloud_run_app
 from line_assistant.runtime_config import ConfigError
 from test_conversation_transport import config
 assert not PilotConfig.from_mapping({}).domain_tool_loop_enabled
 with pytest.raises(ConfigError):PilotConfig.from_mapping({'PILOT_DOMAIN_TOOL_LOOP_ENABLED':'true'})
 with pytest.raises(ConfigError):create_cloud_run_app({**config(),'PILOT_DOMAIN_TOOL_LOOP_ENABLED':'true'},secret_loader=lambda n:pytest.fail('credential loading'))

@pytest.mark.parametrize('word,month',[('ตุลา',10),('ตค',10),('เดือนตุลาคม',10),('กันยา',9),('Feb',2),('กุมภา',2)])
def test_semantic_month_without_text_aliases_fills_day_and_keeps_other_fields(word,month):
 h=Harness();r=h.draft('วันที่ 10 TEST รถเมล์ 42 บาท',day=10,date_source='วันที่ 10')
 assert r.data['stage']=='date' and h.state.pending.payload['loop_context']['awaiting']['field']=='month'
 r=h.send(word,update(month=month,date_source=word),PREVIEW)
 assert r.status=='preview' and h.state.pending.payload['entries'][0]['date']==f'2026-{month:02d}-10'
 assert h.state.pending.payload['entries'][0]['amount']=='42' and not h.sheets.entries
 ctx=json.loads(h.port.requests[-1]['input'][0]['content'])['context']
 assert ctx['awaiting']['field']=='month' and 'เดือนใด' in ctx['latest_question']
 assert ctx['pending']['entries'][0]['date_required']

def test_wrong_summary_attempt_returns_structured_error_then_model_corrects_within_bound():
 h=Harness();h.draft('วันที่ 10 TEST รถเมล์ 42 บาท',day=10,date_source='วันที่ 10')
 r=h.send('ตุลา',('summary_query',{'start_date':'2026-10-12','end_date':'2026-10-12'}),update(month=10,date_source='ตุลา'),PREVIEW)
 assert r.status=='preview' and len(h.service.loop.trace)==2
 feedback=json.loads(h.port.requests[-1]['input'][2]['output'])
 assert feedback['error']['code']=='summary_not_requested' and feedback['awaiting']['field']=='month'
 assert not h.sheets.entries

@pytest.mark.parametrize('value',[13,0,99])
def test_invalid_month_is_calendar_error_and_does_not_complete_or_write(value):
 h=Harness();h.draft('วันที่ 10 TEST รถเมล์ 42 บาท',day=10,date_source='วันที่ 10')
 r=h.send('ตุลา',update(month=value,date_source='ตุลา'),PREVIEW)
 assert r.status=='clarification' and h.service.loop.trace[0]['outcome']['error']['code']=='invalid_or_future_date'
 assert h.state.pending.payload['entries'][0]['date'] is None and not h.sheets.entries

def test_invalid_day_month_can_be_corrected_without_losing_known_components():
 h=Harness();r=h.draft('วันที่ 31 กุมภา TEST รถเมล์ 42 บาท',day=31,month=2,date_source='วันที่ 31 กุมภา')
 assert r.status=='clarification'
 r=h.send('วันที่ 28',update(day=28,date_source='วันที่ 28'),PREVIEW)
 assert r.status=='preview' and h.state.pending.payload['entries'][0]['date']=='2026-02-28'

@pytest.mark.parametrize('day',[20,32])
def test_future_and_impossible_dates_do_not_become_ready(day):
 h=Harness();r=h.draft(f'วันที่ {day} ตุลา TEST รถเมล์ 42 บาท',day=day,month=10,date_source=f'วันที่ {day} ตุลา')
 assert r.status=='clarification' and not h.state.pending.payload['ready']

@pytest.mark.parametrize('patch', [{'month':10},{'day':11,'date_source':'ตุลา'},{'month':10,'date_source':'TEST รถเมล์'}])
def test_unevidenced_or_item_as_date_proposal_is_rejected(patch):
 h=Harness();h.draft('วันที่ 10 TEST รถเมล์ 42 บาท',day=10,date_source='วันที่ 10')
 before=deepcopy(h.state.pending.payload['entries'])
 r=h.send('ตุลา',update(**patch),PREVIEW)
 assert h.service.loop.trace[0]['outcome']['ok'] is False and h.state.pending.payload['entries']==before
 assert not h.sheets.entries

def test_default_date_and_edits_preserve_note_date_and_classification():
 h=Harness();r=h.draft('TEST รถเมล์ 42 บาท หมายเหตุ invoice',note='invoice')
 before=deepcopy(h.state.pending.payload['entries'][0])
 r=h.send('แก้ยอดเป็น 50 บาท',update(amount='50'),PREVIEW)
 e=h.state.pending.payload['entries'][0]
 assert r.status=='preview' and e=={**before,'amount':'50'}
 assert h.click(r,'บันทึก').status=='recorded' and len(h.sheets.entries)==1


def test_genuine_summary_switch_keeps_active_month_and_protected_question():
 h=Harness();h.draft('วันที่ 10 TEST รถเมล์ 42 บาท',day=10,date_source='วันที่ 10')
 before=deepcopy(h.state.pending.payload)
 r=h.send('สรุปวันนี้',('summary_query',{'start_date':'2026-10-12','end_date':'2026-10-12'}))
 assert r.status=='summary' and h.state.pending.payload==before
 r=h.send('ตุลา',update(month=10,date_source='ตุลา'),PREVIEW)
 assert r.status=='preview'


def test_all_categories_searchable_and_page_buttons_keep_free_typing_and_current_version():
 h=Harness();r=h.draft('TEST รายการใหม่ 42 บาท',item='TEST รายการใหม่')
 r=h.click(r,'เพิ่มรายการใหม่');assert r.data['stage']=='category' and 'พิมพ์ชื่อหมวด' in r.message
 assert 'Work/Office' not in str(r.data)
 old=deepcopy(r);r=h.click(r,'หมวดเพิ่มเติม')
 assert any('Work/Office' in b['action']['label'] for b in r.data['line_message']['contents']['footer']['contents'])
 assert h.click(old,'หมวดเพิ่มเติม').status=='stale'
 r=h.click(r,'Work/Office');assert r.status=='preview' and not h.sheets.entries
 assert h.click(r,'บันทึก').status=='recorded' and h.sheets.rows[-1]==('TEST รายการใหม่','Work/Office','')
 assert h.sheets.atomic_batches==1


def test_search_full_taxonomy_then_bare_choice_is_owner_evidence_not_search_result_approval():
 h=Harness();h.draft('TEST รายการใหม่ 42 บาท',item='TEST รายการใหม่')
 r=h.send('Work/Office',('category_search',{'query':'Work/Office','offset':0}),update(category='Work/Office'),PREVIEW)
 feedback=json.loads(h.port.requests[-1]['input'][2]['output'])
 assert feedback['matches'][0]['label']=='Work/Office' and r.status=='preview' and not h.sheets.entries


def test_unrequested_category_guess_cannot_turn_into_owner_selection():
 h=Harness();r=h.send('TEST รายการใหม่ 42 บาท',create(item='TEST รายการใหม่',category='Work/Office'),PREVIEW,PREVIEW)
 assert all(not row['outcome'].get('ok') for row in h.service.loop.trace)
 assert r.status=='no_pending' and h.state.pending.kind=='tool_source' and not h.sheets.entries


def test_active_draft_cannot_be_replaced_by_model_create_and_loop_limit_stops():
 h=Harness();h.draft();before=deepcopy(h.state.pending.payload)
 r=h.send('TEST กาแฟ 30 บาท',create(item='TEST กาแฟ',amount='30'),create(item='TEST กาแฟ',amount='30'),create(item='TEST กาแฟ',amount='30'))
 assert r.status=='tool_loop_limit' and h.state.pending.payload==before and len(h.service.loop.trace)==3


def test_duplicate_event_stale_and_cancelled_buttons_never_repeat_models_or_writes():
 h=Harness();h.port.scripts=[create(),PREVIEW];ev=h.event('TEST รถเมล์ 42 บาท')
 r=h.service.handle(ev);calls=len(h.port.requests)
 assert h.service.handle(ev)==r and len(h.port.requests)==calls
 old=deepcopy(r);r=h.send('แก้ยอดเป็น 50 บาท',update(amount='50'),PREVIEW)
 assert h.click(old,'บันทึก').status=='stale' and not h.sheets.entries
 assert h.send('ยกเลิก').status=='cancelled' and h.click(r,'บันทึก').status=='stale'
 assert h.send('บันทึก').status=='no_pending'


def test_model_cannot_call_commit_or_emit_success_prose():
 for output in [response('commit_expense',{'approved':True}),{'model':MODEL_ID,'status':'completed','usage':{'input_tokens':5,'output_tokens':3},'output':[{'type':'message','content':[{'type':'output_text','text':'Saved!'}]}]}]:
  h=Harness();r=h.send('TEST รถเมล์ 42 บาท',output)
  assert r.status=='model_unavailable' and len(h.port.requests)==1 and not h.sheets.entries


def test_transport_failure_and_usage_overflow_are_terminal_no_retry():
 h=Harness();h.draft();old=deepcopy(h.state.pending.payload)
 r=h.send('ตุลา',RuntimeError('synthetic outage'))
 assert r.status=='model_unavailable' and h.state.pending.payload==old
 value=response('preview_draft',{});value['usage']['input_tokens']=9000
 assert h.send('TEST รถเมล์ 42 บาท',value).status=='model_unavailable' and h.state.model_halted


def test_shared_budget_reserves_each_step_and_stops_without_reset():
 h=Harness(limit=RESERVATION_MICRO_USD);r=h.send('TEST รถเมล์ 42 บาท',('catalog_search',{'query':'รถเมล์','offset':0}),create())
 assert r.status=='model_budget_exhausted' and len(h.port.requests)==1 and h.state.pending
 budget=next(iter(h.state.model_budgets.values()));assert budget.calls==1 and budget.reserved_micro_usd==RESERVATION_MICRO_USD


def test_protected_context_fails_closed_instead_of_dropping_question_or_draft():
 h=Harness();h.draft('วันที่ 10 TEST รถเมล์ 42 บาท',day=10,date_source='วันที่ 10')
 ctx=h.service.loop_context(h.sheets.taxonomy());ctx['recent']=[{'role':'user','text':'x'*240} for _ in range(6)]
 result=body('ตุลา',NOW,ctx);sent=json.loads(result['input'][0]['content'])['context']
 assert sent['pending']==ctx['pending'] and sent['awaiting']==ctx['awaiting'] and sent['latest_question']==ctx['latest_question']
 ctx['pending']['entries'][0]['note']='😀'*10000
 with pytest.raises(LoopBlocked):body('ตุลา',NOW,ctx)


def test_plain_snapshot_cold_start_and_feature_off_preserve_ledger_proofs_and_buttons(tmp_path):
 h=Harness(state=SQLiteState(tmp_path/'candidate.sqlite','mock-owner'));r=h.draft()
 raw=state_snapshot(h.state,'mock-owner');assert len(raw['model_runs'])==1 and all(v['decision'] is None for v in raw['model_runs'].values())
 restored=MemoryState();restore_state(restored,deepcopy(raw),'mock-owner');assert state_snapshot(restored,'mock-owner')==raw
 h2=Harness(state=SQLiteState(tmp_path/'candidate.sqlite','mock-owner'),sheets=h.sheets);h2.now=h.now;h2.counter=h.counter
 assert h2.click(r,'บันทึก').status=='recorded' and len(h.sheets.entries)==1
 current=Current(state=restored,sheets=h.sheets);current.now=h.now
 assert set(current.state.model_runs)==set(h.state.model_runs) and current.state.model_budgets==h.state.model_budgets


def test_model_cannot_invent_month_from_day_only_numeric_witness():
 h=Harness();r=h.send('วันที่ 10 TEST รถเมล์ 42 บาท',create(day=10,month=10,date_source='10'),PREVIEW,PREVIEW)
 assert h.service.loop.trace[0]['outcome']['error']['code']=='month_not_supplied' and not h.sheets.entries


def test_failed_edit_and_transport_failure_invalidate_old_preview_without_losing_facts():
 h=Harness();old=h.draft();facts=deepcopy(h.state.pending.payload['entries'])
 r=h.send('แก้ยอดเป็น 50 บาท',RuntimeError('synthetic outage'))
 assert r.status=='model_unavailable' and h.state.pending.payload['entries']==facts
 assert h.click(old,'บันทึก').status=='stale' and not h.sheets.entries


def test_interrupted_reversible_tool_cold_start_shows_saved_draft_without_paid_retry(tmp_path):
 h=Harness(state=SQLiteState(tmp_path/'interrupted.sqlite','mock-owner'))
 ev=h.event('TEST รถเมล์ 42 บาท');h.port.scripts=[create(),PREVIEW]
 class Crash(BaseException):pass
 h.service.phase_hook=lambda stage,key: (_ for _ in ()).throw(Crash()) if stage=='domain_tool_completed' else None
 with pytest.raises(Crash):h.service.handle(ev)
 assert len(h.port.requests)==1 and h.state.pending and not h.sheets.entries
 h2=Harness(state=SQLiteState(tmp_path/'interrupted.sqlite','mock-owner'),sheets=h.sheets);h2.now=h.now;h2.counter=h.counter
 r=h2.service.handle(ev);assert r.status=='preview' and len(h2.port.requests)==0
 assert h2.service.handle(ev)==r and not h.sheets.entries
 assert all(v.decision is None for v in h2.state.model_runs.values())


def test_candidate_hosted_factory_stays_on_mock_routes_and_owner_signature_gates():
 import httpx
 from fastapi.testclient import TestClient
 from line_assistant.cloud_run import create_cloud_run_app
 from line_assistant.main import signature
 from test_conversation_transport import HostedConversation,config
 from test_cloud_run import fake_secrets
 api=HostedConversation();queued=[('draft_edit',{'mode':'create','entries':[{'entry_id':None,'item':'รถเมล์','amount':'42'}]})]
 def handle(request):
  if request.url.host=='api.openai.com':
   api.models+=1;return httpx.Response(200,json=response(*queued.pop(0),api.models))
  return api.handle(request)
 app=create_cloud_run_app({**config(),'PILOT_DOMAIN_TOOL_LOOP_ENABLED':'true'},transport=httpx.MockTransport(handle),secret_loader=fake_secrets,clock=lambda:api.now)
 with TestClient(app) as client:
  ev=api.event(text='รถเมล์ 42 บาท')
  assert api.send(client,ev).status_code==200
  assert api.snapshot()['receipts'][ev['webhookEventId']]['result']['status']=='preview'
  assert api.models==1 and api.google.sheet_writes==0
  calls=api.models;assert api.send(client,ev).status_code==200 and api.models==calls
  bad=api.event(text='รถเมล์ 42 บาท');bad['source']['userId']='U'+'b'*32
  assert api.send(client,bad).status_code==200 and api.models==calls
  assert client.post('/webhooks/line',content=b'{}',headers={'x-line-signature':'wrong'}).status_code==401
 app.state.pilot_http.close()


def test_tool_schema_requires_every_nullable_property_and_rejects_parallel_results():
 from line_assistant.tool_loop import parse
 def objects(value):
  if isinstance(value,dict):
   if value.get('type')=='object':assert value['additionalProperties'] is False and set(value['required'])==set(value['properties'])
   for v in value.values():objects(v)
  if isinstance(value,list):
   for v in value:objects(v)
 for t in tools():objects(t['parameters'])
 value=response('preview_draft',{});value['output']*=2
 with pytest.raises(LoopBlocked):parse(value)


def test_plain_capacity_is_finite_and_proofs_survive_feature_off_snapshot(tmp_path):
 from line_assistant.snapshot_codec import encode,decode
 from line_assistant.transport import OutcomeUnknown
 h=Harness();last=None
 for i in range(20):
  last=h.draft();assert h.click(last,'บันทึก').status=='recorded'
 raw=state_snapshot(h.state,'mock-owner');encoded=encode(raw,200000,compact=False)
 assert len(encoded.encode())<=200000 and len(raw['model_runs'])==20 and all(v['decision'] is None for v in raw['model_runs'].values())
 restored=MemoryState();restore_state(restored,decode(encoded,200000),'mock-owner')
 assert restored.model_budgets==h.state.model_budgets and restored.actions==h.state.actions and restored.claims==h.state.claims
 oversized=deepcopy(raw);oversized['padding']='x'*200000
 with pytest.raises(OutcomeUnknown):encode(oversized,200000,compact=False)
 assert len(oversized['model_runs'])==20


def test_context_rejects_server_fields_and_extreme_date_components_cannot_poison_draft():
 h=Harness();h.draft('วันที่ 10 TEST รถเมล์ 42 บาท',day=10,date_source='วันที่ 10')
 ctx=h.service.loop_context(h.sheets.taxonomy());ctx['owner_id']='must-not-be-sent'
 with pytest.raises(LoopBlocked):body('ตุลา',NOW,ctx)
 r=h.send('ตุลา',update(month=100000,date_source='ตุลา'),PREVIEW)
 assert r.status=='clarification' and h.state.pending.payload['entries'][0]['date_parts']=={'day':10}
 assert h.service.loop.trace[0]['outcome']['error']['code']=='invalid_arguments'


def test_both_flag_directions_preserve_proofs_and_require_current_button_review():
 h=Harness();r=h.draft();raw=state_snapshot(h.state,'mock-owner')
 restored=MemoryState();restore_state(restored,deepcopy(raw),'mock-owner')
 current=Current(state=restored,sheets=h.sheets);current.now=h.now;current.counter=h.counter
 buttons=r.data['line_message']['contents']['footer']['contents']
 data=next(b['action']['data'] for b in buttons if b['action']['label']=='บันทึก')
 ev=current.event(postback=data);done=current.service.handle(ev)
 assert done.status=='recorded' and current.service.handle(ev)==done and len(h.sheets.entries)==1
 assert current.state.model_budgets==h.state.model_budgets and current.port.calls==0
 source=Current(sheets=h.sheets);old=source.expense(item='TEST รถเมล์',amount='42')
 restored=MemoryState();restore_state(restored,state_snapshot(source.state,'mock-owner'),'mock-owner')
 candidate=Harness(state=restored,sheets=h.sheets);candidate.now=source.now;candidate.counter=source.counter
 r=candidate.send('สรุปวันนี้',('summary_query',{'start_date':'2026-10-12','end_date':'2026-10-12'}))
 assert r.status=='summary' and candidate.state.pending.payload['loop_context']['question']
 assert candidate.click(old,'บันทึก').status=='stale' and len(h.sheets.entries)==1




def test_expense_candidate_keeps_recorded_edit_income_and_calendar_out_of_scope():
 h=Harness();h.draft();facts=deepcopy(h.state.pending.payload['entries']);calls=len(h.port.requests)
 for text,status in [('แก้รายจ่ายล่าสุดเป็น 50 บาท','unsupported'),('ช่วยนัดตรวจฟันพรุ่งนี้','unsupported'),('เงินเข้า 500 บาท','income_unconfigured')]:
  assert h.send(text).status==status and len(h.port.requests)==calls
  assert h.state.pending.payload['entries']==facts and not h.sheets.entries


def test_hosted_default_keeps_no_inspection_trace():
 h=Harness();h.service.loop=ToolLoop(h.port,h.guard)
 assert h.draft().status=='preview' and h.service.loop.trace==[]
