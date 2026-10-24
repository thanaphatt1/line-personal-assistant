"""Owner title→activity answers are remembered, disclosed, reused and forgettable."""
import json,socket
import pytest
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.history_context import HistoryState
from test_calendar_activity_answers import exams,ask
from test_calendar_semantic_queries import SemanticHosted
from test_calendar_text_confirmation_v13 import turn
from test_calendar_mvp import response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


class MemoryHosted(SemanticHosted):
    def __init__(self,enabled=True):
        super().__init__();self.extra_config={'PILOT_ACTIVITY_MEMORY_ENABLED':'true' if enabled else 'false'}


def answer(refs_for):
    def choose(body):
        c=json.loads(body['input'][0]['content']);pending=c['pending_query']
        return response('resolve_activity_dates',{'clarification_ref':pending['clarification_ref'],'candidate_refs':refs_for(pending['candidates']),
            'decision':'confirm','selection_text':c['message'],'source_refs':[c['current_source_ref']]})
    return choose


def label(title,activity,decision):
    def choose(body):
        c=json.loads(body['input'][0]['content'])
        return response('update_activity_label',{'title':title,'activity':activity,'decision':decision,'source_refs':[c['current_source_ref']]})
    return choose


def labels(api):
    return api.snapshot()['history24']['current'].get('activity_labels_v1',{}).get('labels',{})


def confirm_exam(api):
    ask(api)
    return turn(api,'DSDE Exam เป็นคุมสอบ',answer(lambda rows:[r['candidate_ref'] for r in rows if r['title']=='DSDE Exam']))[0]


def test_a_whole_title_answer_is_remembered_and_offers_one_tap_undo():
    api=MemoryHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    result=confirm_exam(api)
    assert result.message.startswith('คุมสอบ 4 นัดครับ') and 'จำไว้แล้วว่า DSDE Exam เป็นคุมสอบ ครั้งหน้าจะนับให้เลยครับ' in result.message
    buttons=[b['action'] for b in result.data['line_message']['contents']['footer']['contents']]
    assert buttons==[{'type':'message','label':'ไม่ต้องจำ','text':'ไม่ต้องจำว่า DSDE Exam เป็นคุมสอบ'}]
    assert [(r['title'],r['activity'],r['decision']) for r in labels(api).values()]==[('DSDE Exam','คุมสอบ','match')]
    assert not api.calendar.posts and not api.google.sheet_writes


def test_the_next_read_applies_the_label_discloses_it_and_classifies_only_the_rest():
    api=MemoryHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    confirm_exam(api);before=len(api.selection_bodies)
    api.labels={'e1':'exclude'}
    result,_=ask(api)
    sent=json.loads(api.selection_bodies[before]['input'][0]['content'])['calendar_selection']['events']
    assert [e['title'] for e in sent]==['facilitator']
    output=result.data['tool_result']
    assert output['complete'] and output['count']==4 and output['calendar_selection']['remembered']==[{'title':'DSDE Exam','decision':'match'}]
    assert 'นับ DSDE Exam เป็นคุมสอบตามที่เคยยืนยันไว้' in result.message and 'read_followup' not in result.data


def test_when_every_title_is_remembered_no_classifier_request_is_made():
    api=MemoryHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    confirm_exam(api)
    stored,_=turn(api,'จำไว้ว่า facilitator ไม่ใช่คุมสอบ',label('facilitator','คุมสอบ','exclude'))
    assert stored.message=='จำไว้แล้วครับ ครั้งหน้าจะไม่นับ facilitator เป็นคุมสอบ'
    before=len(api.selection_bodies)
    result,_=ask(api)
    assert len(api.selection_bodies)==before and result.data['tool_result']['count']==4
    assert 'นับ DSDE Exam เป็นคุมสอบตามที่เคยยืนยันไว้' in result.message and 'ไม่นับ facilitator เพราะเคยบอกว่าไม่ใช่คุมสอบ' in result.message


def test_forgetting_a_label_brings_the_question_back():
    api=MemoryHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    confirm_exam(api)
    forgot,_=turn(api,'ไม่ต้องจำว่า DSDE Exam เป็นคุมสอบ',label('DSDE Exam','คุมสอบ','forget'))
    assert forgot.message=='ไม่จำแล้วครับ ครั้งหน้าถ้าเจอ DSDE Exam จะถามก่อนว่าเป็นคุมสอบไหม' and not labels(api)
    tool=next(t for t in api.model_bodies[-1]['tools'] if t['name']=='update_activity_label')
    assert 'stop remembering' in tool['parameters']['properties']['decision']['description']
    result,_=ask(api)
    assert result.message.rstrip().endswith('DSDE Exam เป็นคุมสอบไหมครับ?')


def test_a_label_must_come_from_the_owner_wording():
    api=MemoryHosted();exams(api)
    seen=[]
    def asks(body):
        seen.append(body);return response(text='อยากให้จำนัดชื่ออะไรครับ?')
    result,_=turn(api,'จำไว้ว่าอันนั้นเป็นคุมสอบ',label('DSDE Exam','คุมสอบ','match'),asks)
    returned=[json.loads(i['output']) for i in seen[0]['input'] if i.get('type')=='function_call_output']
    assert returned[-1]['repair_feedback']['error_code']=='activity_label_not_quoted' and not labels(api)
    assert result.status=='clarification'


def test_choosing_some_dates_of_a_title_is_not_remembered_for_the_title():
    api=MemoryHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    ask(api)
    result,_=turn(api,'27 ต.ค. เป็นคุมสอบ',answer(lambda rows:[r['candidate_ref'] for r in rows if r['start'].startswith('2026-10-27')]))
    assert result.message.startswith('คุมสอบ 1 นัดครับ') and 'จำไว้แล้ว' not in result.message and not labels(api)


@pytest.mark.parametrize('title,activity',[('x'*201,'คุมสอบ'),('DSDE Exam','x'*81)])
def test_whole_title_answers_outside_memory_bounds_do_not_break_the_next_restore(title,activity):
    from types import SimpleNamespace
    from line_assistant.query_followups import learn
    state=HistoryState();state.owner_id='owner'
    host=SimpleNamespace(state=state,activity_memory_enabled=True,
        _current={'source_ref':'msg_1','at':'2026-10-24T17:00:00+07:00'})
    candidate={'title':title,'candidate_ref':'candidate_1'}
    learned=learn(host,{'scope':{'filter_text':activity},'candidates':[candidate]},[candidate],'match')
    restored=HistoryState();restore_history(restored,history_snapshot(state),'owner')
    assert restored.activity_labels==state.activity_labels=={} and learned['titles']==[]


def test_failed_expense_read_does_not_silently_remember_the_activity_answer(monkeypatch):
    from line_assistant import read_queries
    from line_assistant.query_followups import execute
    from types import SimpleNamespace
    state=HistoryState();state.owner_id='owner'
    candidate={'title':'DSDE Exam','candidate_ref':'candidate_1','start':'2026-10-24T08:00:00+07:00',
        'end':'2026-10-24T12:00:00+07:00','all_day':False,'read_ref':'read_1'}
    pending={'goal':'expense','ref':'follow_1','scope':{'start':'2026-10-24','end':'2026-10-24','filter_text':'คุมสอบ'},
        'candidates':[candidate],'confirmed':[],'unconfirmed_count':1}
    host=SimpleNamespace(state=state,settings=SimpleNamespace(owner_id='owner'),activity_memory_enabled=True,_query_reads={},_active_event_id='event_1',
        _current={'source_ref':'msg_1','text':'ใช่','at':'2026-10-24T17:00:00+07:00'},
        _owner_sources=lambda refs:[],calendar_flow=SimpleNamespace(enabled=True,
            adapter=SimpleNamespace(saved_events=lambda *a,**k:[candidate])))
    from line_assistant import query_followups
    monkeypatch.setattr(query_followups,'current',lambda h:pending)
    monkeypatch.setattr(read_queries,'execute',lambda *a,**k:{'complete':False,'error_code':'provider_failure'})
    result=execute(host,{'clarification_ref':'follow_1','candidate_refs':['candidate_1'],'decision':'confirm',
        'selection_text':'ใช่','source_refs':['msg_1']})
    assert not result['ok'] and result['error_code']=='expense_unavailable' and not state.activity_labels


def test_disabled_memory_neither_stores_nor_applies_labels():
    api=MemoryHosted(enabled=False);exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    result=confirm_exam(api)
    assert 'จำไว้แล้ว' not in result.message and 'line_message' not in result.data and not labels(api)
    names={t['name'] for t in api.model_bodies[-1]['tools']}
    assert 'update_activity_label' not in names


def test_labels_use_a_separate_optional_snapshot_key_that_restores_and_rejects_tampering():
    state=HistoryState();state.owner_id='owner'
    plain=history_snapshot(state)
    state.activity_labels={}
    from line_assistant.activity_labels import key
    state.activity_labels[key('คุมสอบ','DSDE Exam')]={'activity':'คุมสอบ','title':'DSDE Exam','decision':'match','source_ref':'msg_1','at':'2026-10-24T17:00:00+07:00'}
    value=history_snapshot(state)
    # Only the optional key differs, so earlier runtimes keep loading the state.
    assert {k:v for k,v in value.items() if k!='activity_labels_v1'}==plain and value['format']==plain['format']
    fresh=HistoryState();restore_history(fresh,value,'owner')
    assert fresh.activity_labels==state.activity_labels
    value['activity_labels_v1']['labels'][key('คุมสอบ','DSDE Exam')]['decision']='uncertain'
    with pytest.raises(ValueError):restore_history(HistoryState(),value,'owner')


@pytest.mark.parametrize('calendar,voice,ok',[('true','true',True),('false','true',False),('true','false',False)])
def test_memory_requires_calendar_reads_and_semantic_classification(calendar,voice,ok):
    from line_assistant.history_cloud_run import HistoryConfig
    from line_assistant.runtime_config import ConfigError
    from test_calendar_read_all import values
    config={**values(True),'PILOT_CALENDAR_ENABLED':calendar,'PILOT_RESPONSE_REALIZATION_ENABLED':voice,'PILOT_ACTIVITY_MEMORY_ENABLED':'true'}
    if calendar=='false':config.pop('PILOT_CALENDAR_ID',None)
    if ok:assert HistoryConfig.from_mapping(config).activity_memory_enabled
    else:
        with pytest.raises(ConfigError):HistoryConfig.from_mapping(config)


@pytest.mark.parametrize('decision,disclosed',[('match',True),('uncertain',False)])
def test_other_wording_of_a_remembered_activity_is_a_disclosed_hint_not_a_rule(decision,disclosed):
    from test_read_queries import plan
    api=MemoryHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    confirm_exam(api);before=len(api.selection_bodies)
    api.labels={'e1':decision,'e2':'exclude'}
    result,_=turn(api,'มีวันไหนต้องไปงานคุมสอบบ้างจนถึงธันวา',plan('calendar','2026-10-24','2026-12-31',filter_text='งานคุมสอบ'))
    payload=json.loads(api.selection_bodies[before]['input'][0]['content'])
    assert [e['title'] for e in payload['calendar_selection']['events']]==['DSDE Exam','facilitator']
    assert payload['owner_labels']==[{'title':'DSDE Exam','activity':'คุมสอบ','decision':'match'}]
    assert ('นับ DSDE Exam โดยอิงที่เคยยืนยันว่าเป็นคุมสอบ' in result.message)==disclosed


def test_without_memory_the_classifier_receives_no_owner_labels():
    api=SemanticHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    ask(api)
    assert json.loads(api.selection_bodies[0]['input'][0]['content'])['owner_labels']==[]


def test_an_expense_total_on_remembered_activity_dates_discloses_the_label():
    from test_query_analysis_loop import read,finish
    from test_expense_query_operations import seed,expense
    api=MemoryHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    confirm_exam(api);before=len(api.selection_bodies)
    seed(api,[('2026-11-01','ค่าข้าว',120),('2026-11-25','ค่ารถ',45),('2026-11-12','ค่าอาหาร',500)])
    from datetime import datetime
    api.now=datetime.fromisoformat('2026-12-03T09:00:00+07:00');api.labels={'e1':'exclude'}
    result,_=turn(api,'เดือนพฤศจิกายนใช้เงินวันที่ไปคุมสอบไปเท่าไร',
        read('calendar','2026-11-01',date_mode='calendar_month',filter_text='คุมสอบ'),
        expense('2026-11-01','2026-11-30',view='total',continue_analysis=True,join=True),finish('report'))
    # Both the Calendar read and the expense basis it produced say which label they used.
    assert '165.00 บาท' in result.message and result.message.count('นับ DSDE Exam เป็นคุมสอบตามที่เคยยืนยันไว้')==2
    assert 'read_followup' not in result.data and len(api.selection_bodies)==before


def test_a_calendar_read_for_spending_keeps_the_expense_goal_even_without_an_expense_call():
    from datetime import datetime
    from test_read_queries import plan
    from test_expense_query_operations import seed
    # November holds only the two DSDE Exam occurrences: one classifier group.
    api=SemanticHosted();exams(api);api.labels={'e1':'uncertain'}
    seed(api,[('2026-11-01','ค่าข้าว',120),('2026-11-25','ค่ารถ',45),('2026-11-12','ค่าอาหาร',500)])
    api.now=datetime.fromisoformat('2026-12-03T09:00:00+07:00')
    def spending(body):
        answer=plan('calendar','2026-11-01','2026-11-30',filter_text='คุมสอบ')(body);call=answer['output'][0]
        call['arguments']=json.dumps({**json.loads(call['arguments']),'purpose':'expense_dates'},ensure_ascii=False);return answer
    first,_=turn(api,'เดือนพฤศจิกายนใช้เงินวันที่ไปคุมสอบไปเท่าไร',spending)
    assert first.data['read_followup']['goal']=='expense' and 'แล้วผมจะรวมรายจ่ายของวันที่ยืนยันให้ครับ' in first.message
    result,_=turn(api,'DSDE Exam เป็นคุมสอบ',answer(lambda rows:[r['candidate_ref'] for r in rows]))
    assert '165.00 บาท' in result.message and 'คุมสอบ 2 นัดครับ' not in result.message


def test_a_count_summary_keeps_the_remembered_label_disclosure():
    from test_query_analysis_loop import read,finish
    api=MemoryHosted();exams(api);api.labels={'e1':'uncertain','e2':'exclude'}
    confirm_exam(api);api.labels={'e1':'exclude'}
    result,_=turn(api,'เดือนหน้ามีคุมสอบกี่วัน',read('calendar','2026-11-01',date_mode='calendar_month',filter_text='คุมสอบ'),finish('calendar_count'))
    assert 'มี 2 ครั้งครับ' in result.message and 'นับ DSDE Exam เป็นคุมสอบตามที่เคยยืนยันไว้' in result.message
