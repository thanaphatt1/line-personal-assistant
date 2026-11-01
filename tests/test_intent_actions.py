"""Native intent plans compose checked read replies without another model call."""
from copy import deepcopy
import json
import socket

import pytest
from test_calendar_mvp import response
from test_unified_query import harness as query_harness, source


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs): raise AssertionError('Intent tests use synthetic providers only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def harness():
    h=query_harness();h.app.response_realization_enabled=True
    return h


def read(sql='SELECT SUM(amount) AS total FROM e', *, sources=None):
    def choose(body):
        c=json.loads(body['input'][0]['content'])
        return response('act',{'request':{'intent':'read','sources':sources or [source()],
            'sql':sql,'source_refs':[c['current_source_ref']]}})
    return choose


def document(view):
    return {'blocks':[
        *[{'kind':'text','template':'{{'+key+'}}'} for key in view['facts']],
        *[{'kind':'rows','record_refs':[row['id']],'heading':'',
           'template':' | '.join('{{'+key+'}}' for key in row['fields'])} for row in view['records']]]}


def respond(*, mutate=None):
    def choose(body):
        c=json.loads(body['input'][0]['content']);offered=c['read_responses'][-1]
        args={'intent':'respond','response_ref':offered['response_ref'],
            'review':{'selection_matches':True,'output_matches':True,'unresolved_conditions':[]},
            'document':document(offered['facts']),'source_refs':[c['current_source_ref']]}
        if mutate:mutate(args)
        value=response('act',{'request':args});value['output'][0]['call_id']='respond_'+str(len(body['input']))
        return value
    return choose


def test_read_review_and_composition_complete_in_two_model_calls():
    h=harness();rows=[];h.app.action_trace_sink=rows.append
    r=h.send('เดือนตุลาคม BTS รวมเท่าไร',read("SELECT SUM(amount) AS total FROM e WHERE item='BTS'"),respond())
    assert r.status=='answer' and ('0.30' in r.message or '0.3' in r.message),r
    assert len(h.model.requests)==2
    assert r.data['response_realization']['mode']=='planner_intent'
    assert all(body['tools'] for body in h.model.requests)  # No auxiliary reply request.
    assert any(v['phase']=='intent_response' and v['status']=='verified' for v in rows)
    assert [v['intent'] for v in rows if v['phase']=='intent_selected']==['read','respond']
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts
    names={t['name'] for t in h.model.requests[0]['tools']}
    assert 'act' in names and not names&{'query','inspect_data_sources'}
    act=next(t for t in h.model.requests[0]['tools'] if t['name']=='act')
    variants=act['parameters']['properties']['request']['anyOf']
    native_read=next(v for v in variants if v['properties']['intent']['enum']==['read'])
    assert 'continue_analysis' not in native_read['properties']
    encoded=json.dumps(rows,ensure_ascii=False)
    assert 'BTS' not in encoded and 'SELECT SUM' not in encoded and 'เดือนตุลาคม' not in encoded


@pytest.mark.parametrize('mutation',['money','drop_cell','foreign_response','owner','review','unknown_intent'])
def test_bad_final_intents_do_not_publish_unverified_answers(mutation):
    h=harness()
    def damage(args):
        if mutation=='money':args['document']['blocks'].insert(0,{'kind':'text','template':'ยอด 999 บาท'})
        elif mutation=='drop_cell':args['document']['blocks'][-1]['template']=''
        elif mutation=='foreign_response':args['response_ref']='answer_foreign'
        elif mutation=='owner':args['source_refs']=['assistant_source']
        elif mutation=='review':args['review']['selection_matches']=False
        else:args['intent']='execute_write'
    r=h.send('เดือนตุลาคม รวมเท่าไร',read(),respond(mutate=damage),response(text='ยังสรุปไม่ได้ครับ'))
    assert r.status=='clarification',r
    assert '999 บาท' not in r.message and not r.data.get('response_realization')
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts


def test_received_invalid_document_can_be_corrected_without_rereading_sources(monkeypatch):
    h=harness();reads=[];original=h.sheets.saved_entries
    def load(*args):reads.append(args);return original(*args)
    monkeypatch.setattr(h.sheets,'saved_entries',load)
    def bad(args):args['document']['blocks'].insert(0,{'kind':'text','template':'999 บาท'})
    r=h.send('เดือนตุลาคม รวมเท่าไร',read(),respond(mutate=bad),respond())
    assert r.status=='answer' and len(h.model.requests)==3 and len(reads)==1,r
    assert r.data['response_realization']['mode']=='planner_intent'


def test_a_repeated_invalid_response_stops_bounded_repair_without_retrying_reads():
    h=harness()
    def bad(args):args['review']['selection_matches']=False
    r=h.send('เดือนตุลาคม รวมเท่าไร',read(),respond(mutate=bad),respond(mutate=bad))
    assert r.status=='clarification' and r.data['query_loop_stopped']=='repeated_read',r
    assert len(h.model.requests)==3 and not h.sheets.write_log


def test_completed_intent_receipt_replays_without_model_or_read_calls():
    h=harness();h.model.values=[read(),respond()];event=h.event('เดือนตุลาคม รวมเท่าไร')
    first=h.app.handle(event);calls=len(h.model.requests)
    assert h.app.handle(deepcopy(event))==first and len(h.model.requests)==calls


def test_old_response_refs_are_not_offered_or_accepted_on_a_new_turn():
    h=harness();first=h.send('เดือนตุลาคม รวมเท่าไร',read(),respond())
    old=first.data['response_realization']['response_ref']
    def reuse(body):
        c=json.loads(body['input'][0]['content'])
        assert not c['read_responses']
        return response('act',{'request':{'intent':'respond','response_ref':old,
            'review':{'selection_matches':True,'output_matches':True,'unresolved_conditions':[]},
            'document':{'blocks':[{'kind':'text','template':'{{f1}}'}]},'source_refs':[c['current_source_ref']]}})
    r=h.send('ดูผลเดิม',reuse,response(text='ต้องอ่านข้อมูลใหม่ครับ'))
    assert r.status=='clarification' and r.data['tool_result']['error_code']=='response_reference_unavailable'


def test_inspection_uses_the_same_intent_entry_point():
    h=harness()
    def inspect(body):
        c=json.loads(body['input'][0]['content'])
        return response('act',{'request':{'intent':'inspect','source':'capabilities',
            'continue_analysis':False,'source_refs':[c['current_source_ref']]}})
    r=h.send('ดูได้อะไรบ้าง',inspect)
    assert r.status=='answer' and r.data['data_inventory'],r
    assert len(h.model.requests)==1 and not h.sheets.write_log


def test_response_proof_binds_actual_display_facts_not_just_a_result_id():
    h=harness()
    def stale(body):
        value=respond()(body)
        row=next(v for v in h.app._query_reads.values() if v.get('relational_query'))
        row['rows'][0]['total']='777'
        return value
    r=h.send('เดือนตุลาคม รวมเท่าไร',read(),stale,response(text='ต้องตรวจผลใหม่ครับ'))
    assert r.status=='clarification' and r.data['tool_result']['error_code']=='response_reference_unavailable'
    assert '777' not in r.message and not h.sheets.write_log


def test_response_view_budget_is_bounded_and_has_no_provider_or_model_effect(monkeypatch):
    from line_assistant import intent_actions
    h=harness();h.send('เดือนตุลาคม รวมเท่าไร',read(),respond())
    calls=len(h.model.requests)
    monkeypatch.setattr(intent_actions,'MAX_RESPONSE_BYTES',100)
    assert intent_actions.response_views(h.app)=={}
    assert len(h.model.requests)==calls and not h.sheets.write_log
