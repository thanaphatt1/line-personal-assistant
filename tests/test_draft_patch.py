"""Scripted full-field regressions for the default-off single-call candidate.

These are runtime tests, not fresh model accuracy evidence. Every socket denied.
"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json,socket
import pytest
from line_assistant.adapters import MockCalendar,SheetLayout
from line_assistant.catalog import ItemCatalog
from line_assistant.config import Settings
from line_assistant.conversation_mocks import ConversationSheets
from line_assistant.draft_patch import DraftPatchAssistant,PatchPlanner,Proposal,NAME,VERSION,body,tools
from line_assistant.model_api import RESERVATION_MICRO_USD
from line_assistant.persistence import SQLiteState,state_snapshot,restore_state
from line_assistant.state import MemoryState
from test_domain_tool_loop import Harness as LoopHarness,response

NOW=datetime(2026,10,15,2,tzinfo=timezone.utc)
ROWS=[('รถสองแถวสายสวน','Transportation',''),('สมุดปกคราม','Work/Education',''),('ชาใบหม่อน','Food','')]
ROWS += [(f'สินค้า{i}',c,'') for i,c in enumerate(['Entertainment','Utilities','Gadget','Gift','Financial','Household','Lifestyle','Sport','ดูแลสุขภาพ'])]

@pytest.fixture(autouse=True)
def offline(monkeypatch):
 def deny(*a,**k):raise AssertionError('No network permitted for draft patch tests')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def change(field,value=None,op='set'):return {'field':field,'op':op,'value':value}
def envelope(intent='draft',mode='create',entries=None,**values):
 return {'version':VERSION,'intent':intent,'mode':mode if intent=='draft' else None,
  'entries':entries if entries is not None else [],'lookup':None,'question_field':None,'start_date':None,'end_date':None,**values}
def patch(mode='create',entry_id='@0',date_source=None,ops=(),**fields):
 return envelope(mode=mode,entries=[{'entry_id':None if mode=='create' else entry_id,'date_source':date_source,
  'result_set_id':None,'changes':[change(k,v) for k,v in fields.items()]+list(ops)}])

class Port:
 mocked=True
 def __init__(self):self.scripts=[];self.requests=[]
 def request(self,method,path,*,body):
  assert (method,path)==('POST','/v1/responses');self.requests.append(deepcopy(body))
  value=self.scripts.pop(0)
  if isinstance(value,Exception):raise value
  if callable(value):value=value(deepcopy(body))
  if isinstance(value,dict) and 'output' in value:return value
  value=deepcopy(value);ctx=json.loads(body['input'][0]['content'])['context']
  for entry in value.get('entries',[]):
   if str(entry.get('entry_id')).startswith('@'):entry['entry_id']=ctx['entry_ids'][int(entry['entry_id'][1:])]
  return response(NAME,value,len(self.requests))

class Harness(LoopHarness):
 def __init__(self,state=None,sheets=None,limit=500000):
  super().__init__(state=state,sheets=sheets or ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),ROWS),limit=limit)
  self.now=NOW
  self.service=DraftPatchAssistant(Settings(owner_id='mock-owner'),self.sheets,MockCalendar(),ItemCatalog([],{}),
   state=self.state,clock=lambda:self.now,candidate_enabled=True,simple_patch_enabled=True)
  self.service.production=False;self.port=Port();self.service.loop=PatchPlanner(self.port,self.guard,capture_trace=True)
 def draft(self,text='รถสองแถวสายสวน 35 บาท',**values):
  return self.send(text,patch(item='รถสองแถวสายสวน',amount='35',**values))

def full(h):
 p=h.state.pending
 if not p or p.kind!='conversation':return None
 tax=h.sheets.taxonomy();rows=[]
 for e in p.payload['entries']:
  known=tax.items.get(e['item_id'])
  rows.append({**{k:e[k] for k in ['item','amount','date','note','kind','date_parts','new_item','date_required']},
   'category':known.category if known else tax.categories.get(e['category_id']),
   'subcategory':known.subcategory if known else tax.pairs.get(e['category_id'],{}).get(e['subcategory_id'])})
 return {'entries':rows,'ready':p.payload['ready']}
def expected(item='รถสองแถวสายสวน',amount='35',date='2026-10-15',note='',category='Transportation',subcategory='',new_item=False,date_parts=None,date_required=False,ready=True):
 return {'entries':[{'item':item,'amount':amount,'date':date,'note':note,'category':category,'subcategory':subcategory,
  'kind':'expense','date_parts':date_parts or {},'new_item':new_item,'date_required':date_required}],'ready':ready}


def test_default_off_mock_only_and_existing_hosted_factory_not_replaced():
 args=(Settings(),ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),ROWS),MockCalendar(),ItemCatalog([],{}))
 with pytest.raises(ValueError):DraftPatchAssistant(*args,candidate_enabled=True)
 with pytest.raises(ValueError):DraftPatchAssistant(*args,simple_patch_enabled=True)
 from line_assistant.pilot_config import PilotConfig
 assert not PilotConfig.from_mapping({}).domain_tool_loop_enabled


def test_ordinary_create_is_one_call_and_complete_fields_are_runtime_owned():
 h=Harness();r=h.draft();assert r.status=='preview' and full(h)==expected()
 assert len(h.port.requests)==1 and not h.sheets.entries
 request=h.port.requests[0];assert [t['name'] for t in request['tools']]==[NAME]
 assert request['store'] is False and request['parallel_tool_calls'] is False
 assert request['max_output_tokens']==1024 and len(json.dumps(request,ensure_ascii=False,separators=(',',':')).encode())<=6144


def test_earlier_failed_holdout_sequence_now_uses_four_single_call_patches():
 h=Harness();r=h.draft('จ่ายค่ารถสองแถวสายสวน 35 บาท วันที่ 4 หมายเหตุ ไปส่งพัสดุหน้าตลาด',day=4,date_source='วันที่ 4',note='ไปส่งพัสดุหน้าตลาด')
 assert r.status=='clarification' and full(h)==expected(date=None,note='ไปส่งพัสดุหน้าตลาด',date_parts={'day':4},date_required=True,ready=False)
 ctx=json.loads(h.port.requests[0]['input'][0]['content'])['context'];assert ctx['pending'] is None
 r=h.send('เดือนก่อนจ้า',patch('update',month=9,year=2026,date_source='เดือนก่อน'))
 assert r.status=='preview' and full(h)==expected(date='2026-09-04',note='ไปส่งพัสดุหน้าตลาด');old=deepcopy(r)
 ctx=json.loads(h.port.requests[1]['input'][0]['content'])['context']
 assert ctx['awaiting']['field']=='month' and 'เดือนใด' in ctx['latest_question'] and ctx['pending']['entries'][0]['amount']=='35'
 r=h.send('แก้วันที่เป็น 6 กันยายน',patch('update',day=6,month=9,date_source='6 กันยายน'))
 assert full(h)==expected(date='2026-09-06',note='ไปส่งพัสดุหน้าตลาด') and h.click(old,'บันทึก').status=='stale'
 r=h.send('ล้างหมายเหตุเดิม',patch('update',ops=[change('note',None,'clear')]))
 assert full(h)==expected(date='2026-09-06') and len(h.port.requests)==4 and not h.sheets.entries

@pytest.mark.parametrize('word,month',[('ตุลา',10),('กันยา',9),('ตค',10),('เดือนกุมภา',2),('Feb',2)])
def test_natural_month_followup_preserves_every_other_field(word,month):
 h=Harness();h.draft('วันที่ 4 รถสองแถวสายสวน 35 บาท หมายเหตุ ใบเสร็จ',day=4,date_source='วันที่ 4',note='ใบเสร็จ')
 r=h.send(word,patch('update',month=month,date_source=word,ops=[change('note',None,'unchanged')]))
 assert r.status=='preview' and full(h)==expected(date=f'2026-{month:02d}-04',note='ใบเสร็จ') and len(h.port.requests)==2

@pytest.mark.parametrize('month,expected_date',[(1,'2025-12-04'),(10,'2026-09-04')])
def test_relative_month_year_rollover_is_checked_from_owner_span(month,expected_date):
 h=Harness();h.now=datetime(2026,month,15,2,tzinfo=timezone.utc)
 h.draft('วันที่ 4 รถสองแถวสายสวน 35 บาท',day=4,date_source='วันที่ 4')
 date=datetime.fromisoformat(expected_date);r=h.send('เดือนก่อนค่ะ',patch('update',month=date.month,year=date.year,date_source='เดือนก่อน'))
 assert r.status=='preview' and full(h)==expected(date=expected_date)

@pytest.mark.parametrize('word,date', [('วันนี้','2026-10-15'),('เมื่อวาน','2026-10-14'),('เมื่อวานซืน','2026-10-13')])
def test_relative_day_parts_have_independent_source_validation(word,date):
 h=Harness();d=datetime.fromisoformat(date)
 r=h.draft(f'รถสองแถวสายสวน 35 บาท {word}',day=d.day,month=d.month,year=d.year,date_source=word)
 assert r.status=='preview' and full(h)==expected(date=date)


def test_wrong_relative_month_is_rejected_without_losing_day():
 h=Harness();h.draft('วันที่ 4 รถสองแถวสายสวน 35 บาท',day=4,date_source='วันที่ 4')
 facts=deepcopy(h.state.pending.payload['entries'])
 r=h.send('เดือนก่อน',patch('update',month=8,date_source='เดือนก่อน'))
 assert r.status=='clarification' and h.state.pending.payload['entries']==facts
 assert h.service.loop.trace[0]['outcome']['error']['code']=='numeric_date_mismatch' and not h.sheets.entries

@pytest.mark.parametrize('values',[{'month':13,'date_source':'ตุลา'},{'day':31,'month':2,'date_source':'วันที่ 31 กุมภา'}])
def test_impossible_dates_stay_unready(values):
 h=Harness();text='ตุลา' if values.get('month')==13 else 'วันที่ 31 กุมภา'
 r=h.draft(f'รถสองแถวสายสวน 35 บาท {text}',**values)
 assert r.status=='clarification' and (full(h) is None or not full(h)['ready']) and not h.sheets.entries

@pytest.mark.parametrize('op', ['omitted','unchanged'])
def test_optional_note_is_not_inferred_from_item_tags(op):
 h=Harness();ops=[] if op=='omitted' else [change('note',None,'unchanged')]
 r=h.send('โปรชาใบหม่อน 48 บาท',patch(item='ชาใบหม่อน',amount='48',ops=ops))
 assert r.status=='clarification' and full(h)==expected(item='โปรชาใบหม่อน',amount='48',category=None,subcategory=None,ready=False)
 assert len(h.port.requests)==1


def test_unbacked_optional_note_is_ignored_without_losing_valid_required_facts():
 h=Harness();r=h.send('โปรชาใบหม่อน 48 บาท',patch(item='ชาใบหม่อน',amount='48',note='โปร'))
 assert full(h)==expected(item='โปรชาใบหม่อน',amount='48',category=None,subcategory=None,ready=False)
 assert h.service.loop.trace[0]['outcome']['warnings'][0]['code']=='note_ignored_no_owner_source' and len(h.port.requests)==1


def test_explicit_note_can_contain_item_tags_quotes_and_negation_literally():
 h=Harness();note='เก็บใบเสร็จชุดโปร TEST ไว้ ไม่บันทึกคำว่า "ยืนยัน"'
 r=h.send('ซื้อสมุดปกครามไปเจ็ดสิบสองบาท หมายเหตุ '+note,patch(item='สมุดปกคราม',amount='72',note=note))
 assert r.status=='preview' and full(h)==expected(item='สมุดปกคราม',amount='72',note=note,category='Work/Education')
 r=h.send('เปลี่ยนหมายเหตุเป็น สำหรับงานทีมดาว',patch('update',note='สำหรับงานทีมดาว'))
 assert full(h)==expected(item='สมุดปกคราม',amount='72',note='สำหรับงานทีมดาว',category='Work/Education')
 r=h.send('ล้างหมายเหตุเดิม',patch('update',ops=[change('note',None,'clear')]))
 assert full(h)==expected(item='สมุดปกคราม',amount='72',category='Work/Education')

@pytest.mark.parametrize('category',[row[1] for row in ROWS])
def test_all_categories_can_be_selected_by_owner_text_without_search(category):
 h=Harness();h.send('โปรชาใบหม่อน 48 บาท',patch(item='โปรชาใบหม่อน',amount='48'))
 r=h.send('ใช้หมวด'+category,patch('update',category=category))
 assert r.status=='preview' and full(h)==expected(item='โปรชาใบหม่อน',amount='48',category=category,new_item=True)
 assert len(h.port.requests)==2 and not h.sheets.entries


def test_unknown_category_is_not_silently_created_or_classified_by_fallback():
 h=Harness();h.send('โปรชาใบหม่อน 48 บาท',patch(item='โปรชาใบหม่อน',amount='48'))
 before=full(h);r=h.send('ใช้หมวดหมวดที่ไม่มีจริง',patch('update',category='หมวดที่ไม่มีจริง'))
 assert r.status=='clarification' and full(h)['entries']==before['entries'] and not full(h)['ready']
 assert h.sheets.rows==ROWS and not h.sheets.entries


def test_summary_topic_switch_preserves_pending_and_checks_today_range():
 h=Harness();h.draft('วันที่ 4 รถสองแถวสายสวน 35 บาท',day=4,date_source='วันที่ 4');before=deepcopy(h.state.pending.payload)
 r=h.send('ขอสรุปรายจ่ายของวันนี้หน่อย',envelope('summary',start_date='2026-10-15',end_date='2026-10-15'))
 assert r.status=='summary' and r.data['expense']=='0.00' and h.state.pending.payload==before and len(h.port.requests)==2
 h.send('กันยา',patch('update',month=9,date_source='กันยา'));assert full(h)==expected(date='2026-09-04')


def test_wrong_summary_range_is_not_accepted_even_when_totals_are_equal():
 h=Harness();h.draft();r=h.send('ขอสรุปรายจ่ายของวันนี้หน่อย',envelope('summary',start_date='2026-10-14',end_date='2026-10-14'))
 assert r.status=='clarification' and h.service.loop.trace[0]['outcome']['error']['code']=='summary_range_mismatch'


def test_no_draft_recovery_uses_retained_source_and_never_recent_guesses(tmp_path):
 path=tmp_path/'state.sqlite';h=Harness(state=SQLiteState(path,'mock-owner'))
 source='รถสองแถวสายสวน 35 บาท วันที่ 4 หมายเหตุ ใบเสร็จ'
 h.send(source,RuntimeError('offline failure'));expiry=h.state.pending.expires_at
 h2=Harness(state=SQLiteState(path,'mock-owner'),sheets=h.sheets);h2.now=h.now;h2.counter=h.counter
 r=h2.send('เดือนก่อนจ้า',patch(item='รถสองแถวสายสวน',amount='35',day=4,month=9,year=2026,date_source='เดือนก่อน'))
 assert r.status=='preview' and full(h2)==expected(date='2026-09-04',note='ใบเสร็จ') and h2.state.pending.expires_at==expiry
 ctx=json.loads(h2.port.requests[0]['input'][0]['content'])['context'];assert ctx['source_context']['text']==source

@pytest.mark.parametrize('control',['cancel','expire'])
def test_failed_source_is_not_resurrected_after_cancel_or_expiry(control):
 h=Harness();h.send('รถสองแถวสายสวน 35 บาท วันที่ 4',RuntimeError('offline'))
 if control=='cancel':h.send('ยกเลิก')
 else:h.now+=timedelta(seconds=1801)
 r=h.send('เดือนก่อนจ้า',patch(item='รถสองแถวสายสวน',amount='35',month=9,date_source='เดือนก่อน'))
 assert r.status=='clarification' and full(h) is None and not h.sheets.entries
 assert json.loads(h.port.requests[-1]['input'][0]['content'])['context']['source_context'] is None

@pytest.mark.parametrize('text',['แก้วันที่เป็น 6 กันยายน','ล้างหมายเหตุเดิม','บันทึก'])
def test_no_draft_controls_do_not_call_model(text):
 h=Harness();assert h.send(text).status=='no_pending' and not h.port.requests and not h.sheets.entries


def test_text_confirmation_is_current_review_bound_and_duplicate_is_once():
 h=Harness();old=h.draft();r=h.send('แก้ยอดเป็น 50 บาท',patch('update',amount='50'))
 assert h.click(old,'บันทึก').status=='stale' and not h.sheets.entries
 event=h.event('โอเคบันทึกเลยครับ');calls=len(h.port.requests);done=h.service.handle(event)
 assert done.status=='recorded' and h.service.handle(deepcopy(event))==done and len(h.sheets.entries)==1 and len(h.port.requests)==calls
 assert h.click(r,'บันทึก').status=='stale'


def test_late_delivery_of_older_text_confirmation_is_stale():
 h=Harness();h.draft();confirm=h.event('บันทึกเลย');h.send('แก้ยอดเป็น 50 บาท',patch('update',amount='50'))
 assert h.service.handle(confirm).status=='stale' and not h.sheets.entries


def test_model_prose_and_general_ack_cannot_commit():
 h=Harness();h.draft();r=response(NAME,envelope('clarify'))
 r['output'].insert(0,{'type':'message','status':'completed','content':[{'type':'output_text','text':'บันทึกเรียบร้อย Saved!'}]})
 result=h.send('โอเคครับ',r)
 assert result.status=='preview' and 'Saved' not in result.message and not h.sheets.entries

@pytest.mark.parametrize('bad',['old_function','version','index','duplicate_field','set_empty_note','unchanged_value','clear_without_source','clear_amount'])
def test_schema_and_operation_failures_are_terminal_and_invalidate_old_review(bad):
 h=Harness();old=h.draft();value=patch('update',amount='50')
 value['entries'][0]['entry_id']=h.service.loop_context(h.sheets.taxonomy())['entry_ids'][0]
 if bad=='old_function':raw=response('draft_edit',{'mode':'update','entries':[{'index':1,'amount':'50'}]})
 else:
  if bad=='version':value['version']='obsolete'
  elif bad=='index':value['entries'][0]['index']=1
  elif bad=='duplicate_field':value['entries'][0]['changes']*=2
  elif bad=='set_empty_note':value['entries'][0]['changes']=[change('note','')]
  elif bad=='unchanged_value':value['entries'][0]['changes']=[change('note','invented','unchanged')]
  elif bad=='clear_without_source':value['entries'][0]['changes']=[change('note',None,'clear')]
  else:value['entries'][0]['changes']=[change('amount',None,'clear')]
  raw=response(NAME,value)
 r=h.send('แก้ยอดเป็น 50 บาท',raw)
 assert r.status in {'model_unavailable','clarification'} and len(h.port.requests)==2 and h.click(old,'บันทึก').status=='stale'
 assert not h.sheets.entries and h.state.pending.payload['entries'][0]['amount']=='35'


def test_adoption_of_existing_draft_requires_fresh_review_before_text_confirm():
 old=LoopHarness(sheets=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),ROWS))
 old.draft('รถสองแถวสายสวน 35 บาท',item='รถสองแถวสายสวน',amount='35')
 saved=state_snapshot(old.state,'mock-owner');state=MemoryState();restore_state(state,saved,'mock-owner')
 h=Harness(state=state,sheets=old.sheets);h.now=old.now;h.counter=old.counter
 r=h.send('บันทึก');assert r.status=='preview' and not h.port.requests and not h.sheets.entries
 assert h.send('บันทึกเลย').status=='recorded' and len(h.sheets.entries)==1


def test_failed_correction_invalidates_confirmation_without_losing_business_facts():
 h=Harness();old=h.draft();facts=full(h)['entries'];r=h.send('ยอดจริง 50 บาท',RuntimeError('offline'))
 assert r.status=='model_unavailable' and full(h)['entries']==facts and not full(h)['ready']
 assert h.click(old,'บันทึก').status=='stale' and not h.sheets.entries


def test_budget_is_shared_and_no_retry_on_failure():
 h=Harness(limit=RESERVATION_MICRO_USD);h.draft();r=h.send('แก้ยอดเป็น 50 บาท',patch('update',amount='50'))
 assert r.status=='model_budget_exhausted' and len(h.port.requests)==1 and not h.sheets.entries
 budget=next(iter(h.state.model_budgets.values()));assert budget.calls==1 and budget.reserved_micro_usd==RESERVATION_MICRO_USD


def test_lookup_has_one_continuation_only_when_missing_result_is_needed():
 h=Harness();lookup=envelope('lookup',lookup={'kind':'category','query':'','offset':8})
 r=h.send('ค้นหาหมวดที่มีอยู่เพิ่มเติม',lookup,envelope('clarify',question_field='category'))
 assert r.status=='clarification' and len(h.port.requests)==1 and 'ดูแลสุขภาพ' in r.message
 # A read-only label list needs no model to interpret its own lookup result.
 assert not h.sheets.entries


def test_redundant_lookup_is_terminal_without_second_model_call():
 h=Harness();r=h.send('รถสองแถวสายสวน 35 บาท',envelope('lookup',lookup={'kind':'item','query':'รถสองแถวสายสวน','offset':0}))
 assert r.status=='clarification' and len(h.port.requests)==1 and h.service.loop.trace[0]['outcome']['error']['code']=='lookup_already_in_context'


def test_model_cannot_chain_more_searches():
 h=Harness();look=envelope('lookup',lookup={'kind':'category','query':'','offset':8})
 r=h.send('ต้องการเลือกหมวดที่อยู่นอกตัวเลือก',look,look)
 assert r.status=='clarification' and len(h.port.requests)==2 and h.service.loop.trace[-1]['outcome']['error']['code']=='lookup_limit'


def test_strict_schema_has_all_required_properties_and_protected_context_never_drops():
 def check(value):
  if isinstance(value,dict):
   if value.get('type')=='object' or value.get('type')==['object','null']:
    assert value['additionalProperties'] is False and set(value['required'])==set(value['properties'])
   for v in value.values():check(v)
  if isinstance(value,list):
   for v in value:check(v)
 check(tools());h=Harness();h.draft('วันที่ 4 รถสองแถวสายสวน 35 บาท',day=4,date_source='วันที่ 4')
 ctx=h.service.loop_context(h.sheets.taxonomy());request=body('เดือนก่อนจ้า',h.now,ctx);sent=json.loads(request['input'][0]['content'])['context']
 for field in ['pending','awaiting','latest_question','source_context','entry_ids']:assert sent[field]==ctx[field]
 assert all(s not in json.dumps(request) for s in ['owner_id','workbook_id','preview_digest'])
 r=h.send('😀'*1999,patch('update',month=9,date_source='เดือนก่อน'))
 assert r.status=='context_too_large' and len(h.port.requests)==1 and not h.sheets.entries


def test_partial_draft_amount_followup_and_explicit_question_are_single_call():
 h=Harness();r=h.send('รถสองแถวสายสวน',patch(item='รถสองแถวสายสวน'))
 assert r.status=='clarification' and h.state.pending.payload['loop_context']['awaiting']['field']=='amount'
 r=h.send('35 บาทค่ะ',patch('update',amount='35'));assert full(h)==expected()
 old=deepcopy(r);r=h.send('ขอแก้ยอดหน่อย',envelope('clarify',question_field='amount'))
 assert r.status=='clarification' and r.message=='จำนวนเงินเท่าไร?' and h.click(old,'บันทึก').status=='stale'
 ctx=h.service.loop_context(h.sheets.taxonomy());assert ctx['awaiting']['field']=='amount' and ctx['latest_question']==r.message
 r=h.send('50 บาท',patch('update',amount='50'));assert full(h)==expected(amount='50') and len(h.port.requests)==4


def test_relative_year_is_not_a_license_to_invent_month():
 h=Harness();h.draft();r=h.send('ปีที่แล้ว',patch('update',year=2025,date_source='ปีที่แล้ว'))
 assert r.status=='preview' and full(h)==expected(date='2025-10-15')
 before=full(h)['entries'];r=h.send('ปีนี้',patch('update',month=9,year=2026,date_source='ปีนี้'))
 assert r.status=='clarification' and full(h)['entries']==before
 assert h.service.loop.trace[0]['outcome']['error']['code']=='month_not_supplied'


def test_partial_relative_day_cannot_leave_a_wrong_complete_date():
 h=Harness();h.draft('วันที่ 4 กันยายน รถสองแถวสายสวน 35 บาท',day=4,month=9,date_source='วันที่ 4 กันยายน')
 old=full(h)['entries'];r=h.send('แก้เป็นวันนี้',patch('update',day=15,date_source='วันนี้'))
 assert r.status=='clarification' and full(h)['entries']==old
 assert h.service.loop.trace[0]['outcome']['error']['code']=='relative_date_incomplete'


def test_multiple_rows_and_named_target_keep_complete_fields_separate():
 h=Harness();p=envelope(mode='create',entries=[patch(item='รถสองแถวสายสวน',amount='35',note='ใบเสร็จ')['entries'][0],
  patch(item='สมุดปกคราม',amount='72',note='โปรงานทีม')['entries'][0]])
 r=h.send('รถสองแถวสายสวน 35 บาท หมายเหตุ ใบเสร็จ\nสมุดปกคราม 72 บาท หมายเหตุ โปรงานทีม',p)
 assert r.status=='preview' and full(h)['entries']==[expected(note='ใบเสร็จ')['entries'][0],expected(item='สมุดปกคราม',amount='72',note='โปรงานทีม',category='Work/Education')['entries'][0]]
 r=h.send('แก้ยอดสมุดปกครามเป็น 80 บาท',patch('update',entry_id='@1',amount='80'))
 assert full(h)['entries']==[expected(note='ใบเสร็จ')['entries'][0],expected(item='สมุดปกคราม',amount='80',note='โปรงานทีม',category='Work/Education')['entries'][0]] and len(h.port.requests)==2


def test_receipt_replay_and_interrupted_checkpoint_do_not_pay_twice(tmp_path):
 path=tmp_path/'interrupted.sqlite';h=Harness(state=SQLiteState(path,'mock-owner'))
 event=h.event('รถสองแถวสายสวน 35 บาท');h.port.scripts=[patch(item='รถสองแถวสายสวน',amount='35')]
 class Crash(BaseException):pass
 h.service.phase_hook=lambda stage,key:(_ for _ in ()).throw(Crash()) if stage=='domain_tool_completed' else None
 with pytest.raises(Crash):h.service.handle(event)
 assert len(h.port.requests)==1 and full(h)==expected() and not h.sheets.entries
 h2=Harness(state=SQLiteState(path,'mock-owner'),sheets=h.sheets);h2.now=h.now;h2.counter=h.counter
 r=h2.service.handle(event);assert r.status=='preview' and not h2.port.requests
 assert h2.service.handle(event)==r and not h2.port.requests
 assert all(run.decision is None for run in h2.state.model_runs.values())


def test_feature_off_keeps_plain_snapshot_proofs_and_compatible_mock_review():
 from test_conversation import Harness as Current
 h=Harness();review=h.draft();snapshot=state_snapshot(h.state,'mock-owner');state=MemoryState();restore_state(state,deepcopy(snapshot),'mock-owner')
 current=Current(state=state,sheets=h.sheets);current.now=h.now;current.counter=h.counter
 assert current.click(review,'บันทึก').status=='recorded' and len(h.sheets.entries)==1
 assert state.model_runs==h.state.model_runs and state.model_budgets==h.state.model_budgets


@pytest.mark.parametrize('item',['สินค้ารุ่นวันที่ 4','ชุดโปรโมชั่นวันนี้'])
def test_date_words_inside_complete_item_are_not_date_evidence(item):
 sheets=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),[*ROWS,(item,'Household','')])
 h=Harness(sheets=sheets);r=h.send(item+' 35 บาท',patch(item=item,amount='35'))
 assert r.status=='preview' and full(h)==expected(item=item,category='Household')


def test_dates_inside_explicit_note_do_not_change_default_expense_date():
 h=Harness();note='เอกสารวันที่ 4 ตุลาและใบเสร็จเมื่อวาน'
 r=h.draft('รถสองแถวสายสวน 35 บาท หมายเหตุ '+note,note=note)
 assert r.status=='preview' and full(h)==expected(note=note)
