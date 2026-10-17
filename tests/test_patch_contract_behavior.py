"""Synthetic typed contracts; syntax is not authorization.

All providers are scripted and offline.
"""
from pathlib import Path
from copy import deepcopy
import itertools,json,re,socket
import pytest
from line_assistant.draft_patch import Proposal, NAME, tools
from test_draft_patch import Harness,patch,envelope,change,expected,full
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def offline(monkeypatch):
 def deny(*a,**k):raise AssertionError('Contract behavior must stay offline')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)



def test_api_valid_integer_amount_has_a_normalized_decimal_contract():
 p=Proposal.model_validate(patch(item='สมุดทดสอบ',amount=47))
 assert next(c.value for c in p.entries[0].changes if c.field=='amount')=='47'
 assert p.edit().entries[0].amount=='47'

@pytest.mark.parametrize('intent',['draft','lookup','summary','clarify'])
@pytest.mark.parametrize('question',[None,'item','category','ambiguous'])
def test_question_hints_and_null_defaults_do_not_create_technical_shape_errors(intent,question):
 # All these are admitted by the API schema. Semantic sufficiency is separate.
 value=envelope(intent,question_field=question)
 if intent=='draft':value['entries']=patch(item='รถสองแถวสายสวน',amount=35)['entries']
 if intent=='lookup':value['lookup']={'kind':'item','query':'รถ','offset':0}
 p=Proposal.model_validate(value)
 assert p.question_field==question


def test_normalized_date_parts_do_not_require_iso_to_appear_in_thai_source():
 h=Harness();r=h.send('รถสองแถวสายสวน 35 บาท วันที่ 4',patch(day=4,date_source='2026-10-15',item='รถสองแถวสายสวน',amount='35'))
 assert r.status=='clarification' and full(h)==expected(date=None,date_parts={'day':4},date_required=True,ready=False)
 r=h.send('เดือนก่อนจ้า',patch('update',month=9,year=2026,date_source='2026-09-04'))
 assert r.status=='preview' and full(h)==expected(date='2026-09-04')


def test_literal_evidence_is_separate_and_checked_against_owner_text():
 h=Harness();p=patch(item='รถสองแถวสายสวน',amount='35',day=4)
 p['entries'][0]['date_evidence']='วันที่ 4'
 r=h.send('รถสองแถวสายสวน 35 บาท วันที่ 4',p)
 assert full(h)==expected(date=None,date_parts={'day':4},date_required=True,ready=False)
 bad=patch('update',month=9);bad['entries'][0]['date_evidence']='กันยายนที่ไม่มีในข้อความ'
 r=h.send('ตุลา',bad)
 assert r.status=='clarification' and not h.sheets.entries
 assert h.state.pending.payload['entries'][0]['date_parts']=={'day':4}


def test_wrong_semantic_month_is_business_rejection_with_facts_preserved():
 h=Harness();h.draft('รถสองแถวสายสวน 35 บาท วันที่ 4',day=4,date_source='วันที่ 4');facts=full(h)['entries']
 r=h.send('กันยา',patch('update',month=10))
 assert r.status=='clarification' and full(h)['entries']==facts and not h.sheets.entries
 assert h.service.loop.trace[0]['outcome']['error']['code']=='date_parts_mismatch'


def test_repeated_date_parts_from_validated_draft_need_no_new_date_words():
 h=Harness();h.draft('รถสองแถวสายสวน 35 บาท วันที่ 4 กันยายน หมายเหตุ ใบเสร็จ',day=4,month=9,date_source='วันที่ 4 กันยายน',note='ใบเสร็จ')
 r=h.send('ล้างหมายเหตุเดิม',patch('update',day=4,month=9,year=2026,ops=[change('note',None,'clear')]))
 assert r.status=='preview' and full(h)==expected(date='2026-09-04')


def test_supplied_context_id_is_data_checked_against_source_not_owner_consent():
 h=Harness()
 def body_value(body):
  ctx=json.loads(body['input'][0]['content'])['context'];row=next(r for r in ctx['choices']['items'] if r['label']=='สมุดปกคราม')
  p=patch(item='สมุดปกคราม',amount=72);p['entries'][0]['changes'].append(change('item_id',row['id']))
  return p
 r=h.send('สมุดปกคราม 72 บาท',body_value)
 assert r.status=='preview' and full(h)==expected(item='สมุดปกคราม',amount='72',category='Work/Education') and not h.sheets.entries


def test_lookup_question_hint_returns_labels_without_spending_summary_or_extra_model_call():
 h=Harness();r=h.send('ค้นหาหมวดเพิ่มเติม',envelope('lookup',lookup={'kind':'category','query':'','offset':8},question_field='category'))
 assert r.status=='clarification' and 'ดูแลสุขภาพ' in r.message and len(h.port.requests)==1 and not h.sheets.entries


def test_explicit_read_only_lookup_can_display_choices_already_in_context():
 h=Harness();r=h.send('ค้นหารายการรถ',envelope('lookup',lookup={'kind':'item','query':'รถ','offset':0}))
 assert r.status=='clarification' and 'รถสองแถวสายสวน' in r.message and len(h.port.requests)==1 and not h.sheets.entries
 assert h.service.loop.trace[0]['outcome']['ok']


def test_legacy_lookup_presentation_has_no_financial_authority():
 h=Harness();h.service._patch_text='ค้นหาหมวดเพิ่มเติม';h.service.loop.admitted_choices={'items':[],'categories':[]}
 tax=h.sheets.taxonomy();h.service._search('category_search',__import__('line_assistant.domain_tools',fromlist=['Search']).Search(query='',offset=8),tax)
 from test_domain_tool_loop import response
 value=response(NAME,envelope('clarify'))
 outcome,result=h.service.domain_call(value['output'][0],'ค้นหาหมวดเพิ่มเติม',1,h.now,tax)
 assert result.status=='clarification' and 'ดูแลสุขภาพ' in result.message and not h.sheets.entries


def schema_valid(value,schema):
 """Small independent checker for every keyword used by this local schema."""
 if 'anyOf' in schema:return any(schema_valid(value,branch) for branch in schema['anyOf'])
 kinds={'null':lambda v:v is None,'string':lambda v:isinstance(v,str),
        'integer':lambda v:type(v) is int,'object':lambda v:isinstance(v,dict),'array':lambda v:isinstance(v,list)}
 types=schema.get('type',list(kinds));types=[types] if isinstance(types,str) else types
 if not any(kinds[t](value) for t in types):return False
 if 'enum' in schema and value not in schema['enum']:return False
 if isinstance(value,str):
  if len(value)>schema.get('maxLength',len(value)):return False
  if 'pattern' in schema and not re.search(schema['pattern'],value):return False
 if type(value) is int and not schema.get('minimum',value)<=value<=schema.get('maximum',value):return False
 if isinstance(value,list):
  if len(value)>schema.get('maxItems',len(value)):return False
  if not all(schema_valid(v,schema['items']) for v in value):return False
 if isinstance(value,dict):
  properties=schema['properties']
  if not set(schema['required'])<=set(value):return False
  if schema.get('additionalProperties') is False and set(value)-set(properties):return False
  if not all(schema_valid(v,properties[k]) for k,v in value.items()):return False
 return True


def test_generated_schema_valid_combinations_parse_without_conditional_type_gaps():
 schema=tools()[0]['parameters'];branches=schema['properties']['entries']['items']['properties']['changes']['items']['anyOf'];count=0
 for branch in branches:
  for field in branch['properties']['field']['enum']:
   kind=branch['properties']['value']['type'];op=branch['properties']['op']['enum'][0]
   value=None if kind=='null' else 47 if kind=='integer' else '47' if field=='amount' else 'ข้อมูล'
   for intent,question,mode in itertools.product(['draft','lookup','summary','clarify'],[None,'item','amount','date','category','subcategory','ambiguous'],[None,'create','update']):
    wire=envelope(intent,mode=mode,entries=[{'entry_id':None,'date_source':None,'date_evidence':None,'result_set_id':None,'changes':[change(field,value,op)]}],question_field=question)
    wire['mode']=mode
    assert schema_valid(wire,schema),(field,op,intent,question,mode)
    Proposal.model_validate_json(json.dumps(wire,ensure_ascii=False));count+=1
 assert count==2016
 # The schema checker itself catches a known cross-type bug and bound mismatch.
 wrong=deepcopy(wire);wrong['entries'][0]['changes']=[change('day','47')]
 assert not schema_valid(wrong,schema)
 wrong=deepcopy(wire);wrong['lookup']={'kind':'item','query':'','offset':-1}
 assert not schema_valid(wrong,schema)


def test_duplicate_changes_are_semantic_rejection_not_provider_format_failure():
 h=Harness();h.draft();facts=full(h)['entries'];p=patch('update',amount=50)
 p['entries'][0]['changes']*=2
 r=h.send('แก้ยอดเป็น 50 บาท',p)
 assert r.status=='clarification' and full(h)['entries']==facts and not h.sheets.entries
 assert h.service.loop.trace[0]['outcome']['error']['code']=='duplicate_patch_field'


@pytest.mark.parametrize('value',[35,'35','35.00'])
def test_amount_representations_preserve_business_value_without_committing(value):
 h=Harness();r=h.send('รถสองแถวสายสวน 35 บาท',patch(item='รถสองแถวสายสวน',amount=value))
 assert r.status=='preview' and full(h)==expected() and not h.sheets.entries


@pytest.mark.parametrize('representation',['omitted','set_null','unchanged','unchanged_legacy_value'])
def test_harmless_null_keep_never_clears_validated_facts(representation):
 h=Harness();h.draft('รถสองแถวสายสวน 35 บาท หมายเหตุ เก็บไว้',note='เก็บไว้');facts=full(h)
 ops=[] if representation=='omitted' else [change('note',None if representation in ['set_null','unchanged'] else 'ข้อมูลที่ไม่ใช้','set' if representation=='set_null' else 'unchanged')]
 r=h.send('ขอดูร่าง',patch('update',ops=ops))
 assert r.status=='preview' and full(h)==facts and not h.sheets.entries


@pytest.mark.parametrize('text,month',[('กันยา',9),('Sept',9),('Sep',9),('ตุลาคม',10),('เดือน 9',9)])
def test_owner_month_semantics_preserve_partial_date(text,month):
 h=Harness();h.draft('รถสองแถวสายสวน 35 บาท วันที่ 4',day=4)
 r=h.send(text,patch('update',month=month,year=2026))
 assert r.status=='preview' and full(h)==expected(date=f'2026-{month:02d}-04') and not h.sheets.entries


@pytest.mark.parametrize('text,fields',[('ไม่ได้ระบุวัน',{'day':4}),('หมายเหตุ วันที่ 4',{'day':4}),('ปีที่แล้ว',{'month':9}),('วันที่ 4',{'day':4,'month':9}),('วันที่ 4 กันยายน',{'day':4,'month':9,'year':2025})])
def test_unsupported_date_parts_do_not_complete_or_write(text,fields):
 h=Harness();h.draft();facts=full(h)['entries']
 r=h.send(text,patch('update',**fields))
 assert r.status=='clarification' and full(h)['entries']==facts and not full(h)['ready'] and not h.sheets.entries


def check_integer_contract():
 h=Harness();r=h.send('รถสองแถวสายสวน 35 บาท',patch(item='รถสองแถวสายสวน',amount=35))
 assert r.status=='preview' and full(h)==expected() and not h.sheets.entries

def check_date_contract():
 h=Harness();r=h.send('รถสองแถวสายสวน 35 บาท วันที่ 4',patch(item='รถสองแถวสายสวน',amount='35',day=4,date_source='2026-10-15'))
 assert r.status=='clarification' and full(h)==expected(date=None,date_parts={'day':4},date_required=True,ready=False)

def check_mixed_confirmation():
 h=Harness();h.draft();r=h.send('บันทึกเลย แต่แก้ยอดเป็น 88 บาท',patch('update',amount='88'))
 assert r.status=='preview' and full(h)==expected(amount='88') and not h.sheets.entries

def test_negative_control_reintroducing_integer_rejection_is_detected(monkeypatch):
 from line_assistant.draft_patch import PatchPlanner
 normal=PatchPlanner.parse_response
 def old(self,response):
  call=normal(self,response)
  if any(c['field']=='amount' and type(c['value']) is int for e in json.loads(call['arguments'])['entries'] for c in e['changes']):raise ValueError('old integer rejection')
  return call
 check_integer_contract()
 with monkeypatch.context() as m:
  m.setattr(PatchPlanner,'parse_response',old)
  with pytest.raises(AssertionError):check_integer_contract()

def test_negative_control_reintroducing_iso_evidence_requirement_is_detected(monkeypatch):
 from line_assistant.draft_patch import DraftPatchAssistant
 def old(self,edit,proposal,*args):
  return edit.model_copy(update={'entries':[e.model_copy(update={'date_source':p.date_source}) for e,p in zip(edit.entries,proposal.entries)]})
 check_date_contract()
 with monkeypatch.context() as m:
  m.setattr(DraftPatchAssistant,'_date_patch',old)
  with pytest.raises(AssertionError):check_date_contract()

def test_negative_control_reintroducing_mixed_confirmation_commit_is_detected(monkeypatch):
 from line_assistant.draft_patch import DraftPatchAssistant
 check_mixed_confirmation()
 with monkeypatch.context() as m:
  m.setattr(DraftPatchAssistant,'confirmation',r'บันทึก.*')
  with pytest.raises(AssertionError):check_mixed_confirmation()




def test_read_only_summary_error_preserves_business_draft_and_review_exactly():
 h=Harness();h.draft();before=deepcopy(h.state.pending)
 r=h.send('ขอสรุปรายจ่ายวันนี้',envelope('summary',start_date='2025-10-15',end_date='2025-10-15'))
 assert r.status=='clarification' and h.state.pending==before and not h.sheets.entries
 assert h.service.loop.trace[0]['outcome']['error']['code']=='summary_range_mismatch'


def test_lookup_ids_and_model_category_never_authorize_new_item_enrollment():
 h=Harness()
 def propose(body):
  ctx=json.loads(body['input'][0]['content'])['context'];cat=next(c for c in ctx['choices']['categories'] if c['label']=='Transportation')
  p=patch(item='ตั๋วสมมติใหม่',amount=35);p['entries'][0]['changes'] += [change('category_id',cat['id']),change('subcategory_id',cat['subcategories'][0]['id'])]
  return p
 r=h.send('ตั๋วสมมติใหม่ 35 บาท',propose)
 assert r.status=='clarification' and (full(h) is None or not full(h)['ready']) and not h.sheets.entries
 assert h.service.loop.trace[0]['outcome']['error']['code']=='category_not_owner_choice'


def test_unknown_context_id_cannot_replace_source_item():
 h=Harness();p=patch(item='รถสองแถวสายสวน',amount=35,item_id='item_invented')
 r=h.send('รถสองแถวสายสวน 35 บาท',p)
 assert r.status=='clarification' and full(h) is None and not h.sheets.entries
 assert h.service.loop.trace[0]['outcome']['error']['code']=='unknown_context_choice'


def test_context_mapping_changed_during_response_is_rejected():
 h=Harness()
 def propose(body):
  ctx=json.loads(body['input'][0]['content'])['context'];row=next(c for c in ctx['choices']['items'] if c['label']=='รถสองแถวสายสวน')
  h.sheets.rows[0]=('รถสองแถวสายสวน','ดูแลสุขภาพ','')
  return patch(item='รถสองแถวสายสวน',amount=35,item_id=row['id'])
 r=h.send('รถสองแถวสายสวน 35 บาท',propose)
 assert r.status=='clarification' and full(h) is None and not h.sheets.entries
 assert h.service.loop.trace[0]['outcome']['error']['code']=='context_choices_changed'




def test_negative_control_reintroducing_exclusive_intent_hint_rule_is_detected(monkeypatch):
 from line_assistant.draft_patch import PatchPlanner
 normal=PatchPlanner.parse_response
 def check():
  h=Harness();p=patch(item='รถสองแถวสายสวน',amount=35);p['question_field']='amount'
  assert h.send('รถสองแถวสายสวน 35 บาท',p).status=='preview' and full(h)==expected() and not h.sheets.entries
 def old(self,response):
  call=normal(self,response);args=json.loads(call['arguments'])
  if args['intent']=='draft' and args['question_field'] is not None:raise ValueError('old exclusive shape')
  return call
 check()
 with monkeypatch.context() as m:
  m.setattr(PatchPlanner,'parse_response',old)
  with pytest.raises(AssertionError):check()


def test_negative_control_bypassing_month_provenance_is_detected(monkeypatch):
 from line_assistant.draft_patch import DraftPatchAssistant
 normal=DraftPatchAssistant._date_patch
 def check():
  h=Harness();h.draft('รถสองแถวสายสวน 35 บาท วันที่ 4',day=4);facts=full(h)['entries']
  r=h.send('กันยา',patch('update',month=10,date_source='กันยา'))
  assert r.status=='clarification' and full(h)['entries']==facts and not full(h)['ready'] and not h.sheets.entries
 def bypass(self,edit,proposal,*args):
  if not any(e.month is not None for e in edit.entries):return normal(self,edit,proposal,*args)
  return edit.model_copy(update={'entries':[e.model_copy(update={'date_source':p.date_source}) for e,p in zip(edit.entries,proposal.entries)]})
 check()
 with monkeypatch.context() as m:
  m.setattr(DraftPatchAssistant,'_date_patch',bypass)
  with pytest.raises(AssertionError):check()


def test_negative_control_reintroducing_ban_on_admitted_item_id_is_detected(monkeypatch):
 from line_assistant.draft_patch import DraftPatchAssistant
 from line_assistant.domain_runtime import ToolError
 normal=DraftPatchAssistant._context_refs
 def check():test_supplied_context_id_is_data_checked_against_source_not_owner_consent()
 def old(self,edit,taxonomy):
  if any(e.item_id is not None and e.result_set_id is None for e in edit.entries):raise ToolError('unknown_result_set')
  return normal(self,edit,taxonomy)
 check()
 with monkeypatch.context() as m:
  m.setattr(DraftPatchAssistant,'_context_refs',old)
  with pytest.raises(AssertionError):check()
