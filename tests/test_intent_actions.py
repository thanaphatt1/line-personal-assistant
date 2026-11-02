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
            'document':document(offered['facts'])}
        if mutate:mutate(args)
        value=response('act',{'request':args});value['output'][0]['call_id']='respond_'+str(len(body['input']))
        return value
    return choose


def test_read_selection_and_composition_complete_in_two_model_calls():
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
        elif mutation=='review':args['review']={'selection_matches':False,'output_matches':True,'unresolved_conditions':[]}
        else:args['intent']='execute_write'
    r=h.send('เดือนตุลาคม รวมเท่าไร',read(),respond(mutate=damage),response(text='ยังสรุปไม่ได้ครับ'))
    assert '999 บาท' not in r.message
    if mutation in {'money','drop_cell'}:
        assert r.status=='answer' and r.data['response_realization']['status']=='fallback',r
        assert '99.3' in r.message and len(h.model.requests)==2
    else:
        assert r.status=='clarification' and not r.data.get('response_realization'),r
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts


def test_received_invalid_document_falls_back_without_another_model_or_source_call(monkeypatch):
    h=harness();reads=[];original=h.sheets.saved_entries
    def load(*args):reads.append(args);return original(*args)
    monkeypatch.setattr(h.sheets,'saved_entries',load)
    def bad(args):args['document']['blocks'].insert(0,{'kind':'text','template':'999 บาท'})
    r=h.send('เดือนตุลาคม รวมเท่าไร',read(),respond(mutate=bad),respond())
    assert r.status=='answer' and len(h.model.requests)==2 and len(reads)==1,r
    assert r.data['response_realization']['mode']=='planner_intent'
    assert r.data['response_realization']['status']=='fallback' and '999' not in r.message


def test_a_repeated_invalid_response_stops_bounded_repair_without_retrying_reads():
    h=harness()
    def bad(args):args['review']={'selection_matches':False,'output_matches':True,'unresolved_conditions':[]}
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


def test_response_schema_binds_the_result_without_self_review_or_duplicate_owner_refs():
    h=harness();h.send('เดือนตุลาคม รวมเท่าไร',read(),respond())
    body=h.model.requests[1];context=json.loads(body['input'][0]['content'])
    act=next(t for t in body['tools'] if t['name']=='act')
    spec=next(v for v in act['parameters']['properties']['request']['anyOf'] if v['properties']['intent']['enum']==['respond'])
    assert set(spec['properties'])=={'intent','response_ref','document'}
    assert spec['properties']['response_ref']['enum']==[v['response_ref'] for v in context['read_responses']]
    assert context['read_responses'][0]['query_ref']!=context['current_source_ref']


def test_single_result_can_answer_inline_without_layout_fallback_or_repeated_evidence():
    h=harness();trace=[];h.app.action_trace_sink=trace.append
    def inline(args):
        args['document']={'blocks':[{'kind':'text','template':'{{f1}} รวม {{c1}}ครับ'}]}
    result=h.send('เดือนตุลาคม BTS รวมเท่าไร',read("SELECT SUM(amount) AS total FROM e WHERE item='BTS'"),respond(mutate=inline))
    assert result.status=='answer' and '0.30 บาท' in result.message,result
    assert result.data['response_realization']['status']=='llm' and len(h.model.requests)==2
    assert not any(row['phase']=='reply_fallback' for row in trace)
    call=next(row['call'] for row in h.app.trace if row['call']['name']=='act' and json.loads(row['call']['arguments'])['request']['intent']=='respond')
    assert set(json.loads(call['arguments'])['request'])=={'intent','response_ref','document'}
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts


def test_finish_analysis_offers_no_self_review_and_can_select_actual_results_without_it():
    from test_unified_query import finish
    h=harness();h.app.response_realization_enabled=False
    def minimal(body):
        tool=next(t for t in body['tools'] if t['name']=='finish_query_analysis')
        assert 'intent_review' not in tool['parameters']['properties']
        value=finish(body);args=json.loads(value['output'][0]['arguments']);args.pop('intent_review')
        value['output'][0]['arguments']=json.dumps(args)
        return value
    result=h.send('เดือนตุลาคม BTS รวมเท่าไร',read("SELECT SUM(amount) AS total FROM e WHERE item='BTS'"),minimal)
    assert result.status=='answer' and '0.30' in result.message and len(h.model.requests)==2,result
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts


def test_mixed_owner_and_query_refs_receive_specific_repair_without_rereading(monkeypatch):
    h=harness();reads=[];original=h.sheets.saved_entries
    def load(*args):reads.append(1);return original(*args)
    monkeypatch.setattr(h.sheets,'saved_entries',load)
    def mixed(body):
        value=respond()(body);context=json.loads(body['input'][0]['content'])
        args=json.loads(value['output'][0]['arguments'])
        args['request']['source_refs']=[context['current_source_ref'],context['read_responses'][0]['query_ref']]
        value['output'][0]['arguments']=json.dumps(args,ensure_ascii=False)
        return value
    result=h.send('เดือนตุลาคม รวมเท่าไร',read(),mixed,respond())
    assert result.status=='answer' and len(h.model.requests)==3 and len(reads)==1
    bad=next(r['result'] for r in h.app.trace if r['result'].get('error_code')=='response_owner_source_required')
    assert bad['recovery']['provider_retry_allowed'] is False
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts


@pytest.mark.parametrize('damage',['literal_year','row_cell_in_text','invented_slot','missing_fact','missing_record','swapped_records'])
def test_observed_formatter_failures_render_all_verified_values_without_repair(damage):
    h=harness();trace=[];h.app.action_trace_sink=trace.append
    def bad(args):
        blocks=args['document']['blocks']
        if damage=='literal_year':blocks[0]['template']+=' ปี 2026'
        elif damage=='row_cell_in_text':blocks[0]['template']+=' {{c1}}'
        elif damage=='invented_slot':blocks[0]['template']+=' {{cell_id:c1}}'
        elif damage=='missing_fact':blocks.pop(0)
        elif damage=='missing_record':blocks.pop()
        else:blocks[-2:]=list(reversed(blocks[-2:]))
    r=h.send('เดือนตุลาคมแยกยอดตามรายการ',
        read('SELECT item,SUM(amount) AS total FROM e GROUP BY item ORDER BY total DESC'),respond(mutate=bad))
    assert r.status=='answer' and len(h.model.requests)==2,r
    assert r.data['response_realization']['status']=='fallback'
    assert r.message.index('Coffee')<r.message.index('BTS')
    assert '99 บาท' in r.message and '0.30 บาท' in r.message and 'เดือนตุลาคม 2026' in r.message
    assert '{{' not in r.message
    assert any(row['phase']=='reply_fallback' and row['reason']=='invalid_answer_document' for row in trace)
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts


def test_followup_has_selected_group_and_period_but_requires_a_fresh_read(monkeypatch):
    h=harness();reads=[];original=h.sheets.saved_entries
    def load(*args):reads.append(1);return original(*args)
    monkeypatch.setattr(h.sheets,'saved_entries',load)
    def bad(args):args['document']['blocks'][0]['template']+=' ปี 2026'
    first=h.send('เดือนตุลาคมรายการไหนบ่อยสุด',
        read('SELECT item,COUNT(*) AS n FROM e GROUP BY item ORDER BY n DESC LIMIT 1'),respond(mutate=bad))
    assert first.status=='answer' and 'BTS' in first.message
    snapshot=first.data['read_query']['answer_context']
    assert snapshot['records']==[{'item':'BTS','n':2}]
    assert snapshot['requires_fresh_read'] is True and snapshot['shown']==1
    def followup(body):
        c=json.loads(body['input'][0]['content']);previous=c['previous_query']
        assert previous['start']=='2026-10-01' and previous['end']=='2026-10-31'
        assert previous['answer_context']==snapshot
        assert not c['read_responses'] and not h.app._query_reads
        selected=previous['answer_context']['records'][0]['item']
        assert selected=='BTS'  # Scripted interpretation, not fresh model accuracy.
        s=source();s['parameters']={**dict.fromkeys(s['parameters']),'date_mode':'previous'}
        return read("SELECT SUM(amount) AS total FROM e WHERE item='"+selected+"'",sources=[s])(body)
    second=h.send('รวมเป็นเงินเท่าไหร่',followup,respond())
    assert second.status=='answer' and '0.30 บาท' in second.message and '99.3' not in second.message,second
    assert len(reads)==2 and len(h.model.requests)==4
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts


def test_oversized_previous_answer_is_omitted_entirely_without_blocking_the_answer(monkeypatch):
    from line_assistant import intent_actions
    monkeypatch.setattr(intent_actions,'MAX_ANSWER_CONTEXT_BYTES',10)
    h=harness();first=h.send('เดือนตุลาคมรวมเท่าไร',read(),respond())
    assert first.status=='answer' and 'answer_context' not in first.data['read_query']
    def inspect(body):
        previous=json.loads(body['input'][0]['content'])['previous_query']
        assert previous['start']=='2026-10-01' and 'answer_context' not in previous
        return response(text='ต้องการดูรายการไหนครับ')
    h.send('แล้วล่ะ',inspect)


@pytest.mark.parametrize('fault',[None,'wrong_workbook','delivery_failed','expired'])
def test_previous_selected_answer_obeys_signed_delivery_scope_and_expiry_gates(fault):
    from datetime import timedelta
    from test_response_realization import VoiceHosted
    from test_calendar_text_confirmation_v13 import turn,mutate
    from test_expense_query_operations import seed
    api=VoiceHosted();seed(api,[('2026-10-15','ชา',20)])
    api.reject_reply=fault=='delivery_failed'
    _,original=turn(api,'เดือนตุลาคม รวมเท่าไร',read(),respond())
    api.reject_reply=False
    if fault=='wrong_workbook':mutate(api,lambda state:state.event_workbooks.__setitem__(original['webhookEventId'],'other-book'))
    if fault=='expired':api.now+=timedelta(hours=25)
    def inspect(body):
        previous=json.loads(body['input'][0]['content'])['previous_query']
        if fault:assert previous is None
        else:
            assert previous['answer_context']['records']==[{'total':'20'}]
            assert previous['answer_context']['requires_fresh_read'] is True
        return response(text='ต้องการดูรายการไหนครับ')
    turn(api,'แล้วล่ะ',inspect)
