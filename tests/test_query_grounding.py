"""Queries preserve typed values, units and consent under hostile arguments/data."""
from copy import deepcopy
import json
import pytest
from test_read_queries import plan
from test_response_realization import VoiceHosted,generated
from test_calendar_text_confirmation_v13 import turn
from test_calendar_mvp import response
from line_assistant import response_realization as voice

@pytest.mark.parametrize('field,value',[('spreadsheet_id','other'),('calendar_id','other'),('sql','SELECT *'),('execute','anything'),('filter',{'kind':'category','value':'unknown'}),('source_refs',['msg_forged']),('start_date',True)])
def test_model_arguments_cannot_expand_read_surface(field,value):
    api=VoiceHosted()
    def malicious(body):
        answer=plan()(body);args=json.loads(answer['output'][0]['arguments']);args[field]=value
        answer['output'][0]['arguments']=json.dumps(args);return answer
    r,_=turn(api,'ขอดูรายจ่าย',malicious)
    assert not r.data['tool_result']['complete'] and r.data['tool_result']['error_code'] in {'invalid_arguments','filter_unknown'}
    assert not api.calendar.posts and not api.google.sheet_writes

@pytest.mark.parametrize('mutation',['drop','duplicate','unknown','money','date','name','saved','swapped_units'])
def test_query_wording_cannot_invent_facts_units_or_success(mutation):
    api=VoiceHosted()
    def bad(body):
        p=json.loads(body['input'][0]['content']);blocks=[{'kind':'text','template':'{{'+key+'}}'} for key in p['answer_facts']['facts']]
        if mutation=='drop':blocks.pop()
        elif mutation=='duplicate':blocks.append({'kind':'rows','record_refs':['r1','r1'],'heading':'','template':'{{c1}}'})
        elif mutation=='unknown':blocks[0]['template']='{{invented}}'
        else:blocks.insert(0,{'kind':'text','template':{'money':'900 บาท','date':'พรุ่งนี้ 2028','name':'{{new_name}}','saved':'บันทึกสำเร็จแล้ว','swapped_units':'{{total_as_items}}'}[mutation]})
        return generated(body,{'blocks':blocks})
    api.voice=bad;r,_=turn(api,'ยอดวันนี้',plan())
    assert r.data['response_realization']=={**r.data['response_realization'],'status':'fallback','reason':'invalid_output'}
    assert '0.00 บาท' in r.message and not api.google.sheet_writes


def test_query_fact_slots_compose_inline_natural_thai():
    api=VoiceHosted()
    def natural(body):
        p=json.loads(body['input'][0]['content']);assert p['answer_document'] and p['answer_facts']['facts']['f2']=='0.00 บาท'
        return generated(body,{'blocks':[{'kind':'text','template':'{{f1}} รายจ่ายรวม {{f2}} จาก {{f3}}ครับ'}]})
    api.voice=natural;r,_=turn(api,'วันนี้รวม',plan())
    assert r.data['response_realization']['status']=='llm' and r.message=='วันนี้ (16 ต.ค. 2026) รายจ่ายรวม 0.00 บาท จาก 0 รายการครับ'


def test_failed_realization_uses_natural_authoritative_fallback():
    api=VoiceHosted();api.voice=lambda body: (_ for _ in ()).throw(TimeoutError())
    r,_=turn(api,'วันนี้รวม',plan())
    assert r.data['response_realization']['status']=='fallback'
    assert r.message=='วันนี้ (16 ต.ค. 2026) ใช้ไป 0.00 บาท จาก 0 รายการครับ'


def test_query_surface_does_not_include_semantic_parser_or_legacy_competing_read():
    from line_assistant import read_queries as q
    assert not any(hasattr(q,name) for name in ['route','period','filter_scope','READ','FILLERS'])
    api=VoiceHosted();turn(api,'เมื่อวานจ่ายอะไรไปบ้าง',plan(start='2026-10-15',listing=True))
    request=next(b for b in api.model_bodies if b['tool_choice']=='auto')
    assert 'spending_summary' not in {t['name'] for t in request['tools']}
    context=json.loads(request['input'][0]['content'])
    assert context['bangkok_datetime'].endswith('+07:00') and context['query_catalog']['complete']
    assert 'กาแฟ' in context['query_catalog']['items']


@pytest.mark.parametrize('mode,offset,start,end',[
 ('calendar_month',0,'2026-10-01','2026-10-31'),('calendar_month',-1,'2026-09-01','2026-09-30'),
 ('calendar_month',1,'2026-11-01','2026-11-30'),('calendar_week',0,'2026-10-12','2026-10-18'),
 ('calendar_week',1,'2026-10-19','2026-10-25'),('calendar_week',-1,'2026-10-05','2026-10-11'),
 ('calendar_year',0,'2026-01-01','2026-12-31')])
def test_model_selects_relative_frame_server_only_calculates_calendar(mode,offset,start,end):
    api=VoiceHosted();r,_=turn(api,'ถ้อยคำที่โมเดลตีความ',plan(start=None,date_mode=mode,relative_offset=offset))
    assert r.data['read_query']['start']==start and r.data['read_query']['end']==end


@pytest.mark.parametrize('offset',[True,121,-121,'1'])
def test_invalid_relative_offsets_cannot_expand_query(offset):
    api=VoiceHosted();r,_=turn(api,'คำถาม',plan(start=None,date_mode='calendar_month',relative_offset=offset))
    assert r.data['tool_result']['error_code']=='invalid_arguments'


def test_calendar_filter_must_come_from_cited_owner_wording():
    api=VoiceHosted();r,_=turn(api,'เฉพาะนัดกีฬา',plan('calendar',filter_text='TA'))
    assert r.status=='clarification' and r.data['tool_result']['error_code']=='invalid_arguments'
    assert not r.data['tool_result']['complete'] and not api.calendar_requests


def test_referenced_records_preserve_every_appointment_without_copying_its_values():
    from test_read_queries import event
    api=VoiceHosted();api.calendar.events={'one':event('one','2026-10-17','2026-10-18',all_day=True)}
    def header(body):
        p=json.loads(body['input'][0]['content'])
        assert p['answer_document'] and 'locked_rows' not in p
        return generated(body,{'blocks':[{'kind':'text','template':'{{f1}} มี {{f2}}ครับ'},
            {'kind':'rows','record_refs':['r1'],'heading':'','template':'{{c2}} — {{c3}}'}]})
    api.voice=header;r,_=turn(api,'ดูนัด',plan('calendar','2026-10-17'))
    assert r.data['response_realization']['status']=='llm' and r.message.count('นัดจำลอง one')==1


@pytest.mark.parametrize('mode,start,end',[('calendar_month','2026-10-01','2026-10-31'),('calendar_week','2026-10-19','2026-10-25'),('calendar_year','2026-01-01','2026-12-31')])
def test_absolute_frame_needs_only_start_anchor_not_redundant_end(mode,start,end):
    api=VoiceHosted()
    def choose(body):
        v=plan(start=start,date_mode=mode)(body);args=json.loads(v['output'][0]['arguments']);args['end_date']=None
        v['output'][0]['arguments']=json.dumps(args);return v
    r,_=turn(api,'ช่วงที่โมเดลเลือก',choose)
    assert r.data['tool_result']['complete'] and r.data['read_query']['end']==end



def test_query_schema_enums_do_not_contain_provider_rejected_control_literals():
    schema=voice.query_schema(['period','count','upcoming','truncation'])
    def inspect(value):
        if isinstance(value,dict):
            if 'enum' in value:assert all(not isinstance(v,str) or not any(ord(c)<32 for c in v) for v in value['enum'])
            for v in value.values():inspect(v)
        elif isinstance(value,list):
            for v in value:inspect(v)
    inspect(schema)



def test_model_can_select_verified_previous_period_without_copying_dates():
    api=VoiceHosted();turn(api,'เดือนนี้รวม',plan(start=None,date_mode='calendar_month',relative_offset=0))
    r,_=turn(api,'แยกรายการ',plan(start=None,date_mode='previous',listing=True))
    assert r.data['read_query']['start']=='2026-10-01' and r.data['read_query']['end']=='2026-10-31'
    assert r.data['read_query']['listing'] is True


def test_previous_period_without_a_verified_scope_never_defaults():
    api=VoiceHosted();r,_=turn(api,'แยกรายการ',plan(start=None,date_mode='previous',listing=True))
    assert r.data['tool_result']['error_code']=='date_required' and not r.data['tool_result']['complete']



def test_current_clock_is_not_evidence_for_an_omitted_owner_period():
    api=VoiceHosted()
    def choose(body):
        v=plan()(body);args=json.loads(v['output'][0]['arguments']);args['period_text']=None
        v['output'][0]['arguments']=json.dumps(args);return v
    r,_=turn(api,'สรุปรายจ่ายหน่อย',choose)
    assert r.status=='clarification' and r.data['tool_result']['error_code']=='date_required'


def test_model_selected_upcoming_mode_computes_the_bounded_window():
    api=VoiceHosted();r,_=turn(api,'นัดถัดไป',plan('calendar',start=None,date_mode='upcoming'))
    assert r.data['read_query']['start']=='2026-10-16' and r.data['read_query']['end']=='2026-10-22'
    assert r.data['read_query']['upcoming'] is True
