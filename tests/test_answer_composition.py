"""Flexible read answers keep source bindings and never acquire write authority.

Scripted documents test the contract, not fresh model language quality.
"""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import json, socket
import pytest
from fastapi.testclient import TestClient

from line_assistant import answer_composition as composition, response_realization as voice
from line_assistant.state import Result
from test_response_realization import VoiceHosted, generated
from test_calendar_text_confirmation_v13 import turn
from test_read_queries import plan, event
from test_expense_query_operations import seed, expense
from test_relational_queries import sql


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k): raise AssertionError('Synthetic answer composition only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def paragraph(text): return {'kind': 'text', 'template': text}
def rows(refs, text, heading=''): return {'kind': 'rows', 'record_refs': refs, 'heading': heading, 'template': text}
def document(*blocks): return {'blocks': list(blocks)}


def expense_view(*, one_day=True):
    view=composition.View()
    view.fact('เมื่อวาน (15 ต.ค. 2026)' if one_day else '15–16 ต.ค. 2026')
    view.fact('50.00 บาท'); view.fact('2 รายการ')
    for day, item, amount in [('2026-10-15','ชา','20.00'),('2026-10-15' if one_day else '2026-10-16','ขนม','30.00')]:
        view.row([('วันที่',day,'date',None),('รายการ',item,None,None),('ยอด',amount,None,'บาท')],
                 {'c1':['f1']} if one_day else {})
    return view.data()


@pytest.mark.parametrize('lead',[
    '{{f1}} ใช้ไป {{f2}} จาก {{f3}}ครับ',
    'รวม {{f2}}ครับ สำหรับ{{f1}} มี{{f3}} ดังนี้',
    '{{f1}}\nยอดรวม: {{f2}}\nจำนวน: {{f3}}',
])
def test_same_verified_data_supports_different_natural_layouts_without_a_vocabulary_list(lead):
    text=composition.render(document(paragraph(lead),rows(['r1','r2'],'• {{c2}} {{c3}}')),expense_view())
    assert text.count('15 ต.ค. 2026')==1
    assert 'ชา 20.00 บาท' in text and 'ขนม 30.00 บาท' in text


def test_multiday_records_can_share_adjacent_day_headings_without_repeating_per_row():
    view=expense_view(one_day=False)
    view['records'].append(deepcopy(view['records'][1]));view['records'][-1]['id']='r3'
    text=composition.render(document(paragraph('{{f1}} {{f2}} {{f3}}'),
        rows(['r1'],'- {{c2}} {{c3}}','{{c1}}:'),
        rows(['r2','r3'],'- {{c2}} {{c3}}','{{c1}}:')),view)
    assert text.count('16 ต.ค. 2026')==2  # once in the overall period, once in its heading
    assert text.count('ขนม 30.00 บาท')==2


@pytest.mark.parametrize('bad',[
    document(paragraph('{{f1}} {{f2}} {{f3}}')),
    document(paragraph('{{f1}} {{f2}}'),rows(['r1','r2'],'{{c2}} {{c3}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}}'),rows(['r1','r1'],'{{c2}} {{c3}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}}'),rows(['r2','r1'],'{{c2}} {{c3}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}}'),rows(['r1','r2'],'{{c2}} {{c4}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}}'),rows(['r1','r2'],'{{c2}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}}'),rows(['foreign'],'{{c2}} {{c3}}')),
    document(paragraph('{{f1}} {{invented}} {{f3}}'),rows(['r1','r2'],'{{c2}} {{c3}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}} 500 บาท'),rows(['r1','r2'],'{{c2}} {{c3}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}} ๕๐๐ บาท'),rows(['r1','r2'],'{{c2}} {{c3}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}} บันทึกเรียบร้อยแล้ว'),rows(['r1','r2'],'{{c2}} {{c3}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}} saved'),rows(['r1','r2'],'{{c2}} {{c3}}')),
    document(paragraph('{{f1}} {{f2}} {{f3}} กรุณายืนยัน'),rows(['r1','r2'],'{{c2}} {{c3}}')),
])
def test_missing_swapped_foreign_values_and_mutation_claims_are_rejected(bad):
    with pytest.raises(ValueError):composition.render(bad,expense_view())


def test_shared_date_heading_cannot_hide_rows_from_a_different_day():
    with pytest.raises(ValueError,match='unknown_answer_fact'):
        composition.render(document(paragraph('{{f1}} {{f2}} {{f3}}'),
            rows(['r1','r2'],'{{c2}} {{c3}}','{{c1}}')),expense_view(one_day=False))


def test_multiday_list_cannot_drop_dates_without_a_matching_shared_heading():
    with pytest.raises(ValueError,match='answer_cell_coverage'):
        composition.render(document(paragraph('{{f1}} {{f2}} {{f3}}'),rows(['r1','r2'],'{{c2}} {{c3}}')),expense_view(one_day=False))


def test_exact_literal_shared_heading_is_verified_against_all_members_without_retry():
    text=composition.render(document(paragraph('{{f1}} {{f2}} {{f3}}'),
        rows(['r1'],'{{c2}} {{c3}}','15 ต.ค. 2026'),
        rows(['r2'],'{{c2}} {{c3}}','16 ต.ค. 2026:')),expense_view(one_day=False))
    assert '16 ต.ค. 2026:\nขนม 30.00 บาท' in text
    with pytest.raises(ValueError):
        composition.render(document(paragraph('{{f1}} {{f2}} {{f3}}'),
            rows(['r1','r2'],'{{c2}} {{c3}}','15 ต.ค. 2026')),expense_view(one_day=False))


def test_matching_clipped_labels_cannot_establish_false_shared_context():
    view=composition.View()
    for name in ['x'*301+'A','x'*301+'B']:view.row([('ชื่อ',name,None,None),('ยอด','1',None,'บาท')])
    assert view.records[0]['fields']['c1']['display']==view.records[1]['fields']['c1']['display']
    with pytest.raises(ValueError):composition.render(document(rows(['r1','r2'],'{{c2}}','{{c1}}')),view.data())


def test_templates_in_source_values_are_never_executed_or_interpolated_again():
    view=composition.View();view.row([('ชื่อ','{{f1}} บันทึก 999',None,None)])
    assert composition.render(document(rows(['r1'],'{{c1}}')),view.data())=='{{f1}} บันทึก 999'


def test_single_day_signed_reply_uses_date_once_and_replay_is_exact_without_new_calls():
    api=VoiceHosted();seed(api,[('2026-10-15','ชา',20),('2026-10-15','ขนม',30)])
    def composed(body):
        assert voice.validate_body(body)
        p=json.loads(body['input'][0]['content']);assert p['answer_document'] and 'locked_rows' not in p
        return generated(body,document(paragraph('{{f1}} ใช้ไป {{f2}} จาก {{f3}}ครับ'),rows(['r1','r2'],'- {{c2}} {{c3}}')))
    api.voice=composed
    result,original=turn(api,'เมื่อวานจ่ายอะไรไปบ้าง',plan(start='2026-10-15',listing=True))
    assert result.data['response_realization']['status']=='llm',result
    assert result.message=='เมื่อวาน (15 ต.ค. 2026) ใช้ไป 50.00 บาท จาก 2 รายการครับ\n- ขนม 30.00 บาท\n- ชา 20.00 บาท'
    before=len(api.model_bodies)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(original)).status_code==200
    assert len(api.model_bodies)==before and not api.google.sheet_writes and not api.calendar.posts


def test_invalid_composition_uses_compact_single_day_fallback_and_does_not_retry():
    api=VoiceHosted();seed(api,[('2026-10-15','ชา',20),('2026-10-15','ขนม',30)])
    api.voice=lambda body:generated(body,document(paragraph('{{f1}} ใช้ไป 999 บาท')))
    result,_=turn(api,'เมื่อวานจ่ายอะไรไปบ้าง',plan(start='2026-10-15',listing=True))
    assert result.data['response_realization']['reason']=='invalid_output'
    assert result.message.count('15 ต.ค. 2026')==1 and '50.00 บาท' in result.message and '999' not in result.message
    assert len(api.voice_bodies)==1 and not api.google.sheet_writes


def test_calendar_group_heading_keeps_original_spanning_time_and_all_day_range():
    api=VoiceHosted();api.calendar.events={
        'a':event('a','2026-10-15T23:00:00+07:00','2026-10-17T01:00:00+07:00'),
        'b':event('b','2026-10-16','2026-10-18',all_day=True)}
    def composed(body):
        p=json.loads(body['input'][0]['content']);rs=p['answer_facts']['records']
        return generated(body,document(paragraph('{{f1}} มี {{f2}}ครับ'),
            rows([r['id'] for r in rs],'{{c3}} — {{c2}}')))
    api.voice=composed
    result,_=turn(api,'นัดวันนี้',plan('calendar','2026-10-16'))
    assert result.data['response_realization']['status']=='llm'
    assert 'เริ่ม 15 ต.ค. 2026 23:00 ถึง 17 ต.ค. 2026 01:00' in result.message
    assert 'ทั้งวัน (16 ต.ค. 2026 ถึง 17 ต.ค. 2026)' in result.message


def test_sql_scalar_answer_is_natural_prose_with_linked_computed_cells():
    api=VoiceHosted();seed(api,[('2026-09-01','ชา',20),('2026-09-02','ขนม',30)])
    api.voice=lambda body:generated(body,document(paragraph('{{f1}}'),
        rows(['r1'],'รวมใช้ไป {{c1}} บาท จาก {{c2}} รายการครับ')))
    result,_=turn(api,'รายจ่าย ก.ย. รวมเท่าไร',expense(continue_analysis=True),
        sql(lambda rs:f"SELECT SUM(amount) AS total,COUNT(*) AS n FROM {rs[0]['sql_table']}"))
    assert result.data['response_realization']['status']=='llm',result
    assert 'รวมใช้ไป 50.00 บาท จาก 2 รายการครับ' in result.message
    assert 'total:' not in result.message and 'ข้อมูลรายจ่าย:' not in result.message and not api.google.sheet_writes


def test_arbitrary_people_columns_need_no_expense_fields_or_per_table_formatter():
    host=SimpleNamespace(_current={'at':'2026-10-16T15:00:00+07:00'})
    output={'ok':True,'complete':True,'table_query':True,'domain':'table','label':'ผู้ติดต่อจำลอง',
            'count':1,'result_count':1,'rows':[{'id':'private-row-id','name':'คนจำลอง','phone':None,'active':False}],
            'shown':1,'truncated':False,'column_labels':{'name':'ชื่อ','phone':'เบอร์','active':'ติดต่อแล้ว'}}
    result=Result('answer','fallback',{'read_only':True,'tool_result':output})
    view=composition.for_result(host,result)
    assert view and 'private-row-id' not in json.dumps(view)
    assert composition.render(document(paragraph('{{f1}}'),rows(['r1'],'{{c1}} — เบอร์: {{c2}} — ติดต่อแล้ว: {{c3}}')),view).endswith('คนจำลอง — เบอร์: ว่าง — ติดต่อแล้ว: ไม่ใช่')


@pytest.mark.parametrize('status,complete,write', [('preview',True,False),('recorded',True,True),('answer',False,False),('summary',True,True)])
def test_incomplete_results_and_mutations_never_enter_the_free_read_layout(status,complete,write):
    host=SimpleNamespace(_current={'at':'2026-10-16T15:00:00+07:00'})
    r=Result(status,'authoritative',{'read_only':True,'financial_write_authorized':write,
        'tool_result':{'complete':complete,'domain':'expense'}})
    assert composition.for_result(host,r) is None


def test_new_body_cannot_add_tools_or_change_output_budget_outside_existing_reservation():
    api=VoiceHosted();turn(api,'ยอดวันนี้',plan())
    body=deepcopy(api.voice_bodies[-1]);assert voice.validate_body(body)
    body['tools']=[{'type':'function','name':'save'}];assert not voice.validate_body(body)
    body['tools']=[];body['max_output_tokens']+=1;assert not voice.validate_body(body)


def test_partial_activity_subtotal_cannot_drop_the_remaining_candidate_question():
    host=SimpleNamespace(_current={'at':'2026-10-16T15:00:00+07:00'})
    result=Result('summary','ยังเหลือนัดที่รอยืนยัน',{'read_only':True,'read_followup':{'unconfirmed_count':1},
        'tool_result':{'complete':True,'domain':'expense'}})
    assert composition.for_result(host,result) is None
