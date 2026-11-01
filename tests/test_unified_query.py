"""General source plans, same-call computation and observable safe failures."""
from copy import deepcopy
import json
import socket

import pytest

from line_assistant import action_trace
from line_assistant.contracts import Transaction
from test_calendar_mvp import Harness, response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Only synthetic query providers')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def source():
    return {'source': 'expense', 'alias': 'e', 'parameters': {
        'date_mode': 'calendar_month', 'start_date': '2026-10-01', 'end_date': None,
        'period_text': 'เดือนตุลาคม', 'relative_offset': None}}


def query(sql, *, sources=None, mutate=None):
    def choose(body):
        context = json.loads(body['input'][0]['content'])
        args = {'sources': deepcopy(sources if sources is not None else [source()]),
                'sql': sql, 'source_refs': [context['current_source_ref']], 'continue_analysis': True}
        if mutate:
            mutate(args)
        value = response('query', args)
        value['output'][0]['call_id'] = 'query_' + str(len(body['input']))
        return value
    return choose


def finish(body):
    context = json.loads(body['input'][0]['content'])
    result = json.loads(body['input'][-1]['output'])
    return response('finish_query_analysis', {
        'query_refs': [result['query_ref']], 'operation': 'report', 'question': None,
        'intent_review': {'selection_matches': True, 'output_matches': True, 'unresolved_conditions': []},
        'source_refs': [context['current_source_ref']]})


def harness():
    h = Harness()
    h.sheets.rows = [('BTS', 'Transportation', ''), ('Coffee', 'Food', '')]
    h.sheets.entries = [Transaction(item=item, amount=amount, kind='expense', date=day)
        for item, amount, day in [('BTS', '0.10', '2026-10-01'), ('BTS', '0.20', '2026-10-02'),
                                 ('Coffee', '99', '2026-10-02'), ('BTS', '999', '2026-09-30')]]
    return h


def test_one_query_fetches_and_computes_with_aliases_known_before_reading():
    h = harness()
    result = h.send('เดือนตุลาคม BTS รวมเท่าไร', query("SELECT SUM(amount) AS total FROM e WHERE canonical_item='BTS'"), finish)
    assert result.status == 'answer', result
    assert result.data['tool_result']['query_refs']
    assert '0.30' in result.message or '0.3' in result.message
    assert len(h.model.requests) == 2
    first = h.model.requests[0]
    assert 'query_source_schemas' in json.loads(first['input'][0]['content'])
    names = {t['name'] for t in first['tools']}
    assert 'act' in names and not names & {'query_expenses', 'query_calendar', 'query_relations', 'query_sources'}
    tool = next(t for t in first['tools'] if t['name'] == 'act')
    request = next(v for v in tool['parameters']['properties']['request']['anyOf'] if v['properties']['intent']['enum']==['read'])
    expense = request['properties']['sources']['items']['anyOf'][0]
    assert not {'group_by', 'order_by', 'limit', 'filter', 'continue_analysis'} & expense['properties']['parameters']['properties'].keys()
    assert not h.state.actions and not h.sheets.write_log and not h.provider.posts


def test_changed_sql_reuses_source_after_an_argument_error_without_provider_reread(monkeypatch):
    h = harness(); reads = []; original = h.sheets.saved_entries
    def load(*args):
        reads.append(args)
        return original(*args)
    monkeypatch.setattr(h.sheets, 'saved_entries', load)
    result = h.send('เดือนตุลาคม รวมเท่าไร', query('SELECT SUM(absent) AS total FROM e'),
                    query('SELECT SUM(amount) AS total FROM e'), finish)
    assert result.status == 'answer', result
    assert len(reads) == 1 and len(h.model.requests) == 3
    feedback = json.loads(h.model.requests[1]['input'][-1]['output'])['repair_feedback']
    assert feedback['allowed_tools'] == ['act']
    assert not h.sheets.write_log


@pytest.mark.parametrize('damage', ['duplicate_alias', 'foreign_ref', 'owner_source', 'table_unconfigured'])
def test_invalid_source_plans_are_validated_before_any_read(damage, monkeypatch):
    h = harness()
    def deny(*args):
        pytest.fail('Invalid query must not read a provider')
    monkeypatch.setattr(h.sheets, 'saved_entries', deny)
    def mutate(args):
        if damage == 'duplicate_alias': args['sources'] *= 2
        elif damage == 'foreign_ref': args['sources'] = [{'source': 'result', 'alias': 'r', 'query_ref': 'read_foreign'}]
        elif damage == 'owner_source': args['source_refs'] = ['assistant_source']
        else: args['sources'] = [{'source': 'table', 'alias': 't', 'parameters': {'table_id': 'unconfigured'}}]
    result = h.send('เดือนตุลาคม ดูยอด', query('SELECT * FROM e', mutate=mutate), response(text='ขอเงื่อนไขเพิ่มเติมครับ'))
    assert result.status == 'clarification' and not result.data['tool_result']['ok'], result
    assert not h.state.actions and not h.sheets.write_log


def test_read_only_sql_cannot_write_and_does_not_infer_success():
    h = harness(); before = deepcopy(h.sheets.entries)
    result = h.send('เดือนตุลาคม ดูข้อมูล', query('DELETE FROM e'), response(text='ยังสรุปไม่ได้ครับ'))
    assert result.status == 'clarification' and not result.data['tool_result']['ok'], result
    assert h.sheets.entries == before and not h.sheets.write_log and not h.state.actions


def test_failed_read_is_incomplete_and_never_a_zero_total(monkeypatch):
    from line_assistant.transport import NotReady
    h = harness()
    def fail(*args): raise NotReady('synthetic private provider diagnostic')
    monkeypatch.setattr(h.sheets, 'saved_entries', fail)
    result = h.send('เดือนตุลาคม รวมเท่าไร', query('SELECT SUM(amount) AS total FROM e'))
    output = result.data['tool_result']
    assert not output['ok'] and not output['complete'] and output['error_code'] == 'query_incomplete_source'
    assert 'expense' not in output and 'rows' not in output


def test_repeat_is_traced_before_guard_and_ends_without_second_execution():
    h = harness(); rows = []; h.app.action_trace_sink = rows.append
    plan = query('SELECT SUM(amount) AS total FROM e')
    result = h.send('เดือนตุลาคม รวมเท่าไร', plan, plan)
    assert result.data['query_loop_stopped'] == 'repeated_read'
    proposed = [r for r in rows if r['phase'] == 'tool_proposed']
    assert len(proposed) == 2
    assert proposed[0]['arguments']['arguments_ref'] == proposed[1]['arguments']['arguments_ref']
    assert sum(r['phase'] == 'tool_request' and r['tool'] == 'act' for r in rows) == 1
    assert any(r['phase'] == 'planning_stopped' and r['reason'] == 'repeated_read' for r in rows)
    assert rows[-1]['stop_reason'] == 'repeated_read'
    processes = {r['process'] for r in rows if r['phase'] == 'process_finished'}
    assert {'owner_request', 'context_build', 'query_source_read', 'query_materialize', 'query_sql', 'receipt_store'} <= processes
    encoded = json.dumps(rows, ensure_ascii=False)
    for private in ['เดือนตุลาคม', 'BTS', 'Coffee', 'SELECT SUM', 'mock-owner', 'canonical_item']:
        assert private not in encoded


def test_observation_failure_does_not_change_query_result():
    h = harness()
    def broken(row): raise OSError('synthetic sink unavailable')
    h.app.action_trace_sink = broken
    result = h.send('เดือนตุลาคม รวมเท่าไร', query('SELECT COUNT(*) AS payments FROM e'), finish)
    assert result.status == 'answer' and action_trace.ACTIVE.get() is None


def test_sql_feature_summary_never_logs_comments_literals_or_identifiers():
    result = action_trace.sql_shape("SELECT 'secret SUM WHERE' AS private_name FROM private_table /* JOIN secret */")
    assert result['features'] == ['select']
    assert not any(v in json.dumps(result) for v in ['secret', 'private_name', 'private_table'])


def test_expense_calendar_join_in_one_call_keeps_payment_occurrences_once():
    from test_read_queries import event
    h = harness()
    h.provider.events = {key: event(key, '2026-10-02', '2026-10-03', all_day=True) for key in ['a', 'b']}
    calendar = {'source': 'calendar', 'alias': 'c', 'parameters': {
        **source()['parameters'], 'filter_text': None, 'purpose': 'expense_dates', 'count_unit': 'events', 'upcoming': False}}
    result = h.send('เดือนตุลาคม วันที่มีนัดใช้เงินเท่าไร', query(
        'SELECT SUM(e.amount) AS total,COUNT(*) AS payments FROM e WHERE EXISTS '
        '(SELECT 1 FROM c WHERE e.date BETWEEN c.start_date AND c.last_date)', sources=[source(), calendar]), finish)
    assert result.status == 'answer', result
    relation = next(v for v in h.app._query_reads.values() if v.get('relational_query'))
    assert relation['rows'] == [{'total': '99.20', 'payments': 2}]
    assert len(h.model.requests) == 2 and not h.provider.posts and not h.sheets.write_log


def test_exploratory_read_then_sql_reuses_same_snapshot_without_an_extra_fetch(monkeypatch):
    h = harness(); reads = []; original = h.sheets.saved_entries
    def load(*args): reads.append(args); return original(*args)
    monkeypatch.setattr(h.sheets, 'saved_entries', load)
    result = h.send('เดือนตุลาคม มีกี่รายการ', query(None), query('SELECT COUNT(*) AS payments FROM e'), finish)
    assert result.status == 'answer' and len(reads) == 1, result
    assert len(h.app._query_reads) == 2  # One source, one derived result.


def test_current_result_can_be_used_with_a_new_alias_without_source_references_in_sql():
    h = harness()
    def second(body):
        ref = json.loads(body['input'][-1]['output'])['query_ref']
        return query('SELECT SUM(total) AS grand_total FROM r', sources=[{'source': 'result', 'alias': 'r', 'query_ref': ref}])(body)
    result = h.send('เดือนตุลาคม รวมยอดตามร้านแล้วรวมทั้งหมด',
                    query('SELECT item,SUM(amount) AS total FROM e GROUP BY item'), second, finish)
    assert result.status == 'answer', result
    derived = [v for v in h.app._query_reads.values() if v.get('relational_query')]
    assert derived[-1]['rows'] == [{'grand_total': '99.30'}]


def test_result_references_are_not_available_in_the_next_turn():
    h = harness()
    h.send('เดือนตุลาคม รวมเท่าไร', query('SELECT SUM(amount) AS total FROM e'), finish)
    ref = next(k for k, v in h.app._query_reads.items() if v.get('relational_query'))
    result = h.send('ดูผลเดิม', query('SELECT * FROM r', sources=[{'source': 'result', 'alias': 'r', 'query_ref': ref}]),
                    response(text='ต้องอ่านข้อมูลใหม่ครับ'))
    assert result.status == 'clarification' and result.data['tool_result']['error_code'] == 'relation_reference_unavailable'


def test_generic_tables_with_unrelated_schemas_use_the_same_query_action():
    from test_table_workflow import harness as table_harness
    from test_tables import seed
    h = table_harness(); reg = h.app.table_flow.registry
    seed(reg, 'people', [{'name': 'A', 'phone': None, 'status': 'called'}, {'name': 'B', 'phone': None, 'status': 'pending'}])
    seed(reg, 'family_income', [{'date': '2026-10-01', 'amount': '500', 'customer': 'row0', 'note': None},
                               {'date': '2026-10-02', 'amount': '900', 'customer': 'row1', 'note': None}])
    result = h.send('รวมรายรับของคนที่โทรแล้ว', query(
        "SELECT p.name,SUM(i.amount) AS income FROM p JOIN i ON i.customer=p.id WHERE p.status='called' GROUP BY p.id,p.name",
        sources=[{'source': 'table', 'alias': 'p', 'parameters': {'table_id': 'people'}},
                 {'source': 'table', 'alias': 'i', 'parameters': {'table_id': 'family_income'}}]), finish)
    assert result.status == 'answer', result
    relation = next(v for v in h.app._query_reads.values() if v.get('relational_query'))
    assert relation['rows'] == [{'name': 'A', 'income': '500'}]
    assert all(getattr(store, 'writes', 0) == 0 for store in reg.stores.values())


def test_uncertain_calendar_source_preserves_owner_selection_and_blocks_sql_total():
    from test_calendar_semantic_queries import SemanticHosted
    from test_calendar_text_confirmation_v13 import turn
    from test_read_queries import event
    api = SemanticHosted()
    api.calendar.events = {'a': event('a', '2026-10-02', '2026-10-03', all_day=True, summary='กิจกรรมจำลอง')}
    api.labels = {'e1': 'uncertain'}
    calendar = {'source': 'calendar', 'alias': 'c', 'parameters': {
        **source()['parameters'], 'filter_text': 'TA', 'purpose': 'expense_dates', 'count_unit': 'events', 'upcoming': False}}
    result, _ = turn(api, 'เดือนตุลาคม วันที่ไป TA ใช้เงินเท่าไร', query(
        'SELECT SUM(e.amount) AS total FROM e WHERE EXISTS '
        '(SELECT 1 FROM c WHERE e.date BETWEEN c.start_date AND c.last_date)', sources=[source(), calendar]))
    assert result.status == 'clarification' and not result.data['tool_result']['complete'], result
    assert result.data['read_followup']['goal'] == 'expense'
    assert len(result.data['read_followup']['candidates']) == 1
    assert 'rows' not in result.data['tool_result'] and not api.google.sheet_writes and not api.calendar.posts


def test_semantically_resolved_calendar_is_computed_in_the_same_query_call():
    from test_calendar_semantic_queries import SemanticHosted
    from test_calendar_text_confirmation_v13 import turn
    from test_read_queries import event
    from test_expense_query_operations import seed
    api=SemanticHosted()
    api.calendar.events={'a':event('a','2026-10-02','2026-10-03',all_day=True,summary='กิจกรรมจำลอง')}
    api.labels={'e1':'match'}
    seed(api,[('2026-10-02','Rail',12),('2026-10-03','Rail',99)])
    calendar={'source':'calendar','alias':'c','parameters':{
        **source()['parameters'],'filter_text':'TA','purpose':'expense_dates','count_unit':'events','upcoming':False}}
    result,_=turn(api,'เดือนตุลาคม วันที่ไป TA ใช้เงินเท่าไร',query(
        'SELECT SUM(e.amount) AS total FROM e WHERE EXISTS '
        '(SELECT 1 FROM c WHERE e.date BETWEEN c.start_date AND c.last_date)',sources=[source(),calendar]),finish)
    assert result.status=='answer' and result.data['tool_result']['complete'],result
    assert '12 บาท' in result.message and '99 บาท' not in result.message
    assert len(api.selection_bodies)==1 and not api.google.sheet_writes and not api.calendar.posts
