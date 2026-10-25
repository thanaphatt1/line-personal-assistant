"""Reads the release eval found fragile: relative days, next appointments, quoting slips and SQL display."""
import json,socket
import pytest
from test_read_queries import plan
from test_response_realization import VoiceHosted
from test_calendar_text_confirmation_v13 import turn
from test_calendar_mvp import response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def edited(base,**changes):
    def choose(body):
        answer=base(body);call=answer['output'][0]
        call['arguments']=json.dumps({**json.loads(call['arguments']),**changes},ensure_ascii=False);return answer
    return choose


@pytest.mark.parametrize('offset,day',[(-1,'2026-10-15'),(0,'2026-10-16'),(1,'2026-10-17')])
def test_a_relative_day_is_computed_by_the_server_from_the_bangkok_clock(offset,day):
    api=VoiceHosted()
    result,_=turn(api,'เมื่อวานจ่ายอะไรไปบ้าง',edited(plan(listing=True),date_mode='calendar_day',relative_offset=offset,start_date=None,end_date=None,period_text='เมื่อวาน'))
    scope=result.data['read_query']
    assert result.data['tool_result']['complete'] and scope['start']==scope['end']==day


def test_next_appointments_need_no_period_words():
    api=VoiceHosted()
    result,_=turn(api,'ขอดูนัดถัดไปหน่อย',edited(plan('calendar',upcoming=True,date_mode='upcoming'),period_text=None,start_date=None,end_date=None))
    output=result.data['tool_result']
    assert output['complete'] and output['scope']['upcoming'] and output['scope']['start']=='2026-10-16'


def test_a_misquoted_period_goes_back_to_the_planner_instead_of_asking_the_owner():
    api=VoiceHosted();seen=[]
    def quoted(body):
        seen.append(body);return edited(plan(start='2026-10-01',end='2026-10-31'),period_text='ต.ค. นี้')(body)
    result,_=turn(api,'ต.ค. นี้หมดไปกี่บาทแล้ว',edited(plan(start='2026-10-01',end='2026-10-31'),period_text='ต.ค. นี้หมดไปแล้ว'),quoted)
    returned=[json.loads(i['output']) for i in seen[0]['input'] if i.get('type')=='function_call_output']
    assert returned[-1]['repair_feedback']['error_code']=='period_text_not_quoted'
    assert result.data['tool_result']['complete'] and 'ต้องการดูช่วงวันไหน' not in result.message


def test_read_tool_guidance_consistently_uses_sql_for_filters_and_names_balance():
    from line_assistant import read_queries,relational
    tools={t['name']:t for t in read_queries.definitions()+relational.definitions()}
    expenses=tools['query_expenses']['parameters']['properties']
    assert 'including one named item or category' in expenses['filter']['description']
    assert expenses['filter']['type']=='null' and expenses['view']['enum']==['list']
    assert expenses['continue_analysis']['enum']==[True]
    assert 'เหลือเงิน' in expenses['measure']['description']
    assert tools['query_relations']['description'].startswith('Primary')
    assert tools['query_relations']['parameters']['properties']['continue_analysis']['enum']==[True]
    calendar=tools['query_calendar']['parameters']['properties']
    assert 'calendar_day' in calendar['date_mode']['enum'] and 'เมื่อวาน' in calendar['relative_offset']['description']


def test_sql_fallback_shows_amounts_in_the_shared_format_without_column_jargon():
    from line_assistant.relational import message
    text=message([{'total_baht':'296.00','payments':7}],1,[{'name':'total_baht','unit':'บาท'}])
    assert text=='ผลลัพธ์ครับ\ntotal: 296 บาท | payments: 7'
    assert message([{'item':'Tram','amount':'110.00'},{'item':'Coffee','amount':'25.50'}],2,[{'name':'amount','unit':'บาท'}]).split('\n')[1:]==['1. item: Tram | amount: 110 บาท','2. item: Coffee | amount: 25.50 บาท']


def test_table_reviews_show_thai_dates_not_iso():
    from line_assistant.table_workflow import shown
    assert shown('2026-10-23')=='23 ต.ค. 2026' and shown('0000187')=='0000187' and shown(None)=='ว่าง' and shown('2026-13-01')=='2026-13-01'
