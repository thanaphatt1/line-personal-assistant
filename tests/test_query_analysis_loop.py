"""Synthetic uncapped read continuations; all arithmetic and provenance local."""
from copy import deepcopy
import json,socket
import pytest
from fastapi.testclient import TestClient
from line_assistant.sheet_dates import date_to_serial
from datetime import date
from test_calendar_semantic_queries import SemanticHosted,appointment
from test_calendar_text_confirmation_v13 import turn
from test_read_queries import plan
from test_calendar_mvp import response
from test_calendar_read_all import PERSONAL

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Only synthetic query analysis')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def read(domain,start,end=None,*,filter_text=None,identifier='read',**options):
    def choose(body):
        value=plan(domain,start,end,filter_text=filter_text,**options)(body)
        call=value['output'][0];args=json.loads(call['arguments']);args['continue_analysis']=True
        call.update(arguments=json.dumps(args,ensure_ascii=False),call_id=identifier)
        return value
    return choose

def results(body):return [json.loads(i['output']) for i in body['input'] if i.get('type')=='function_call_output']
def finish(operation='report',*,refs=None):
    def choose(body):
        values=[v for v in results(body) if v.get('query_ref')]
        p=json.loads(body['input'][0]['content'])
        return response('finish_query_analysis',{'query_refs':refs if refs is not None else [v['query_ref'] for v in values],
            'operation':operation,'source_refs':[p['current_source_ref']]})
    return choose

def expenses(api,rows):
    for index,(day,amount) in enumerate(rows,2):
        for col,value in enumerate([date_to_serial(date.fromisoformat(day)),'กาแฟ',amount,''],1):api.google.cells[('All Expenses',index,col)]=value

def test_six_reads_and_finish_exceed_two_calls_and_preserve_every_tool_output():
    api=SemanticHosted();api.calendar.events={'a':appointment('a',1),'b':appointment('b',6)}
    calls=[read('calendar',f'2026-11-0{i}',identifier=f'call_{i}') for i in range(1,7)]
    def done(body):
        assert len(results(body))==6
        assert {t['name'] for t in body['tools']}=={'query_expenses','query_calendar','inspect_data_sources','finish_query_analysis','resolve_temporal','query_relations'}
        return finish('calendar_count')(body)
    result,original=turn(api,'ดูนัด 1-6 พ.ย. รวมกี่ครั้ง',*calls,done)
    output=result.data['tool_result']
    assert output['complete'] and output['count']==2 and len(api.model_bodies)==8
    assert 'มี 2 ครั้งครับ' in result.message and len(output['query_refs'])==6
    state=api.snapshot();planner={key:run for key,run in state['model_runs'].items() if not key.startswith('realize01_')}
    assert len(planner)==7 and all(run['decision'] is None for run in planner.values())
    assert result.data['response_realization']['status']=='llm'
    calls=len(api.model_bodies);reads=len(api.calendar_requests)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(original)).status_code==200
    assert len(api.model_bodies)==calls and len(api.calendar_requests)==reads
    assert not api.calendar.posts and not api.google.sheet_writes and not state['actions']

def test_calendar_inference_can_continue_into_expense_reads_and_code_sums_matching_days():
    api=SemanticHosted();api.calendar.events={'a':appointment('a',3),'b':appointment('b',10)}
    expenses(api,[('2026-11-03',35),('2026-11-10',40),('2026-11-11',99)])
    def day(body):
        cal=results(body)[0];assert cal['count']==2 and len(cal['events'])==2
        assert cal['events'][0]['start']=='2026-11-03'
        return read('expense','2026-11-03',identifier='day_1')(body)
    result,_=turn(api,'เดือน พ.ย. TA กี่ครั้ง และวันนั้นจ่ายเท่าไร',
        read('calendar','2026-11-01',date_mode='calendar_month',filter_text='TA',identifier='calendar'),
        day,read('expense','2026-11-10',identifier='day_2'),finish('expense_sum'))
    assert result.data['tool_result']['expense']=='75.00' and result.data['tool_result']['count']==2
    assert '75.00 บาท' in result.message and 'TA 2 ครั้ง' in result.message and '99.00' not in result.message
    assert len(api.model_bodies)==6 and len(api.selection_bodies)==1
    assert not api.google.sheet_writes and not api.calendar.posts

def test_calendar_union_deduplicates_occurrence_across_overlapping_reads():
    api=SemanticHosted();api.calendar.events={'a':appointment('a',3)}
    result,_=turn(api,'ดูนัด 1-5 พ.ย.',read('calendar','2026-11-01','2026-11-03',identifier='one'),
        read('calendar','2026-11-03','2026-11-05',identifier='two'),finish('calendar_count'))
    assert result.data['tool_result']['count']==1

def test_expense_overlap_is_rejected_instead_of_double_counting():
    api=SemanticHosted();expenses(api,[('2026-11-03',35)])
    result,_=turn(api,'รวมค่าใช้จ่าย 1-5 พ.ย.',read('expense','2026-11-01','2026-11-03',identifier='one'),
        read('expense','2026-11-03','2026-11-05',identifier='two'),finish('expense_sum'))
    assert result.status=='clarification' and result.data['tool_result']['error_code']=='overlap'
    assert 'expense' not in result.data['tool_result']

def test_compare_expenses_uses_decimal_difference_and_preserves_each_period():
    api=SemanticHosted();expenses(api,[('2026-11-03',0.1),('2026-11-04',0.3)])
    result,_=turn(api,'เทียบรายจ่าย 3 กับ 4 พ.ย.',read('expense','2026-11-03',identifier='one'),
        read('expense','2026-11-04',identifier='two'),finish('compare'))
    assert result.data['tool_result']['differences']==['0.20'] and '+0.20 บาท' in result.message
    assert '3 พ.ย. 2026' in result.message and '4 พ.ย. 2026' in result.message

@pytest.mark.parametrize('mode',['unknown','duplicate','cross_turn'])
def test_query_refs_cannot_be_invented_duplicated_or_reused_on_a_new_turn(mode):
    api=SemanticHosted()
    if mode=='cross_turn':
        old,_=turn(api,'ดูนัด 3 พ.ย.',read('calendar','2026-11-03'),finish())
        refs=old.data['tool_result']['query_refs']
    else:refs=None
    def invalid(body):
        ref=results(body)[0]['query_ref']
        return finish(refs=refs if mode=='cross_turn' else ['read_invented'] if mode=='unknown' else [ref,ref])(body)
    result,_=turn(api,'ดูนัด 4 พ.ย.',read('calendar','2026-11-04'),invalid)
    assert result.status=='clarification' and result.data['tool_result']['error_code']=='invalid_query_refs'

def test_repeated_identical_read_stops_without_reexecuting_provider():
    api=SemanticHosted();r=read('calendar','2026-11-03',identifier='one')
    result,_=turn(api,'ดูนัด 3 พ.ย.',r,r)
    assert result.data['query_loop_stopped']=='repeated_read' and len(api.model_bodies)==2
    assert len(api.calendar_requests)==1

def test_analysis_phase_cannot_prepare_a_write_even_if_model_requests_one():
    api=SemanticHosted()
    result,_=turn(api,'ดูนัด 3 พ.ย.',read('calendar','2026-11-03'),response('prepare_expenses',{'entries':[]}))
    assert result.status=='model_unavailable' and not api.calendar.posts and not api.google.sheet_writes
    assert not api.snapshot()['actions']

def test_plain_model_answer_after_reads_cannot_replace_grounded_totals():
    api=SemanticHosted();expenses(api,[('2026-11-03',35)])
    result,_=turn(api,'รายจ่าย 3 พ.ย. เท่าไร',read('expense','2026-11-03'),response(text='รวม 999999 บาท บันทึกแล้ว'))
    assert '35.00 บาท' in result.message and '999999' not in result.message and 'บันทึกแล้ว' not in result.message

def test_incomplete_calendar_read_cannot_be_combined_into_zero_count():
    api=SemanticHosted();api.event_pages[PERSONAL]['']=403
    result,_=turn(api,'รวมจำนวน 3 พ.ย.',read('calendar','2026-11-03'),finish('calendar_count'))
    assert result.status=='clarification' and not result.data['tool_result']['complete']
    assert 'count' not in result.data['tool_result'] and '0 ครั้ง' not in result.message

def test_ambiguous_semantic_membership_cannot_be_promoted_to_a_complete_combined_count():
    api=SemanticHosted();api.calendar.events={'a':appointment('a',3)};api.labels={'e1':'uncertain'}
    result,_=turn(api,'เดือน พ.ย. TA มีกี่ครั้ง',
        read('calendar','2026-11-01',date_mode='calendar_month',filter_text='TA'),finish('calendar_count'))
    assert result.data['tool_result']['error_code']=='incomplete_basis' and not result.data['tool_result']['complete']
    assert 'count' not in result.data['tool_result']

def test_duplicate_model_call_ids_are_not_replayed_in_a_later_api_request():
    api=SemanticHosted()
    result,_=turn(api,'ดูนัด 1-2 พ.ย.',read('calendar','2026-11-01',identifier='same'),
        read('calendar','2026-11-02',identifier='same'))
    assert result.data['query_loop_stopped']=='duplicate_call_id' and len(api.model_bodies)==2

def test_removing_turn_ceiling_preserves_configured_daily_accounting_gate():
    api=SemanticHosted();app=api.app();app.state.assistant.guard.max_calls_day=2
    with TestClient(app) as client:
        result,_=api.turn(client,'ดูนัด 1-3 พ.ย.',read('calendar','2026-11-01',identifier='one'),
            read('calendar','2026-11-02',identifier='two'),read('calendar','2026-11-03',identifier='three'))
    assert result.status=='model_budget_exhausted' and len(api.model_bodies)==2
    budget=next(iter(api.snapshot()['model_budgets'].values()))
    assert budget['calls']==2 and budget['reserved_micro_usd']>0
