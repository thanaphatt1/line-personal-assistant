"""Long activity questions read once, decide repeated titles once and ask one clear question."""
from datetime import date,timedelta
import json,socket
import pytest
from line_assistant import calendar_inference as inference
from test_calendar_semantic_queries import SemanticHosted
from test_calendar_text_confirmation_v13 import turn
from test_read_queries import event,plan
from test_calendar_mvp import response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def timed(identifier,day,start,end,title):
    return {**event(identifier,f'{day}T{start}:00+07:00',f'{day}T{end}:00+07:00'),'summary':title}


def exams(api):
    api.calendar.events={
        'a':timed('a','2026-10-27','07:30','12:30','DSDE Exam'),'b':timed('b','2026-10-29','08:00','17:00','facilitator'),
        'c':timed('c','2026-11-01','07:30','17:00','DSDE Exam'),'d':timed('d','2026-11-25','12:00','17:00','DSDE Exam'),
        'e':timed('e','2026-12-02','07:30','17:00','DSDE Exam'),'f':timed('f','2027-01-10','09:00','10:00','DSDE Exam')}


def ask(api):
    return turn(api,'มีวันไหนที่ต้องไปคุมสอบบ้างจนถึงธันวา',plan('calendar','2026-10-24','2026-12-31',filter_text='คุมสอบ'))


def test_one_read_covers_months_and_repeated_titles_share_one_semantic_decision():
    api=SemanticHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    result,_=ask(api)
    payload=json.loads(api.selection_bodies[0]['input'][0]['content'])['calendar_selection']
    groups=[(e['title'],e['occurrences']) for e in payload['events']]
    assert groups==[('DSDE Exam',4),('facilitator',1)] and len(api.selection_bodies)==1
    # The production gate validates this exact grouped body before transmission.
    from line_assistant import response_realization as voice
    assert voice.validate_body(api.selection_bodies[0])
    output=result.data['tool_result']
    assert output['scope']['start']=='2026-10-24' and output['scope']['end']=='2026-12-31'
    assert output['count']==0 and output['calendar_selection']['uncertain_count']==4


def test_zero_confirmed_leads_with_possible_matches_grouped_by_title_and_one_question():
    api=SemanticHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    result,_=ask(api);text=result.message
    assert text.startswith('24 ต.ค. 2026 ถึง 31 ธ.ค. 2026 มี 4 นัดที่อาจเป็นคุมสอบครับ') and result.data['tool_result']['count']==0
    assert 'มี 4 นัดที่อาจเป็นคุมสอบครับ' in text and 'DSDE Exam (4 นัด)' in text and '0 ครั้ง' not in text
    assert '• อ. 27 ต.ค. 2026 07:30–12:30' in text and '• พ. 2 ธ.ค. 2026 07:30–17:00' in text
    assert 'facilitator' not in text and text.rstrip().endswith('DSDE Exam เป็นคุมสอบไหมครับ?')
    buttons=[b['action'] for b in result.data['line_message']['contents']['footer']['contents']]
    assert buttons==[{'type':'message','label':'ใช่','text':'DSDE Exam เป็นคุมสอบ'},{'type':'message','label':'ไม่ใช่','text':'DSDE Exam ไม่ใช่คุมสอบ'}]


def test_answering_for_a_title_settles_every_occurrence_without_a_write():
    api=SemanticHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    first,_=ask(api)
    def confirm_title(body):
        c=json.loads(body['input'][0]['content']);pending=c['pending_query']
        refs=[row['candidate_ref'] for row in pending['candidates'] if row['title']=='DSDE Exam']
        return response('resolve_activity_dates',{'clarification_ref':pending['clarification_ref'],'candidate_refs':refs,'decision':'confirm',
            'selection_text':c['message'],'source_refs':[c['current_source_ref']]})
    result,_=turn(api,'DSDE Exam เป็นคุมสอบ',confirm_title)
    assert len(first.data['read_followup']['candidates'])==4
    assert result.status=='answer' and result.data['tool_result']['count']==4 and result.data['tool_result']['complete']
    assert result.message.startswith('24 ต.ค. 2026 ถึง 31 ธ.ค. 2026 มีคุมสอบ 4 ครั้งครับ') and 'บาท' not in result.message and 'DSDE Exam (4 นัด)' in result.message
    assert not api.calendar.posts and not api.google.sheet_writes


@pytest.mark.parametrize('start,end,ok',[('2026-01-01','2028-12-31',True),('2026-01-01','2029-01-01',False)])
def test_calendar_window_spans_years_within_the_capacity_bound(start,end,ok):
    api=SemanticHosted();result,_=turn(api,'ปฏิทินช่วงยาว',plan('calendar',start,end))
    assert (result.data['tool_result'].get('error_code')!='date_bounds')==ok


@pytest.mark.parametrize('empty_month',[None,'first','last'])
def test_a_period_split_into_month_reads_is_still_answered_as_one_question(empty_month):
    from test_calendar_semantic_queries import selected
    from test_response_realization import generated
    api=SemanticHosted();exams(api)
    if empty_month:
        month='2026-10' if empty_month=='first' else '2026-12'
        api.calendar.events={key:value for key,value in api.calendar.events.items() if not value['start']['dateTime'].startswith(month)}
    count=3 if empty_month else 4
    def classify(body):
        p=json.loads(body['input'][0]['content'])
        if 'calendar_selection' not in p:return generated(body)
        api.selection_bodies.append(body)
        return selected(body,{e['ref']:'uncertain' if e['title']=='DSDE Exam' else 'exclude' for e in p['calendar_selection']['events']})
    api.voice=classify
    def months(body):
        c=json.loads(body['input'][0]['content'])
        def read(start,end):
            return {'tool':'query_calendar','arguments':{'period_text':'จนถึงธันวา','relative_offset':None,'date_mode':'range','start_date':start,'end_date':end,
                'continue_analysis':True,'source_refs':[c['current_source_ref']],'upcoming':False,'filter_text':'คุมสอบ'}}
        return response('query_sources',{'reads':[read('2026-10-24','2026-10-31'),read('2026-11-01','2026-11-30'),read('2026-12-01','2026-12-31')]})
    result,_=turn(api,'มีวันไหนที่ต้องไปคุมสอบบ้างจนถึงธันวา',months,response(text='สรุปให้แล้วครับ'))
    text=result.message
    assert len(api.selection_bodies)==(2 if empty_month else 3) and text.startswith(f'24 ต.ค. 2026 ถึง 31 ธ.ค. 2026 มี {count} นัดที่อาจเป็นคุมสอบครับ')
    assert text.count('นัดที่อาจเป็นคุมสอบ')==1 and f'DSDE Exam ({count} นัด)' in text and text.rstrip().endswith('DSDE Exam เป็นคุมสอบไหมครับ?')
    assert len(result.data['read_followup']['candidates'])==count and result.data['line_message']['contents']['footer']['contents']


def test_a_paraphrased_activity_filter_goes_back_to_the_planner_to_quote_the_owner():
    api=SemanticHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    seen=[]
    def quoted(body):
        seen.append(body);return plan('calendar','2026-10-24','2026-12-31',filter_text='คุมสอบ')(body)
    result,_=turn(api,'มีวันไหนที่ต้องไปคุมสอบบ้างจนถึงธันวา',plan('calendar','2026-10-24','2026-12-31',filter_text='วันที่ต้องไปคุมสอบ'),quoted)
    returned=[json.loads(i['output']) for i in seen[0]['input'] if i.get('type')=='function_call_output']
    assert returned[-1]['repair_feedback']['error_code']=='filter_text_not_quoted' and returned[-1]['repair_feedback']['invalid_fields']==['filter_text']
    assert result.message.startswith('24 ต.ค. 2026 ถึง 31 ธ.ค. 2026 มี 4 นัดที่อาจเป็นคุมสอบครับ') and len(result.data['read_followup']['candidates'])==4


def test_planner_context_and_tool_description_state_the_real_window():
    from line_assistant import read_queries
    from line_assistant.data_inventory import context
    calendar=next(t for t in read_queries.definitions() if t['name']=='query_calendar')
    assert '31' not in calendar['description'] and 'ONE read' in calendar['description']
    assert 'character-for-character' in calendar['parameters']['properties']['filter_text']['description']
    class Host:
        _data_inventory={}
        class calendar_flow:enabled=True
    sources=context(Host())['data_sources']
    assert sources['calendar']['query_window_days']==read_queries.MAX_QUERY_DAYS and sources['expense']['query_window_days']==read_queries.MAX_EXPENSE_DAYS


def test_a_read_stops_before_the_turn_budget_so_the_turn_still_saves_and_replies(monkeypatch):
    from line_assistant import calendar_provider
    from line_assistant.pilot_http import take_rpc
    # Leave the read less headroom than this turn has, as a busy multi-year window would.
    monkeypatch.setattr(calendar_provider,'READ_RESERVE_CALLS',90)
    api=SemanticHosted();exams(api);result,_=ask(api)
    assert result.data['tool_result']['error_code']=='read_capacity' and not api.selection_bodies
    assert 'มีมากเกินกว่าจะอ่านให้ครบในข้อความเดียว' in result.message and 'รูปแบบข้อมูล' not in result.message


def test_a_spent_call_budget_in_a_read_is_reported_as_capacity(monkeypatch):
    from line_assistant import read_queries
    from line_assistant.pilot_http import rpc_budget,take_rpc
    with rpc_budget(1):
        take_rpc()
        assert read_queries.capacity_or('schema_failure')=='read_capacity'
    assert read_queries.capacity_or('schema_failure')=='schema_failure'
