"""Planner-to-executor SQL relations through real synthetic workflows."""
from copy import deepcopy
from datetime import date
import json,socket
import pytest
from fastapi.testclient import TestClient
from test_calendar_mvp import response
from test_calendar_semantic_queries import SemanticHosted,appointment
from test_calendar_text_confirmation_v13 import turn
from test_expense_query_operations import seed,expense
from test_query_analysis_loop import read,finish,results
from test_table_workflow import harness,call
from test_tables import seed as table_seed


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic relational providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def sql(make,*,continue_analysis=False,mutate=None):
    def choose(body):
        c=json.loads(body['input'][0]['content']);rels=[r for r in c['relations_catalog'] if r.get('sql_table')]
        args={'sql':make(rels),'query_refs':[r['query_ref'] for r in rels],
            'source_refs':[c['current_source_ref']],'continue_analysis':continue_analysis}
        if mutate:mutate(args)
        value=response('query_relations',args);value['output'][0]['call_id']='sql_'+str(len(body['input']))
        return value
    return choose


def name(relations,domain):return next(r['sql_table'] for r in relations if r['domain']==domain)


def test_full_expense_rows_survive_display_limit_and_nested_ranking_computes_all():
    api=SemanticHosted();seed(api,[('2026-09-01','A',1)]*25+[('2026-09-02','B',20)])
    query=sql(lambda rs:f"WITH g AS (SELECT item,SUM(amount) AS total,COUNT(*) AS n FROM {name(rs,'expense')} GROUP BY item) SELECT item,total,n FROM g ORDER BY total COLLATE decimal DESC LIMIT 1")
    r,event=turn(api,'เดือน ก.ย. จัดอันดับร้านตามยอดรวม เอาอันดับหนึ่ง',expense(continue_analysis=True,limit=1),query)
    assert r.status=='answer' and r.data['tool_result']['rows']==[{'item':'A','total':'25.00','n':25}],r
    assert not api.google.sheet_writes and not api.calendar.posts and not api.snapshot()['actions']
    count=len(api.model_bodies)
    with TestClient(api.app()) as app:assert api.send(app,deepcopy(event)).status_code==200
    assert len(api.model_bodies)==count


def test_activity_date_exists_join_counts_each_payment_once_for_overlapping_events():
    api=SemanticHosted();api.calendar.events={'a':appointment('a',3),'b':appointment('b',3)}
    seed(api,[('2026-11-03','A',0.1),('2026-11-03','B',0.2),('2026-11-04','C',99)])
    query=sql(lambda rs:f"SELECT SUM(e.amount) AS spending_baht,COUNT(*) AS payments FROM {name(rs,'expense')} e WHERE EXISTS (SELECT 1 FROM {name(rs,'calendar')} c WHERE e.date BETWEEN c.start_date AND c.last_date)")
    r,_=turn(api,'เดือน พ.ย. วันที่ไป TA ใช้เงินรวมเท่าไร',read('calendar','2026-11-01',date_mode='calendar_month',filter_text='TA'),
        expense('2026-11-01','2026-11-30',continue_analysis=True),query)
    assert r.status=='answer' and r.data['tool_result']['rows']==[{'spending_baht':'0.30','payments':2}],r
    assert not api.google.sheet_writes and not api.calendar.posts


def test_generic_people_join_income_with_no_table_specific_query_function():
    h=harness();reg=h.app.table_flow.registry
    table_seed(reg,'people',[{'name':'A','phone':None,'status':'called'},{'name':'B','phone':None,'status':'pending'}])
    table_seed(reg,'family_income',[{'date':'2026-10-01','amount':'500','customer':'row0','note':None},
        {'date':'2026-10-02','amount':'900','customer':'row1','note':None}])
    def query(rs):
        a,b=rs
        return f"SELECT p.name,SUM(i.amount) AS income_baht FROM {a['sql_table']} p JOIN {b['sql_table']} i ON i.customer=p.id WHERE p.status='called' GROUP BY p.id,p.name"
    r=h.send('รายรับของคนที่โทรแล้วเป็นเท่าไร',call('query_table',table_id='people',continue_analysis=True),
        call('query_table',table_id='family_income',continue_analysis=True),sql(query))
    assert r.status=='answer' and r.data['tool_result']['rows']==[{'name':'A','income_baht':'500'}],r
    assert all(getattr(store,'writes',0)==0 for store in reg.stores.values()) and not h.sheets.entries and not h.provider.posts


def test_sql_error_returns_offered_schema_and_repairs_without_losing_reads():
    api=SemanticHosted();seed(api,[('2026-09-01','A',7)])
    def repaired(body):
        outputs=[json.loads(v['output']) for v in body['input'] if v.get('type')=='function_call_output']
        assert outputs[-1]['recovery']['kind']=='assistant_arguments' and outputs[-1]['repair_feedback']
        assert 'no such column' in outputs[-1]['sql_diagnostic']
        return sql(lambda rs:f"SELECT SUM(amount) AS total FROM {rs[0]['sql_table']}")(body)
    r,_=turn(api,'รายจ่าย ก.ย. รวมเท่าไร',expense(continue_analysis=True),
        sql(lambda rs:f"SELECT SUM(invented_column) AS total FROM {rs[0]['sql_table']}"),repaired)
    assert r.status=='answer' and r.data['tool_result']['rows']==[{'total':'7.00'}],r


def test_derived_result_can_be_queried_again_and_finished_by_reference():
    api=SemanticHosted();seed(api,[('2026-09-01','A',10),('2026-09-01','B',20)])
    def second(rs):
        derived=name(rs,'relation');return f"SELECT item,decimal_div(total,'30',2) AS fraction FROM {derived} ORDER BY total COLLATE decimal DESC"
    def finish_last(body):
        ref=[r['query_ref'] for r in results(body) if r.get('relational_query')][-1]
        return finish(refs=[ref])(body)
    r,_=turn(api,'ก.ย. แต่ละร้านคิดเป็นสัดส่วนเท่าไร',expense(continue_analysis=True),
        sql(lambda rs:f"SELECT item,SUM(amount) AS total FROM {name(rs,'expense')} GROUP BY item",continue_analysis=True),
        sql(second,continue_analysis=True),finish_last)
    assert r.status=='answer' and 'B — 0.67' in r.message,r


@pytest.mark.parametrize('damage',['foreign_ref','duplicate','source','missing_column'])
def test_unoffered_references_and_fields_never_supply_a_result(damage):
    api=SemanticHosted();seed(api,[('2026-09-01','A',7)])
    def mutate(p):
        if damage=='foreign_ref':p['query_refs']=['read_other_owner']
        elif damage=='duplicate':p['query_refs']*=2
        elif damage=='source':p['source_refs']=['assistant_source']
        else:p['sql']='SELECT absent FROM '+p['sql'].split(' FROM ')[1]
    r,_=turn(api,'ก.ย. ใช้เงินเท่าไร',expense(continue_analysis=True),
        sql(lambda rs:f"SELECT amount FROM {rs[0]['sql_table']}",mutate=mutate),response(text='ขอตรวจแผนครับ'))
    assert r.status=='clarification' and not r.data['tool_result']['ok'],r
    assert not api.calendar.posts and not api.google.sheet_writes


def test_incomplete_calendar_basis_never_yields_zero_or_a_partial_total():
    from test_calendar_read_all import PERSONAL
    api=SemanticHosted();api.event_pages[PERSONAL]['']=403
    def incomplete(body):
        c=json.loads(body['input'][0]['content']);r=c['relations_catalog'][0]
        return response('query_relations',{'query_refs':[r['query_ref']],'sql':'SELECT COUNT(*) AS n FROM nonexistent',
            'source_refs':[c['current_source_ref']],'continue_analysis':False})
    r,_=turn(api,'นัด 3 พ.ย. มีกี่ครั้ง',read('calendar','2026-11-03'),incomplete)
    assert r.status=='clarification' and 'rows' not in r.data['tool_result'],r
    assert r.data['tool_result']['error_code']=='relation_incomplete_source'


def test_summary_without_raw_rows_is_explicitly_unavailable_for_sql():
    api=SemanticHosted();seed(api,[('2026-11-03','A',7)])
    def choose(body):
        c=json.loads(body['input'][0]['content']);row=c['relations_catalog'][0]
        assert not row['available'] and row['error_code']=='relation_rows_unavailable'
        return response('query_relations',{'sql':'SELECT COUNT(*) FROM missing','query_refs':[row['query_ref']],
            'source_refs':[c['current_source_ref']],'continue_analysis':False})
    r,_=turn(api,'รายจ่าย 3 พ.ย.',read('expense','2026-11-03'),choose,response(text='ต้องอ่านรายการก่อนครับ'))
    assert r.status=='clarification' and not r.data['tool_result']['ok']


def test_read_loop_still_cannot_prepare_or_confirm_writes_after_sql():
    api=SemanticHosted();seed(api,[('2026-09-01','A',7)])
    r,_=turn(api,'อ่าน ก.ย.',expense(continue_analysis=True),
        sql(lambda rs:f"SELECT amount FROM {rs[0]['sql_table']}",continue_analysis=True),response('prepare_expenses',{'entries':[]}))
    assert r.status=='model_unavailable' and not api.google.sheet_writes and not api.calendar.posts


def test_query_relations_refs_expire_with_the_owner_turn():
    api=SemanticHosted();seed(api,[('2026-09-01','A',7)])
    r,_=turn(api,'อ่าน ก.ย.',expense(continue_analysis=True),sql(lambda rs:f"SELECT amount FROM {rs[0]['sql_table']}"))
    old=r.data['tool_result']['query_ref']
    def stale(body):
        c=json.loads(body['input'][0]['content'])
        return response('query_relations',{'sql':'SELECT * FROM old','query_refs':[old],
            'source_refs':[c['current_source_ref']],'continue_analysis':False})
    r,_=turn(api,'ดูอีกที',stale,response(text='ต้องอ่านใหม่ครับ'))
    assert r.status=='clarification' and not r.data['tool_result']['ok']
    assert r.data['tool_result']['error_code']=='relation_reference_unavailable'


def test_empty_complete_read_can_prove_zero():
    api=SemanticHosted()
    r,_=turn(api,'ก.ย. จ่ายกี่รายการ',expense(continue_analysis=True),
        sql(lambda rs:f"SELECT COUNT(*) AS payments FROM {rs[0]['sql_table']}"))
    assert r.status=='answer' and r.data['tool_result']['rows']==[{'payments':0}],r


def test_an_unrelated_nonfinancial_schema_needs_only_metadata():
    from line_assistant.tables import Table,Column,MemoryTableStore,Registry
    table=Table(id='books',label='หนังสือทดสอบ',description='หนังสือที่อ่าน',columns=[
        Column(name='title',label='ชื่อเรื่อง',type='text',required=True),Column(name='pages',label='หน้า',type='integer')])
    reg=Registry([table],{'books':MemoryTableStore('synthetic-books')});h=harness(reg)
    table_seed(reg,'books',[{'title':'A','pages':100},{'title':'B','pages':300}])
    r=h.send('หนังสือเล่มไหนหนากว่าค่าเฉลี่ย',call('query_table',table_id='books',continue_analysis=True),
        sql(lambda rs:f"SELECT title,pages FROM {rs[0]['sql_table']} WHERE decimal_cmp(pages,(SELECT decimal_div(SUM(pages),COUNT(pages),2) FROM {rs[0]['sql_table']}))>0"))
    assert r.status=='answer' and r.data['tool_result']['rows']==[{'title':'B','pages':300}],r
    assert not reg.stores['books'].writes


@pytest.mark.parametrize('timed',[False,True])
def test_calendar_date_columns_use_bangkok_and_only_the_read_period(timed):
    api=SemanticHosted()
    event=appointment('a',3)
    if timed:event.update(start={'dateTime':'2026-11-02T18:30:00+00:00'},end={'dateTime':'2026-11-02T19:00:00+00:00'})
    else:event.update(start={'date':'2026-11-01'},end={'date':'2026-11-06'})
    api.calendar.events={'a':event}
    seed(api,[('2026-11-02','A',5),('2026-11-03','B',10),('2026-11-04','C',20)])
    r,_=turn(api,'รายจ่าย พ.ย. เฉพาะนัดที่อ่านวันที่ 3 พ.ย.',read('calendar','2026-11-03'),
        expense('2026-11-01','2026-11-30',continue_analysis=True),sql(lambda rs:
            f"SELECT SUM(e.amount) AS total FROM {name(rs,'expense')} e WHERE EXISTS (SELECT 1 FROM {name(rs,'calendar')} c WHERE e.date BETWEEN c.start_date AND c.last_date)"))
    assert r.status=='answer' and r.data['tool_result']['rows']==[{'total':'10.00'}],r
