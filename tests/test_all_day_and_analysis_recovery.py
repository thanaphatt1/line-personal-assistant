"""Reported conversation shapes with synthetic appointments and financial data."""
from copy import deepcopy
import json,socket
import pytest

from line_assistant import operation_composition as operation
from test_calendar_mvp import Harness,response
from test_calendar_multiline_metadata import proposed,TITLE,LAYOUTS
from test_semantic_calendar_proposals import restore
from test_pending_records import patch
from test_query_analysis_loop import read,results,expenses
from test_calendar_semantic_queries import SemanticHosted
from test_calendar_text_confirmation_v13 import turn
from test_relational_queries import sql
from test_compact_expense_experience import review_result

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic services only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def incomplete(body,*,batch=False,uncertain=('time_text','end_time_text')):
    out=proposed(body,batch=batch);args=json.loads(out['output'][0]['arguments'])
    event=args['events'][0] if batch else args
    event['fields'].update(time_text=None,end_time_text=None)
    event['uncertain_fields']=list(uncertain)
    return response('prepare_calendar_events' if batch else 'prepare_calendar_event',args)


@pytest.mark.parametrize('batch',[False,True])
def test_all_day_answer_retires_missing_clock_uncertainty_and_cold_confirms_once(batch):
    h=Harness();r=h.send(LAYOUTS[0].format(title=TITLE),lambda b:incomplete(b,batch=batch))
    assert r.status=='clarification' and h.provider.posts==0
    r=h.send('ทั้งวัน',patch({0:[('all_day',True,'ทั้งวัน')]},domain='calendar_batch' if batch else 'calendar'))
    assert r.status=='preview',r
    assert h.draft()['issues']==[] and h.draft()['semantic_uncertain_fields']==[]
    assert h.draft()['start']=='2026-11-07' and h.draft()['end']=='2026-11-08'
    other=restore(h);event=other.event(r.data['confirm_command']);saved=other.app.handle(event)
    assert saved.status=='created' and h.provider.posts==1
    assert other.app.handle(deepcopy(event))==saved and h.provider.posts==1


def test_all_day_does_not_remove_uncertainty_about_the_date():
    h=Harness();h.send(LAYOUTS[0].format(title=TITLE),lambda b:incomplete(b,uncertain=('date_text','time_text')))
    r=h.send('ตลอดวัน',patch({0:[('all_day',True,'ตลอดวัน')]},domain='calendar'))
    assert r.status=='clarification' and h.draft()['issues']==['calendar_date_unclear']
    assert 'confirm_command' not in r.data and h.provider.posts==0


def test_replacing_an_uncertain_endpoint_with_duration_retires_its_uncertainty():
    h=Harness()
    def prepare(body):
        out=proposed(body);args=json.loads(out['output'][0]['arguments'])
        args['fields']['end_time_text']=None;args['uncertain_fields']=['end_time_text']
        return response('prepare_calendar_event',args)
    h.send(LAYOUTS[0].format(title=TITLE),prepare)
    r=h.send('ยาวหนึ่งชั่วโมง',patch({0:[('duration_text',60,'หนึ่งชั่วโมง')]},domain='calendar'))
    assert r.status=='preview' and h.draft()['end']=='2026-11-07T13:00:00+07:00'
    assert h.draft()['semantic_uncertain_fields']==[] and h.provider.posts==0


def test_confirmed_name_resolution_is_not_required_review_prose():
    result=review_result();row=result.data['tool_result']['entries'][3]
    row['item_resolution']={'from':'รถรับจ้างจำลอง','to':row['item']}
    row['note']='ขาไป → ขากลับ'
    view=operation.for_result(result);text=operation.fallback(view)
    assert not any(f['label']=='ชื่อเดิมที่เลือก' for r in view['records'] for f in r['fields'].values())
    assert row['item'] in text and 'รถรับจ้างจำลอง' not in text
    assert 'ขาไป → ขากลับ' in text


def finish_source(body):
    c=json.loads(body['input'][0]['content'])
    ref=next(v['query_ref'] for v in results(body) if v.get('domain')=='expense' and v.get('query_ref'))
    out=response('finish_query_analysis',{'query_refs':[ref],'operation':'report','source_refs':[c['current_source_ref']]})
    out['output'][0]['call_id']='finish_'+str(len(body['input']))
    return out


def test_report_can_repair_again_after_a_new_verified_result_without_re_reading():
    api=SemanticHosted();expenses(api,[('2026-11-03',12),('2026-11-04',23)])
    def select_result(body):
        feedback=results(body)[-1]
        assert feedback['repair_feedback']['error_code']=='analysis_projection_required'
        choices=feedback['query_selection']['available_results']
        usable=[r for r in choices if r['reportable']]
        assert len(usable)==2 and all(r['domain']=='relation' for r in usable)
        c=json.loads(body['input'][0]['content'])
        return response('finish_query_analysis',{'query_refs':[usable[-1]['query_ref']],'operation':'report','source_refs':[c['current_source_ref']]})
    r,_=turn(api,'สรุปรายจ่ายเดือนนี้',read('expense','2026-11-01','2026-11-30',listing=True),
        sql(lambda rs:f"SELECT SUM(amount) AS spending FROM {rs[0]['sql_table']}",continue_analysis=True),finish_source,
        sql(lambda rs:f"SELEC SUM(amount) FROM {rs[0]['sql_table']}",continue_analysis=True),
        sql(lambda rs:f"SELECT SUM(amount) AS total,COUNT(*) AS payments FROM {rs[0]['sql_table']}",continue_analysis=True),
        finish_source,select_result)
    assert r.status=='answer',r
    assert r.data['tool_result']['complete'] and len(r.data['tool_result']['query_refs'])==1
    assert '35 บาท' in r.message and not api.google.sheet_writes and not api.calendar.posts


def test_unchanged_bad_selection_still_stops_without_new_verified_evidence():
    api=SemanticHosted();expenses(api,[('2026-11-03',12)])
    r,_=turn(api,'สรุปรายจ่ายเดือนนี้',read('expense','2026-11-01','2026-11-30',listing=True),finish_source,finish_source)
    assert r.status=='clarification' and r.data['tool_result']['repair_stopped']=='repeated_failure'
    assert not api.google.sheet_writes and not api.calendar.posts


def test_an_unverified_completion_goes_back_to_the_planner_without_asking_the_owner_to_repeat():
    h=Harness()
    def repaired(body):
        c=json.loads(body['input'][0]['content'])
        assert c['repair_feedback']['error_code']=='unverified_completion_claim'
        assert c['message']==LAYOUTS[0].format(title=TITLE)
        assert h.provider.posts==0
        return proposed(body)
    r=h.send(LAYOUTS[0].format(title=TITLE),response(text='เพิ่มนัดเรียบร้อยแล้วครับ'),repaired)
    assert r.status=='preview' and h.provider.posts==0 and len(h.model.requests)==2
    assert h.send(r.data['confirm_command']).status=='created' and h.provider.posts==1


def test_a_repeated_unverified_completion_stops_without_a_tool_or_write():
    h=Harness();bad=response(text='เพิ่มนัดเรียบร้อยแล้วครับ')
    r=h.send(LAYOUTS[0].format(title=TITLE),bad,bad)
    assert r.status=='clarification' and len(h.model.requests)==2 and h.provider.posts==0
    assert not h.state.calendar_drafts and 'confirm_command' not in r.data


def test_an_ordinary_clarification_does_not_trigger_response_repair():
    h=Harness();r=h.send('อยากเพิ่มนัดครับ',response(text='นัดเรื่องอะไรครับ?'))
    assert r.message=='นัดเรื่องอะไรครับ?' and len(h.model.requests)==1 and h.provider.posts==0


@pytest.mark.parametrize('certainty,status',[('exact','preview'),('uncertain','clarification')])
def test_all_day_choice_accepts_its_explicit_certainty_without_a_repair_roundtrip(certainty,status):
    h=Harness();h.send(LAYOUTS[0].format(title=TITLE),incomplete)
    def qualify(args):args['records'][0]['changes'][0]['certainty']=certainty
    calls=len(h.model.requests)
    r=h.send('ทั้งวัน',patch({0:[('all_day',True,'ทั้งวัน')]},domain='calendar',mutate=qualify))
    assert r.status==status and len(h.model.requests)==calls+1 and h.provider.posts==0
    if certainty=='uncertain':assert 'calendar_event_type_conflict' in h.draft()['issues'] and 'confirm_command' not in r.data


def test_an_existing_all_day_draft_with_old_clock_uncertainty_can_be_reaffirmed():
    h=Harness();h.send(LAYOUTS[0].format(title=TITLE),incomplete)
    h.send('ทั้งวัน',patch({0:[('all_day',True,'ทั้งวัน')]},domain='calendar'))
    d=h.draft();d.update(semantic_uncertain_fields=['time_text'],issues=['calendar_time_unclear'],review=None,status='needs_information')
    h.app.calendar_flow.seal(d)
    other=restore(h)
    r=other.send('นัดทั้งวัน',patch({0:[('all_day',True,'ทั้งวัน')]},domain='calendar'))
    assert r.status=='preview' and other.draft()['semantic_uncertain_fields']==[] and h.provider.posts==0
