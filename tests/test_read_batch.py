"""Batch reads save planner round trips while retaining complete source scopes."""
from copy import deepcopy
import json
import pytest
from line_assistant.sheets_api import SheetsApi
from test_calendar_semantic_queries import SemanticHosted
from test_calendar_text_confirmation_v13 import turn
from test_calendar_mvp import response
from test_expense_query_operations import seed,expense
from test_query_analysis_loop import results,finish,read


def batch(*plans,mutate=None):
    def choose(body):
        reads=[]
        for plan in plans:
            call=plan(body)['output'][0]
            reads.append({'tool':call['name'],'arguments':json.loads(call['arguments'])})
        args={'reads':reads}
        if mutate:mutate(args)
        return response('query_sources',args)
    return choose


def finish_batch(body):
    reads=results(body)[-1]['results'];context=json.loads(body['input'][0]['content'])
    return response('finish_query_analysis',{'query_refs':[r['query_ref'] for r in reads],
        'operation':'compare','source_refs':[context['current_source_ref']]})


def test_two_month_comparison_uses_one_snapshot_and_two_planning_calls(monkeypatch):
    api=SemanticHosted();seed(api,[('2026-09-01','A',10),('2026-10-01','B',37)])
    snapshots=[];load=SheetsApi._saved_snapshot
    def observed(self):snapshots.append(1);return load(self)
    monkeypatch.setattr(SheetsApi,'_saved_snapshot',observed)
    result,_=turn(api,'เทียบรายจ่าย ก.ย. กับ ต.ค.',batch(
        expense(continue_analysis=True),expense('2026-10-01','2026-10-31',continue_analysis=True)),finish_batch)
    assert result.status=='answer' and result.data['tool_result']['differences']==['27.00'],result
    assert len(snapshots)==1 and len(api.model_bodies)==3  # two planner + one wording
    assert not api.google.sheet_writes and not api.calendar.posts
    sequential=SemanticHosted();seed(sequential,[('2026-09-01','A',10),('2026-10-01','B',37)])
    comparison,_=turn(sequential,'เทียบรายจ่าย ก.ย. กับ ต.ค.',
        read('expense','2026-09-01','2026-09-30',identifier='september'),
        read('expense','2026-10-01','2026-10-31',identifier='october'),finish('compare'))
    assert comparison.data['tool_result']['differences']==result.data['tool_result']['differences']
    assert len(sequential.model_bodies)==4  # same computed result, one extra planner call


@pytest.mark.parametrize('damage',['write','dependent','duplicate','no_analysis','bad_source'])
def test_entire_invalid_batch_is_rejected_before_any_saved_data_read(monkeypatch,damage):
    api=SemanticHosted();snapshots=[]
    monkeypatch.setattr(SheetsApi,'_saved_snapshot',lambda self:snapshots.append(1) or [])
    def mutate(args):
        row=args['reads'][0]
        if damage=='write':row['tool']='prepare_expenses'
        elif damage=='dependent':row['arguments']['calendar_query_refs']=['invented']
        elif damage=='duplicate':args['reads'].append(deepcopy(row))
        elif damage=='no_analysis':row['arguments']['continue_analysis']=False
        else:row['arguments']['source_refs']=['foreign_source']
    result,_=turn(api,'เทียบรายจ่าย ก.ย.',batch(expense(continue_analysis=True),mutate=mutate),response(text='ยังวางแผนอ่านข้อมูลไม่สำเร็จครับ'))
    assert not snapshots and not api.google.sheet_writes and not api.calendar.posts
    assert result.status!='recorded'


def test_failed_member_cannot_disappear_when_model_ends_with_only_partial_prose():
    api=SemanticHosted();seed(api,[('2026-09-01','A',10),('2026-10-01','B',37)])
    def wrong_span(args):args['reads'][0]['arguments']['period_text']='เดือน ก.ย. ปี 2026'
    result,_=turn(api,'เทียบรายจ่ายเดือน ก.ย. กับ ต.ค. ปี 2026',batch(
        expense(continue_analysis=True),expense('2026-10-01','2026-10-31',continue_analysis=True),mutate=wrong_span),
        response(text='เดือนตุลาคมใช้ไป 37 บาท แต่ยังเปรียบเทียบไม่ได้ครับ'))
    assert result.status=='clarification' and result.data['tool_result']['complete'] is False
    assert len(result.data['tool_result']['query_refs'])==2 and '37.00' in result.message
    assert not api.google.sheet_writes and not api.calendar.posts
