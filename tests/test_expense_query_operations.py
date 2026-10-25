"""Grounded ranking/date joins through signed synthetic webhooks.

Scripted model choices exercise contracts; they do not measure live planning.
"""
from copy import deepcopy
from datetime import date
import json,socket
import pytest
from fastapi.testclient import TestClient
from test_calendar_semantic_queries import SemanticHosted
from test_query_analysis_loop import read,results,finish
from test_calendar_text_confirmation_v13 import turn
from test_read_queries import plan,event
from test_calendar_mvp import response
from line_assistant.sheet_dates import date_to_serial

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Only synthetic query operations')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def seed(api,rows):
    for index,(day,item,amount) in enumerate(rows,2):
        for col,value in enumerate([date_to_serial(date.fromisoformat(day)),item,amount,''],1):
            api.google.cells[('All Expenses',index,col)]=value

def expense(start='2026-09-01',end='2026-09-30',*,join=False,**options):
    def choose(body):
        date_mode=options.get('date_mode','range')
        value=plan('expense',start,end,listing=True,date_mode=date_mode)(body)
        call=value['output'][0];args=json.loads(call['arguments'])
        args.update(options)
        if join:args['calendar_query_refs']=[r['query_ref'] for r in results(body) if r.get('domain')=='calendar']
        call.update(arguments=json.dumps(args,ensure_ascii=False),call_id='expense_read')
        return value
    return choose

def test_numeric_sort_uses_all_entries_before_top_limit_and_preserves_full_total():
    api=SemanticHosted()
    seed(api,[('2026-09-01','กาแฟ',i) for i in range(1,24)]+[('2026-09-30','รถไฟ',900)])
    result,_=turn(api,'เดือนที่แล้วเรียงรายการยอดมากไปน้อย',expense(order_by='amount',descending=True,limit=2))
    output=result.data['tool_result']
    assert [e['amount'] for e in output['entries']]==['900.00','23.00']
    assert output['expense']=='1176.00' and output['count']==24
    assert 'เรียงตามยอดเงินจากมากไปน้อย' in result.message and 'แสดง 2 รายการแรก' in result.message

def test_item_ranking_groups_all_payments_before_sorting_not_largest_single_payment():
    api=SemanticHosted();seed(api,[('2026-09-01','กาแฟ',400),('2026-09-02','กาแฟ',400),('2026-09-03','รถไฟ',700)])
    result,_=turn(api,'เดือนที่แล้วใช้ไรเยอะสุด',expense(group_by='item',order_by='amount',descending=True))
    output=result.data['tool_result']
    assert output['groups']==[{'label':'กาแฟ','amount':'800.00','count':2},{'label':'รถไฟ','amount':'700.00','count':1}]
    assert output['expense']=='1500.00' and output['count']==3
    assert 'กาแฟ — 800 บาท' in result.message and '2 รายการ' in result.message

@pytest.mark.parametrize('options,labels',[
    ({'order_by':'amount'},['B','A','C']),
    ({'order_by':'label','descending':True},['C','B','A']),
    ({'order_by':'date','descending':True},['C','B','A']),
])
def test_individual_sort_modes_are_explicit_and_deterministic(options,labels):
    api=SemanticHosted();seed(api,[('2026-09-01','A',20),('2026-09-02','B',3),('2026-09-03','C',100)])
    result,_=turn(api,'รายจ่ายเดือน ก.ย.',expense(**options))
    assert [e['item'] for e in result.data['tool_result']['entries']]==labels

def test_date_groups_use_decimal_sum_and_count_order():
    api=SemanticHosted();seed(api,[('2026-09-01','A',0.1),('2026-09-01','B',0.2),('2026-09-02','C',100)])
    result,_=turn(api,'เดือน ก.ย. แยกวันเรียงจำนวน',expense(group_by='date',order_by='count',descending=True))
    assert result.data['tool_result']['groups'][0]=={'label':'2026-09-01','amount':'0.30','count':2}
    assert '1 ก.ย. 2026 — 0.30 บาท' in result.message and '2 รายการ' in result.message

@pytest.mark.parametrize('grouping',['category','subcategory'])
def test_unmapped_category_group_never_claims_complete_ranking(grouping):
    api=SemanticHosted();seed(api,[('2026-09-01','unknown-synthetic-item',700)])
    result,_=turn(api,'เดือน ก.ย. แยกหมวด',expense(group_by=grouping,order_by='amount',descending=True))
    assert not result.data['tool_result']['complete'] and result.data['tool_result']['error_code']=='mapping_incomplete'
    assert '0.00 บาท' not in result.message

@pytest.mark.parametrize('grouping,first',[('category','Food'),('subcategory','Drink')])
def test_grouped_categories_use_current_verified_mapping_and_exact_sums(grouping,first):
    api=SemanticHosted()
    for index,values in enumerate([['กาแฟ','Food','Drink'],['Tea','Food','Drink'],['Rail','Transportation','Train']],2):
        for col,value in enumerate(values,1):api.google.cells[('Database',index,col)]=value
    seed(api,[('2026-09-01','กาแฟ',35),('2026-09-02','Tea',40),('2026-09-03','Rail',50)])
    result,_=turn(api,'เดือน ก.ย. แยกหมวดเรียงยอด',expense(group_by=grouping,order_by='amount',descending=True))
    assert result.data['tool_result']['groups'][0]=={'label':first,'amount':'75.00','count':2}
    assert result.data['tool_result']['expense']=='125.00'

@pytest.mark.parametrize('options',[
    {'order_by':'count'}, {'group_by':'item'}, {'limit':0},{'limit':21},{'limit':True},{'order_by':'arbitrary_sql'},
    {'view':'total','group_by':'item'},
])
def test_invalid_operations_are_rejected_instead_of_silently_showing_chronological_rows(options):
    api=SemanticHosted();seed(api,[('2026-09-01','กาแฟ',700)])
    result,_=turn(api,'เดือน ก.ย. เรียงรายการ',expense(**options))
    # Schema errors stay invalid; a conflicting view/sort/group goes back to the planner.
    assert result.data['tool_result']['error_code'] in {'invalid_arguments','expense_view_conflict'} and 'entries' not in result.data['tool_result']


def test_a_total_ignores_sort_flags_instead_of_refusing_the_read():
    api=SemanticHosted();seed(api,[('2026-09-01','กาแฟ',700),('2026-09-02','ชา',50)])
    result,_=turn(api,'เดือน ก.ย. รวมเท่าไหร่',expense(view='total',order_by='amount',descending=True))
    output=result.data['tool_result']
    assert output['complete'] and output['expense']=='750.00' and not output['entries'] and 'order_by' not in output['scope']

def test_sort_followup_keeps_verified_month_across_clarification_and_yes():
    api=SemanticHosted();seed(api,[('2026-09-01','A',1),('2026-09-30','B',100)])
    turn(api,'เดือนที่แล้วใช้เท่าไหร่',expense(date_mode='calendar_month',relative_offset=-1))
    turn(api,'sort max ไม่ได้หรอ',response(text='ต้องการเรียงยอดมากไปน้อยใช่ไหมครับ'))
    def choose(body):
        payload=json.loads(body['input'][0]['content'])
        assert payload['previous_query']['start']=='2026-09-01' and payload['previous_query']['end']=='2026-09-30'
        assert payload['query_capabilities']['sort_before_limit']
        return expense(start=None,end=None,date_mode='previous',order_by='amount',descending=True)(body)
    result,_=turn(api,'ใช่',choose)
    assert result.data['tool_result']['entries'][0]['item']=='B'
    assert result.data['read_query']['end']=='2026-09-30' and 'ช่วงวันไหน' not in result.message

def test_unverified_delivery_never_supplies_previous_period():
    api=SemanticHosted();api.reject_reply=True
    turn(api,'เดือน ก.ย.',expense());api.reject_reply=False
    result,_=turn(api,'เรียงยอด',expense(start=None,end=None,date_mode='previous',order_by='amount',descending=True))
    assert result.data['tool_result']['error_code']=='date_required'

@pytest.mark.parametrize('claim',['ผมเรียงให้ใหม่ได้ครับ ต้องการเรียงจากมากไปน้อยใช่ไหมครับ',
    'ผมจัดอันดับให้แล้วครับ','เรียงรายจ่ายเสร็จแล้วครับ'])
def test_free_model_prose_cannot_promise_or_claim_query_execution_without_a_read(claim):
    api=SemanticHosted();result,_=turn(api,'sort max ไม่ได้หรอ',response(text=claim))
    assert result.status=='clarification' and result.message=='รอบนี้ยังไม่ได้ค้นหรือจัดเรียงข้อมูลใหม่ครับ'

def test_honest_unsupported_operation_reply_is_preserved():
    api=SemanticHosted();text='ผมคำนวณค่ามัธยฐานให้ไม่ได้ครับ ยังไม่รองรับการคำนวณนี้'
    result,_=turn(api,'ค่ามัธยฐานล่ะ',response(text=text))
    assert result.message==text

def calendar(api,*,uncertain=False):
    api.calendar.events={'a':event('a','2026-09-03','2026-09-04',all_day=True,summary='EXAM invigilation'),
                         'b':event('b','2026-09-03T09:00:00+07:00','2026-09-03T10:00:00+07:00',summary='EXAM supervision'),
                         'c':event('c','2026-09-10','2026-09-11',all_day=True,summary='EXAM duty'),
                         'd':event('d','2026-09-11','2026-09-12',all_day=True,summary='Dental check')}
    api.labels={'e1':'match','e2':'match','e3':'uncertain' if uncertain else 'match','e4':'exclude'}
    return read('calendar','2026-09-01',date_mode='calendar_month',filter_text='คุมสอบ',identifier='calendar_read')

def test_activity_spending_joins_unique_calendar_dates_and_sums_all_matching_payments():
    api=SemanticHosted();cal=calendar(api)
    seed(api,[('2026-09-03','A',35),('2026-09-03','B',10),('2026-09-10','A',40),('2026-09-11','A',999)])
    result,original=turn(api,'เดือนที่แล้วที่ไปคุมสอบผมใช้ตังไปเท่าไหร่',cal,
        expense(start=None,end=None,date_mode='previous',view='total',join=True))
    output=result.data['tool_result'];basis=output['scope']['calendar_basis']
    assert output['expense']=='85.00' and output['count']==3
    assert basis['dates']==['2026-09-03','2026-09-10'] and basis['event_count']==3
    assert 'เฉพาะวันที่มีนัดคุมสอบ' in result.message and '85 บาท' in result.message
    assert 'ไม่ได้แยกว่าเป็นค่าใช้จ่ายของนัดโดยตรง' in result.message
    before=(len(api.model_bodies),len(api.calendar_requests),len(api.extra_requests))
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(original)).status_code==200
    assert before==(len(api.model_bodies),len(api.calendar_requests),len(api.extra_requests))
    assert not api.google.sheet_writes and not api.calendar.posts and not api.snapshot()['actions']

def test_join_and_group_are_composable_with_grounded_finisher_and_followup_scope():
    api=SemanticHosted();cal=calendar(api)
    seed(api,[('2026-09-03','A',35),('2026-09-10','A',40),('2026-09-11','A',999)])
    result,_=turn(api,'เดือนที่แล้ววันที่คุมสอบใช้ไรเยอะสุด',cal,
        expense(start=None,end=None,date_mode='previous',group_by='item',order_by='amount',descending=True,continue_analysis=True,join=True),finish())
    assert 'A — 75 บาท' in result.message and '2 รายการ' in result.message
    assert result.data['read_query']['calendar_basis']['dates']==['2026-09-03','2026-09-10']
    # A later turn cannot silently broaden this conditional query to all days.
    result,_=turn(api,'ขอเรียงยอด',expense(start=None,end=None,date_mode='previous',order_by='amount',descending=True))
    assert result.data['tool_result']['error_code']=='calendar_basis_required' and '999' not in result.message

@pytest.mark.parametrize('fault',['uncertain','unknown_ref','duplicate_ref','coverage','cross_turn'])
def test_calendar_basis_failures_never_become_zero_or_an_unconditional_month_total(fault):
    api=SemanticHosted();cal=calendar(api,uncertain=fault=='uncertain')
    seed(api,[('2026-09-03','A',35),('2026-09-11','A',999)])
    old=[]
    if fault=='cross_turn':
        r,_=turn(api,'เดือนที่แล้วมีคุมสอบอะไรบ้าง',cal,finish());old=r.data['tool_result']['query_refs']
    def choose(body):
        value=expense(start='2026-09-01',end='2026-10-01' if fault=='coverage' else '2026-09-30',join=fault!='cross_turn')(body)
        call=value['output'][0];args=json.loads(call['arguments'])
        if fault=='unknown_ref':args['calendar_query_refs']=['read_forged']
        elif fault=='duplicate_ref':args['calendar_query_refs']*=2
        elif fault=='cross_turn':args['calendar_query_refs']=old
        call['arguments']=json.dumps(args);return value
    args=[] if fault=='cross_turn' else [cal]
    result,_=turn(api,'เดือนที่แล้ววันที่คุมสอบใช้ตังเท่าไหร่',*args,choose)
    output=result.data['tool_result']
    assert not output['complete'] and output['error_code'] in {'invalid_calendar_refs','incomplete_calendar_basis','calendar_scope_incomplete'}
    assert 'expense' not in output and '0.00 บาท' not in result.message

def test_spanning_events_use_bangkok_dates_and_exclude_midnight_end_day():
    api=SemanticHosted()
    api.calendar.events={'span':event('span','2026-09-03T17:00:00Z','2026-09-05T17:00:00Z',summary='EXAM duty'),
                         'day':event('day','2026-09-04','2026-09-06',all_day=True,summary='EXAM work')}
    seed(api,[('2026-09-03','A',100),('2026-09-04','A',1),('2026-09-05','A',2),('2026-09-06','A',200)])
    result,_=turn(api,'เดือน ก.ย. วันที่คุมสอบจ่ายเท่าไหร่',
        read('calendar','2026-09-01',date_mode='calendar_month',filter_text='คุมสอบ'),
        expense(start=None,end=None,date_mode='previous',view='total',join=True))
    assert result.data['tool_result']['expense']=='3.00'
    assert result.data['read_query']['calendar_basis']['dates']==['2026-09-04','2026-09-05']

def test_complete_empty_calendar_basis_proves_zero_matching_dates():
    api=SemanticHosted();seed(api,[('2026-09-01','A',999)])
    result,_=turn(api,'เดือน ก.ย. วันที่คุมสอบใช้เท่าไหร่',
        read('calendar','2026-09-01',date_mode='calendar_month',filter_text='คุมสอบ'),
        expense(start=None,end=None,date_mode='previous',view='total',join=True))
    assert result.data['tool_result']['complete'] and result.data['tool_result']['expense']=='0.00'
    assert result.data['read_query']['calendar_basis']['dates']==[]

def test_gap_between_complete_calendar_windows_cannot_prove_full_expense_period():
    api=SemanticHosted();seed(api,[('2026-09-11','A',999)])
    result,_=turn(api,'เดือน ก.ย. วันที่คุมสอบใช้เท่าไหร่',
        read('calendar','2026-09-01','2026-09-10',filter_text='คุมสอบ',identifier='one'),
        read('calendar','2026-09-12','2026-09-30',filter_text='คุมสอบ',identifier='two'),expense(join=True,view='total'))
    assert result.data['tool_result']['error_code']=='calendar_scope_incomplete'
    assert 'expense' not in result.data['tool_result']

def test_different_activity_windows_do_not_prove_full_month_of_either_condition():
    api=SemanticHosted()
    result,_=turn(api,'เดือน ก.ย. วันที่คุมสอบหรือสอนจ่ายเท่าไหร่',
        read('calendar','2026-09-01','2026-09-10',filter_text='คุมสอบ',identifier='one'),
        read('calendar','2026-09-11','2026-09-30',filter_text='สอน',identifier='two'),expense(join=True,view='total'))
    assert result.data['tool_result']['error_code']=='calendar_condition_mismatch'
