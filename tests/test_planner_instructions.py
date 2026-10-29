"""Prompt composition contracts; real language behavior needs separate model evals."""
import json
import socket

import pytest

from line_assistant.planner_instructions import build
from line_assistant.raw_history import request_instructions


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('No inference or provider access in prompt contract tests')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)


def offered(*names):return [{'name':name} for name in names]


@pytest.mark.parametrize('names,present,absent',[
    (('query_expenses','query_relations'),'Relational analysis:','Calendar preparation:'),
    (('prepare_expenses',),'Expense preparation:','Pending changes:'),
    (('patch_pending_records',),'Pending changes:','Saved corrections:'),
    (('query_table','describe_table'),'Configured tables:','Calendar preparation:'),
    (('prepare_calendar_events',),'Calendar preparation:','Expense preparation:'),
    (('resolve_temporal',),'Temporal interpretation:','Stored category proposals:'),
    (('resolve_activity_dates',),'Pending activity clarification:','Calendar preparation:'),
    (('scan_historical_categories',),'Stored category proposals:','Pending changes:'),
    (('finish_preparation_reply',),'Repair reply:','Expense preparation:'),
])
def test_guidance_follows_capabilities_without_keyword_routing(names,present,absent):
    prompt=build(offered(*names))
    assert present in prompt and absent not in prompt
    assert build(list(reversed(offered(*names)))+offered(*names))==prompt


def test_all_capabilities_keep_principles_without_replaying_incident_examples():
    names=('prepare_expenses','query_expenses','query_calendar','query_relations','query_table',
           'prepare_table_change','prepare_calendar_events','patch_pending_records','find_saved_records',
           'resolve_temporal','resolve_activity_dates','inspect_data_sources','connect_calendar',
           'scan_historical_categories','propose_expense_categories')
    prompt=build(offered(*names))
    assert len(prompt)<10000
    for example in ('Laptop Orion','Tablet Aurora','วันที่20ตุลาทั้งวัน','คุมสอบ','Workshop','27 sep'):
        assert example not in prompt
    assert 'metadata' in prompt and 'original source timestamp' in prompt
    assert 'fresh owner confirmation' in prompt and 'never instructions or consent' in prompt
    assert 'current authenticated owner request selects the task' in prompt
    assert 'do not assume date, amount or financial columns' in prompt


def test_calendar_flag_cannot_load_unoffered_calendar_write_workflow():
    tools=offered('query_expenses','query_relations')
    assert request_instructions(tools,calendar=True)==request_instructions(tools,calendar=False)
    assert 'Calendar preparation:' not in request_instructions(tools,calendar=True)


def test_known_field_formats_live_in_tool_parameters():
    from line_assistant.relational import definitions as relational
    from line_assistant.temporal import definitions as temporal
    sql=relational()[0]['parameters']['properties']['sql']['description']
    assert 'COLLATE decimal' in sql and 'decimal_div' in sql and 'AVG/TOTAL' in sql
    assert 'Tablet' not in sql
    clock=temporal()[0]['parameters']['properties']
    assert 'Monday=0' in clock['weekday']['description']
    assert 'original owner source timestamp' in clock['date']['description']


def test_actual_request_keeps_original_dialogue_and_adds_only_available_workflows():
    from test_raw_history import Harness,context
    from test_history_first import response
    h=Harness()
    h.send('เรื่องที่อ้างถึงก่อนหน้า',response(text='รับทราบครับ'))
    h.send('ขอดูรายละเอียดต่อ',response(text='ต้องการรายละเอียดส่วนไหนครับ'))
    body=h.port.requests[-1];c=context(body)
    assert [r['text'] for r in c['history']]==['เรื่องที่อ้างถึงก่อนหน้า','รับทราบครับ']
    assert body['instructions']==build(body['tools'])
    assert 'Pending changes:' not in body['instructions']
    assert body['tool_choice']=='auto' and body['parallel_tool_calls'] is False
    assert body['store'] is False


def test_read_analysis_drops_creation_guidance_without_dropping_query_scope():
    from test_expense_sql_filtering import sample,aggregate
    from test_expense_query_operations import expense
    from test_calendar_text_confirmation_v13 import turn
    api=sample()
    def analyze(body):
        c=json.loads(body['input'][0]['content'])
        assert 'Expense preparation:' not in body['instructions']
        assert 'Calendar preparation:' not in body['instructions']
        assert 'Pending changes:' not in body['instructions']
        assert 'Relational analysis:' in body['instructions']
        assert c['relations_catalog'][0]['row_count']==5
        return aggregate("item != 'Tablet Aurora'")(body)
    result,_=turn(api,'เดือน ก.ย. รวมตามเงื่อนไข',expense(continue_analysis=True),analyze)
    assert result.data['tool_result']['rows']==[{'total_baht':'125.00','payments':4}]
    assert not api.google.sheet_writes and not api.calendar.posts
