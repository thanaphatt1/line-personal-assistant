"""Answers use the unit and period the owner asked about, in one readable format."""
from datetime import datetime
import json,socket
import pytest
from line_assistant.reply_display import money,named,polite
from line_assistant.read_queries import period_label,covered_days
from test_calendar_activity_answers import exams,timed,ask
from test_calendar_semantic_queries import SemanticHosted
from test_calendar_text_confirmation_v13 import turn
from test_read_queries import plan
from test_calendar_mvp import response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


@pytest.mark.parametrize('value,shown',[('30.00','30'),('32','32'),(32.5,'32.50'),('1250.00','1,250'),('0.00','0'),(None,'?'),('','?'),('n/a','n/a')])
def test_one_amount_format_drops_zero_satang_and_groups_thousands(value,shown):
    assert money(value)==shown


def test_latin_names_get_spaces_and_thai_names_do_not():
    assert 'มี'+named('คุมสอบ')+' 2 ครั้ง'=='มีคุมสอบ 2 ครั้ง' and 'มี'+named('TA').rstrip()+' 2 ครั้ง'=='มี TA 2 ครั้ง'
    assert polite('ไม่มี TA')=='ไม่มี TA ครับ' and polite('ไม่มีคุมสอบ')=='ไม่มีคุมสอบครับ'


@pytest.mark.parametrize('start,end,label',[
    ('2026-11-01','2026-11-30','เดือนพฤศจิกายน 2026'),('2026-02-01','2026-02-28','เดือนกุมภาพันธ์ 2026'),
    ('2026-01-01','2026-12-31','ปี 2026'),('2026-11-01','2026-11-29','1 พ.ย. 2026 ถึง 29 พ.ย. 2026'),
    ('2026-10-24','2026-12-31','24 ต.ค. 2026 ถึง 31 ธ.ค. 2026')])
def test_whole_months_and_years_are_named_not_spelled_as_ranges(start,end,label):
    assert period_label({'start':start,'end':end},datetime.fromisoformat('2026-10-24T09:00:00+07:00'))==label


def test_covered_days_counts_dates_not_appointments_and_keeps_exclusive_ends():
    def at(day,start,end):return {'start':f'{day}T{start}:00+07:00','end':f'{day}T{end}:00+07:00','all_day':False}
    rows=[at('2026-11-01','07:30','12:30'),at('2026-11-01','13:00','17:00'),
          {'start':'2026-11-03','end':'2026-11-05','all_day':True},at('2026-11-07','23:00','23:59')]
    assert covered_days(rows,'2026-11-01','2026-11-30')==['2026-11-01','2026-11-03','2026-11-04','2026-11-07']
    assert covered_days(rows,'2026-11-04','2026-11-30')==['2026-11-04','2026-11-07']


def days(domain_args):
    def choose(body):
        answer=plan('calendar',*domain_args[:2],filter_text=domain_args[2])(body);call=answer['output'][0]
        call['arguments']=json.dumps({**json.loads(call['arguments']),'count_unit':'days'},ensure_ascii=False);return answer
    return choose


def two_on_one_day(api):
    exams(api);api.calendar.events['x']=timed('x','2026-11-01','18:00','19:00','DSDE Exam')


def test_how_many_days_counts_distinct_dates_for_an_activity():
    api=SemanticHosted();two_on_one_day(api);api.labels={'e1':'match'}
    result,_=turn(api,'เดือนพฤศจิกายนมีคุมสอบกี่วัน',days(('2026-11-01','2026-11-30','คุมสอบ')))
    output=result.data['tool_result']
    assert output['count']==3 and output['day_count']==2
    assert result.message.startswith('เดือนพฤศจิกายน 2026 มีคุมสอบ 2 วัน (3 ครั้ง)ครับ')


def test_how_many_days_without_a_filter_counts_dates_of_all_appointments():
    api=SemanticHosted();two_on_one_day(api)
    result,_=turn(api,'เดือนพฤศจิกายนมีนัดกี่วัน',days(('2026-11-01','2026-11-30',None)),response(text='สรุปแล้วครับ'))
    assert result.data['tool_result']['day_count']==2 and result.data['query_facts']['count']=='2 วัน (3 นัด)'


def test_a_title_confirmation_answers_in_days_when_days_were_asked():
    api=SemanticHosted();two_on_one_day(api);api.labels={'e1':'uncertain'}
    first,_=turn(api,'เดือนพฤศจิกายนมีคุมสอบกี่วัน',days(('2026-11-01','2026-11-30','คุมสอบ')))
    def confirm(body):
        c=json.loads(body['input'][0]['content']);pending=c['pending_query']
        return response('resolve_activity_dates',{'clarification_ref':pending['clarification_ref'],'candidate_refs':[r['candidate_ref'] for r in pending['candidates']],
            'decision':'confirm','selection_text':c['message'],'source_refs':[c['current_source_ref']]})
    result,_=turn(api,'DSDE Exam เป็นคุมสอบ',confirm)
    assert result.message.startswith('เดือนพฤศจิกายน 2026 มีคุมสอบ 2 วัน (3 ครั้ง)ครับ')


def test_a_confirmed_dates_subtotal_still_names_the_period_that_was_asked():
    from test_expense_query_operations import seed
    api=SemanticHosted();exams(api);api.labels={'e1':'uncertain'}
    seed(api,[('2026-11-01','ค่าข้าว',120),('2026-11-25','ค่ารถ',45),('2026-11-12','ค่าอาหาร',500)])
    api.now=datetime.fromisoformat('2026-12-03T09:00:00+07:00')
    def spending(body):
        answer=plan('calendar','2026-11-01','2026-11-30',filter_text='คุมสอบ')(body);call=answer['output'][0]
        call['arguments']=json.dumps({**json.loads(call['arguments']),'purpose':'expense_dates'},ensure_ascii=False);return answer
    turn(api,'เดือนพฤศจิกายนใช้เงินวันที่ไปคุมสอบไปเท่าไร',spending)
    def confirm(body):
        c=json.loads(body['input'][0]['content']);pending=c['pending_query']
        return response('resolve_activity_dates',{'clarification_ref':pending['clarification_ref'],'candidate_refs':[r['candidate_ref'] for r in pending['candidates']],
            'decision':'confirm','selection_text':c['message'],'source_refs':[c['current_source_ref']]})
    result,_=turn(api,'DSDE Exam เป็นคุมสอบ',confirm)
    # The subtotal reads only Nov 1–25, but the owner asked about November.
    assert result.message.startswith('เดือนพฤศจิกายน 2026 เฉพาะวันที่มีนัดคุมสอบ ใช้ไป 165 บาท')
    assert 'ยอดเฉพาะวันที่ยืนยันแล้ว' not in result.message and '25 พ.ย. 2026 เฉพาะ' not in result.message


def test_zero_confirmed_answer_has_one_polite_lead_without_repeated_confirmation_words():
    api=SemanticHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    first_line=ask(api)[0].message.split('\n')[0]
    assert first_line=='24 ต.ค. 2026 ถึง 31 ธ.ค. 2026 มี 4 นัดที่อาจเป็นคุมสอบครับ' and first_line.count('ครับ')==1


@pytest.mark.parametrize('template,shown',[
    ('เดือนนี้ เฉพาะ {{f1}} ใช้ไปทั้งหมด{{f2}} จาก{{f3}}ครับ','เดือนนี้ เฉพาะ อาหาร ใช้ไปทั้งหมด 20 บาท จาก 1 รายการครับ'),
    ('เดือน {{f4}} รวม{{f2}}','เดือนกันยายน 2026 รวม 20 บาท'),
    ('ที่{{f2}}','ที่ 20 บาท'),('{{f2}}ครับ','20 บาทครับ'),('{{f1}}\n{{f2}}','เฉพาะ อาหาร\n20 บาท')])
def test_composed_prose_and_facts_join_without_repeated_words_or_glued_digits(template,shown):
    from line_assistant.answer_composition import joined
    assert joined(template,{'f1':'เฉพาะ อาหาร','f2':'20 บาท','f3':'1 รายการ','f4':'เดือนกันยายน 2026'})==shown
