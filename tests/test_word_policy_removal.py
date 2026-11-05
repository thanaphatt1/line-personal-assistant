"""Language can vary; typed data, owner bindings and actual effects stay checked."""
from copy import deepcopy
from datetime import datetime
import json
import socket

import pytest

from line_assistant import response_realization as voice
from line_assistant.contracts import Transaction
from line_assistant.state import Result
from line_assistant.tool_errors import recovery
from test_calendar_mvp import response
from test_intent_actions import harness,read,respond
from test_response_realization import generated


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)


@pytest.mark.parametrize('period',[None,'เดือนนี้','พฤศจิกายนนี้'])
def test_typo_in_owner_month_does_not_block_native_read_or_require_word_repair(period):
    h=harness();h.now=datetime.fromisoformat('2026-11-05T07:26:00+07:00')
    h.sheets.entries=[Transaction(item='BTS',amount=amount,kind='expense',date=day)
        for amount,day in [('10','2026-11-01'),('20','2026-11-05'),('1000','2026-10-31')]]
    source={'source':'expense','alias':'e','parameters':{'date_mode':'calendar_month',
        'start_date':None,'end_date':None,'relative_offset':0,'period_text':period}}
    events=[];h.app.action_trace_sink=events.append
    r=h.send('เดือนนีใ้ช้ตังกี่บาทละ',read(sources=[source]),respond())
    assert r.status=='answer' and len(h.model.requests)==2
    result=next(v for v in h.app._query_reads.values() if v.get('relational_query'))
    assert result['rows']==[{'total':'30'}]
    assert r.data['read_query']['start']=='2026-11-01' and r.data['read_query']['end']=='2026-11-30'
    assert not any(v['phase']=='planning_transition' and v.get('reason')=='argument_repair' for v in events)
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts


@pytest.mark.parametrize('phrase',[
    'ช่วยทบทวนรายละเอียดที่ยังไม่ได้บันทึกได้ไหมครับ?',
    'กรุณาตรวจชื่อเดิม ก่อนยืนยันรายการครับ',
    'Saved is a word in the explanation, please check the details.',
    'ข้อความเชื่อมประโยคที่แต่งได้เองครับ',
])
def test_ordinary_fact_reply_accepts_free_prose_and_keeps_exact_facts(phrase):
    class Host:
        _current={};_active_event_id='synthetic'
        class state:history=[]
    body=voice.body_for(Host(),Result('clarification','ข้อมูลที่ต้องตรวจสอบ',{}))
    payload=json.loads(body['input'][0]['content'])
    assert 'allowed_speech_words' not in payload and voice.validate_body(body)
    schema=body['text']['format']['schema']
    assert 'pattern' not in schema['properties']['opening']
    parts=[{'kind':'text','value':phrase},*[{'kind':'fact','value':k} for k in payload['facts']]]
    text=voice.parse(generated(body,parts),body)
    assert phrase in text and 'ข้อมูลที่ต้องตรวจสอบ' in text


@pytest.mark.parametrize('damage',['fact','number','url','control'])
def test_free_prose_still_requires_fact_ids_and_bound_literal_data(damage):
    class Host:
        _current={};_active_event_id='synthetic'
        class state:history=[]
    body=voice.body_for(Host(),Result('clarification','ข้อมูลที่ต้องตรวจสอบ',{}))
    text={'fact':'', 'number':'ยอด 999 บาท','url':'https://example.invalid','control':'คำ\x00'}[damage]
    parts=[{'kind':'fact','value':'status' if damage!='fact' else 'missing'}]
    if text:parts.insert(0,{'kind':'text','value':text})
    with pytest.raises(ValueError):voice.parse(generated(body,parts),body)


def test_query_fact_connectors_are_free_prose_not_phrase_enums(monkeypatch):
    class Host:
        _current={};_active_event_id='synthetic'
        class state:history=[]
    monkeypatch.setattr(voice.answer_composition,'for_result',lambda *a:None)
    r=Result('summary','checked',{'query_facts':{'period':'เดือนพฤศจิกายน','total':'30 บาท'}})
    body=voice.body_for(Host(),r);assert voice.validate_body(body)
    schema=body['text']['format']['schema']
    assert schema['properties']['parts']['properties']['total']['properties']['before']=={'type':'string','maxLength':100}
    parts={'period':{'before':'รายการที่บันทึกไว้สำหรับ ','after':'\n'},
        'total':{'before':'ค่าใช้จ่ายสะสมเท่ากับ ','after':' ครับ'}}
    assert voice.parse(generated(body,parts),body)=='รายการที่บันทึกไว้สำหรับ เดือนพฤศจิกายน\nค่าใช้จ่ายสะสมเท่ากับ 30 บาท ครับ'


@pytest.mark.parametrize('kind',['assistant_arguments','unavailable'])
def test_source_failure_keeps_diagnosis_and_only_argument_errors_can_repair(kind,monkeypatch):
    h=harness();original=h.app.tool;calls=[]
    def tool(call):
        if call['name']=='query_expenses':
            calls.append(call)
            if len(calls)==1:
                return {'ok':False,'complete':False,'domain':'expense','status':'clarification','outcome':'clarification',
                    'error_code':'invalid_arguments' if kind=='assistant_arguments' else 'read_capacity',
                    'recovery':recovery(kind=kind,fields=['date_mode']),
                    'scope':{'domain':'expense'},'read_only':True}
        return original(call)
    monkeypatch.setattr(h.app,'tool',tool)
    sources=[{'source':'expense','alias':'e','parameters':{'date_mode':'calendar_month',
        'start_date':'2026-10-01','end_date':None,'relative_offset':None,'period_text':'เดือนนี้'}}]
    second=deepcopy(sources);second[0]['parameters']['period_text']='ตุลาคม'
    seen=[]
    def repaired(body):
        if not body['tools']:return generated(body)
        seen.append(body)
        feedback=json.loads(body['input'][-1]['output'])['repair_feedback']
        assert feedback['error_code']=='invalid_arguments'
        assert feedback['invalid_fields']==['date_mode'] and 'act' in feedback['allowed_tools']
        value=read(sources=second)(body);value['output'][0]['call_id']='read_repaired'
        return value
    r=h.send('เดือนนี้รวมเท่าไหร่',read(sources=sources),repaired,respond())
    if kind=='assistant_arguments':
        assert r.status=='answer' and len(calls)==2 and len(seen)==1
    else:
        out=r.data['tool_result']
        assert r.status=='clarification' and len(calls)==1 and not seen
        assert out['error_code']=='query_incomplete_source' and out['source_errors'][0]['error_code']=='read_capacity'
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts


@pytest.mark.parametrize('wording',['ผมจะเรียงให้ครับ','บันทึกรายการให้เรียบร้อยแล้วครับ','Your calendar is now connected.'])
def test_toolless_wording_cannot_create_a_verified_operation(wording):
    h=harness();r=h.send('อธิบายประโยคนี้',response(text=wording))
    assert r.status=='answer' and r.message==wording and len(h.model.requests)==1
    assert not h.state.actions and not h.state.calendar_drafts and not h.sheets.write_log and not h.provider.posts


def test_correcting_one_source_reuses_other_complete_snapshot(monkeypatch):
    h=harness();original=h.app.tool;failed=[];fetches=[]
    saved=h.sheets.saved_entries
    def snapshot(*args):fetches.append(args);return saved(*args)
    monkeypatch.setattr(h.sheets,'saved_entries',snapshot)
    def tool(call):
        args=json.loads(call['arguments'])
        if call['name']=='query_expenses' and args['start_date']=='2026-10-01':
            failed.append(True)
            if len(failed)==1:
                return {'ok':False,'complete':False,'domain':'expense','status':'clarification','outcome':'clarification',
                    'error_code':'invalid_arguments','recovery':recovery(kind='assistant_arguments',fields=['date_mode']),
                    'scope':{'domain':'expense'},'read_only':True}
        return original(call)
    monkeypatch.setattr(h.app,'tool',tool)
    sources=[{'source':'expense','alias':alias,'parameters':{'date_mode':'calendar_month',
        'start_date':day,'end_date':None,'relative_offset':None,'period_text':period}}
        for alias,day,period in [('e','2026-10-01','ตุลาคม'),('previous','2026-09-01','กันยายน')]]
    fixed=deepcopy(sources);fixed[0]['parameters']['period_text']='เดือนตุลาคม'
    sql='SELECT (SELECT SUM(amount) FROM e) AS current_total,(SELECT SUM(amount) FROM previous) AS previous_total'
    def repair(body):
        value=read(sql,sources=fixed)(body);value['output'][0]['call_id']='repair_one_source';return value
    result=h.send('เทียบยอดกันยายนและตุลาคม',read(sql,sources=sources),repair,respond())
    assert result.status=='answer' and len(failed)==2
    assert len(fetches)==2  # One successful read per period; the other source is reused.
    assert len(h.model.requests)==3 and not h.sheets.write_log and not h.state.actions


def test_mixed_argument_and_provider_failures_cannot_retry_or_run_sql(monkeypatch):
    h=harness();h.app.response_realization_enabled=False;original=h.app.tool;calls=[]
    def tool(call):
        if call['name']=='query_expenses':
            kind='assistant_arguments' if not calls else 'unavailable';calls.append(kind)
            return {'ok':False,'complete':False,'domain':'expense','status':'clarification','outcome':'clarification',
                'error_code':'invalid_arguments' if kind=='assistant_arguments' else 'read_capacity',
                'recovery':recovery(kind=kind,fields=['date_mode']),
                'scope':{'domain':'expense','start':json.loads(call['arguments'])['start_date']},'read_only':True}
        return original(call)
    monkeypatch.setattr(h.app,'tool',tool)
    sources=[{'source':'expense','alias':alias,'parameters':{'date_mode':'calendar_month',
        'start_date':day,'end_date':None,'relative_offset':None,'period_text':None}}
        for alias,day in [('e','2026-10-01'),('other','2026-09-01')]]
    result=h.send('เทียบสองเดือน',read(sources=sources));output=result.data['tool_result']
    assert result.status=='clarification' and len(h.model.requests)==1 and len(calls)==2
    assert output['error_code']=='query_incomplete_source' and output['recovery']['kind']=='unavailable'
    assert [v['error_code'] for v in output['source_errors']]==['invalid_arguments','read_capacity']
    assert not any(v.get('relational_query') for v in h.app._query_reads.values())
    assert not h.sheets.write_log and not h.state.actions
