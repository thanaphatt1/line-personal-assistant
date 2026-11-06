"""Repair a received final-selection failure using existing results only."""
from copy import deepcopy
from datetime import datetime
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from line_assistant.history_first import parse_response
from line_assistant.planner_failures import PlannerResponseError, planner_failure
from test_calendar_mvp import response
from test_intent_actions import harness, read, respond
from test_read_queries import event


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs): raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def broken(kind='missing_answer'):
    def choose(body):
        value = response('act', {'request': {'intent': 'respond'}})
        if kind == 'missing_answer': value['output'] = [{'type': 'reasoning', 'summary': []}]
        elif kind == 'invalid_arguments_json': value['output'][0]['arguments'] = '{PRIVATE_ARGUMENT_SENTINEL'
        elif kind == 'unknown_tool': value['output'][0]['name'] = 'PRIVATE_TOOL_SENTINEL'
        elif kind == 'one_serial_tool': value['output'] *= 2
        elif kind == 'refusal': value['output'] = [{'type': 'message', 'status': 'completed', 'content': [{'type': 'refusal', 'refusal': 'PRIVATE_REFUSAL_SENTINEL'}]}]
        elif kind == 'incomplete': value['status'] = 'incomplete'
        elif kind == 'usage': value['usage']['input_tokens'] = 10000000
        elif kind == 'timeout': raise httpx.ReadTimeout('PRIVATE_NETWORK_SENTINEL')
        return value
    return choose


def check_completion_tools(body):
    context = json.loads(body['input'][0]['content'])
    assert context['repair_feedback']['kind'] == 'planner_response'
    assert context['repair_feedback']['source_reads_allowed'] is False
    assert {t['name'] for t in body['tools']} == {'act', 'finish_query_analysis'}
    act = next(t for t in body['tools'] if t['name'] == 'act')
    assert [v['properties']['intent']['enum'] for v in act['parameters']['properties']['request']['anyOf']] == [['respond']]
    finish = next(t for t in body['tools'] if t['name'] == 'finish_query_analysis')
    assert 'source_refs' not in finish['parameters']['properties']
    assert finish['parameters']['properties']['query_refs']['items']['enum']
    return context


@pytest.mark.parametrize('kind', ['missing_answer', 'invalid_arguments_json', 'unknown_tool', 'one_serial_tool'])
def test_one_received_failure_is_repaired_without_rereading_or_writes(kind, monkeypatch):
    h = harness(); loads = []; original = h.sheets.saved_entries; traces = []; h.app.action_trace_sink = traces.append
    monkeypatch.setattr(h.sheets, 'saved_entries', lambda *args: (loads.append(args) or original(*args)))
    def final(body):
        check_completion_tools(body)
        return respond()(body)
    r = h.send('เดือนตุลาคม BTS รวมเท่าไร', read("SELECT SUM(amount) AS total FROM e WHERE item='BTS'"), broken(kind), final)
    assert r.status == 'answer' and '0.30 บาท' in r.message
    assert r.data['query_completion_recovery'] == {'status': 'completed', 'validation_code': kind}
    assert len(h.model.requests) == 3 and len(loads) == 1
    assert any(v['phase'] == 'model_result' and v.get('validation_code') == kind for v in traces)
    logged = json.dumps(traces)
    assert 'PRIVATE_ARGUMENT_SENTINEL' not in logged and 'PRIVATE_TOOL_SENTINEL' not in logged
    assert not h.state.actions and not h.sheets.write_log and not h.provider.posts


def calendar_source():
    return {'source': 'calendar', 'alias': 'c', 'parameters': {'date_mode': 'range',
            'start_date': '2026-11-06', 'end_date': '2026-12-31', 'period_text': None,
            'relative_offset': None, 'filter_text': None, 'purpose': 'appointments',
            'count_unit': 'events', 'upcoming': False}}


def ta_harness():
    h = harness(); h.now = datetime.fromisoformat('2026-11-06T13:54:00+07:00')
    days = [7, 7, 10, 12, 14, 25, 25]
    h.provider.events = {str(i): event(str(i), f'2026-11-{day:02}T09:00:00+07:00',
                                     f'2026-11-{day:02}T12:00:00+07:00', summary='TA Synthetic ' + str(i))
                         for i, day in enumerate(days)}
    h.provider.events['dec'] = event('dec', '2026-12-02T13:00:00+07:00', '2026-12-02T16:00:00+07:00', summary='TA Synthetic Dec')
    return h


def test_dates_then_dates_and_times_keep_all_eight_occurrences_and_fresh_read():
    h = ta_harness(); before = deepcopy(h.provider.events)
    first = h.send('เช็ก TA มีวันไหน', read('SELECT DISTINCT start_date FROM c ORDER BY start_date', sources=[calendar_source()]), respond())
    assert first.status == 'answer'
    first_reads = len(h.provider.calls)
    def final(body):
        check_completion_tools(body)
        return respond()(body)
    r = h.send('ขอวันที่และเวลาด้วย', read('SELECT title,start,"end" FROM c ORDER BY start', sources=[calendar_source()]), broken(), final)
    assert r.status == 'answer' and r.data['query_completion_recovery']['status'] == 'completed'
    raw = next(v for v in h.app._query_reads.values() if v.get('relational_query'))
    assert raw['count'] == len(raw['rows']) == 8 and len(raw['columns']) == 3
    assert '09:00' in r.message and '12:00' in r.message and '13:00' in r.message and '16:00' in r.message
    assert len(h.provider.calls) == first_reads + 1
    assert h.provider.events == before and not h.provider.posts and not h.sheets.write_log and not h.state.actions


def test_recovered_receipt_replays_without_any_model_or_provider_calls():
    h = harness(); h.model.values = [read(), broken(), respond()]; e = h.event('รวมเดือนตุลาคม')
    r = h.app.handle(e); calls = len(h.model.requests)
    assert r.status == 'answer' and h.app.handle(deepcopy(e)) == r and len(h.model.requests) == calls == 3


@pytest.mark.parametrize('kind', ['timeout', 'usage', 'incomplete', 'refusal'])
def test_unknown_or_incomplete_outcomes_and_refusals_do_not_start_repair(kind):
    h = harness(); r = h.send('รวมเดือนตุลาคม', read(), broken(kind))
    assert r.status == 'model_unavailable' and len(h.model.requests) == 2
    assert not r.data.get('query_completion_recovery') and not h.sheets.write_log and not h.provider.posts


def test_failure_before_any_query_does_not_start_repair():
    h = harness(); r = h.send('รวมเดือนตุลาคม', broken())
    assert r.status == 'model_unavailable' and len(h.model.requests) == 1
    assert r.data['model_failure']['validation_code'] == 'missing_answer'
    assert r.data['response_realization']['reason'] == 'planner_invalid_response'


def test_second_malformed_response_stops_without_loop_or_more_source_reads():
    h = harness(); r = h.send('รวมเดือนตุลาคม', read(), broken(), broken())
    assert r.status == 'model_unavailable' and len(h.model.requests) == 3 and not h.sheets.write_log


def test_repair_cannot_smuggle_a_read_even_with_an_offered_act_name(monkeypatch):
    h = harness(); loads = []; original = h.sheets.saved_entries
    monkeypatch.setattr(h.sheets, 'saved_entries', lambda *args: (loads.append(args) or original(*args)))
    r = h.send('รวมเดือนตุลาคม', read(), broken(), read())
    assert r.status == 'clarification' and r.data['query_completion_recovery']['status'] == 'rejected'
    assert len(loads) == 1 and len(h.model.requests) == 3 and not h.state.actions and not h.sheets.write_log


def test_repair_can_decline_to_treat_a_supporting_result_as_the_final_answer():
    h = harness()
    def clarify(body):
        c = check_completion_tools(body)
        return response('finish_query_analysis', {'query_refs': [], 'operation': 'clarify',
            'question': 'ยังต้องคำนวณข้อมูลส่วนอื่นก่อนจึงจะตอบเงื่อนไขนี้ได้ครับ'})
    r = h.send('ขอเปรียบเทียบกับรายได้', read(), broken(), clarify)
    assert r.status == 'clarification' and not r.data['tool_result']['complete']
    assert 'ต้องคำนวณ' in r.message and len(h.model.requests) == 3


def test_incomplete_current_evidence_is_not_recovered_from_another_complete_read():
    h = harness()
    def fail(body):
        next(iter(h.app._query_reads.values()))['complete'] = False
        return broken()(body)
    r = h.send('รวมเดือนตุลาคม', read(), fail)
    assert r.status == 'model_unavailable' and len(h.model.requests) == 2


def test_source_exploration_does_not_become_a_final_answer_after_failure():
    h = harness(); r = h.send('รวมเดือนตุลาคม', read(sql=None), broken())
    assert r.status == 'model_unavailable' and len(h.model.requests) == 2


def test_parser_metadata_never_contains_response_text_arguments_or_reasoning():
    value = response('secret_tool_value', {'owner': 'PRIVATE_ARGUMENT_SENTINEL'})
    value['output'].insert(0, {'type': 'reasoning', 'summary': ['PRIVATE_REASONING_SENTINEL']})
    value['output'].append({'type': 'message', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'PRIVATE_TEXT_SENTINEL'}]})
    with pytest.raises(PlannerResponseError) as caught: parse_response(value, allowed_tools={'act'})
    detail = planner_failure(caught.value); wire = json.dumps(detail)
    assert detail['validation_code'] == 'unknown_tool' and 'PRIVATE_' not in wire and 'secret_tool_value' not in wire
    assert detail['response_summary']['output_types'] == {'reasoning': 1, 'message': 1, 'function_call': 1, 'other': 0}


def test_normal_success_adds_no_extra_call():
    h = harness(); r = h.send('รวมเดือนตุลาคม', read(), respond())
    assert r.status == 'answer' and len(h.model.requests) == 2 and 'query_completion_recovery' not in r.data


@pytest.mark.parametrize('plain', [False, True])
def test_hosted_calendar_repair_obeys_http_reservations_and_cold_delivery_replay(plain):
    from test_calendar_semantic_queries import SemanticHosted
    from test_calendar_text_confirmation_v13 import turn
    from test_plain_read_answers import answer
    api = SemanticHosted(); api.now = datetime.fromisoformat('2026-11-06T13:54:00+07:00')
    api.calendar.events = deepcopy(ta_harness().provider.events)
    api.extra_config = {'PILOT_PLAIN_READ_ANSWERS_ENABLED': 'true' if plain else 'false'}
    src = calendar_source(); src['parameters']['filter_text'] = 'TA'
    def final(body):
        check_completion_tools(body)
        return (answer('TA มี 8 นัด ช่วงเช้า 09:00–12:00 และนัดเดือนธันวาคม 13:00–16:00 ครับ') if plain else respond())(body)
    r, e = turn(api, 'ขอวันที่และเวลาของ TA ตั้งแต่วันนี้ถึงสิ้นปี',
                read('SELECT title,start,"end" FROM c ORDER BY start', sources=[src]), broken(), final)
    assert r.status == 'answer' and r.data['query_completion_recovery']['status'] == 'completed'
    assert len(api.model_bodies) == 4 and len(api.selection_bodies) == 1
    assert len(api.calendar_requests) == 1 and not api.calendar.posts and not api.google.sheet_writes
    assert '09:00' in r.message and '16:00' in r.message
    calls = len(api.model_bodies); reads = len(api.calendar_requests)
    with TestClient(api.app()) as client: assert api.send(client, deepcopy(e)).status_code == 200
    assert len(api.model_bodies) == calls and len(api.calendar_requests) == reads
    assert not api.snapshot()['actions']


@pytest.mark.parametrize('stop', ['deadline', 'quota'])
def test_recovery_stays_within_existing_deadline_and_model_budget(monkeypatch, stop):
    from line_assistant import pilot_http
    h = harness(); finished = False
    def fail(body):
        nonlocal finished
        finished = True
        if stop == 'quota': h.app.guard.limit = 0
        return broken()(body)
    if stop == 'deadline': monkeypatch.setattr(pilot_http, 'remaining_model_seconds', lambda: 0 if finished else 30)
    r = h.send('รวมเดือนตุลาคม', read(), fail)
    assert len(h.model.requests) == 2 and r.status in {'clarification', 'model_budget_exhausted'}
    assert not h.sheets.write_log and not h.provider.posts and not h.state.actions


def test_foreign_reference_in_completion_repair_cannot_replace_actual_results():
    h = harness()
    r = h.send('รวมเดือนตุลาคม', read(), broken(), respond(mutate=lambda args: args.update(response_ref='answer_foreign')))
    assert r.status == 'clarification' and r.data['query_completion_recovery']['status'] == 'rejected'
    assert len(h.model.requests) == 3 and not h.sheets.write_log


def test_tooleless_repair_cannot_publish_an_unselected_free_text_answer():
    h = harness(); r = h.send('รวมเดือนตุลาคม', read(), broken(), response(text='รวม 999 บาทครับ'))
    assert r.status == 'clarification' and '999' not in r.message and len(h.model.requests) == 3


def test_cross_row_qualified_template_does_not_duplicate_recovered_records():
    h = harness()
    def bad_layout(args):
        args['document']['blocks'] = [
            {'kind': 'text', 'template': '{{f1}}'},
            {'kind': 'rows', 'record_refs': ['r1', 'r2'], 'heading': '',
             'template': '{{r1_c1}} {{r1_c2}}\n{{r2_c1}} {{r2_c2}}'}]
    r = h.send('เดือนตุลาคมแยกยอดตามรายการ',
               read('SELECT item,SUM(amount) AS total FROM e GROUP BY item ORDER BY item'),
               broken(), respond(mutate=bad_layout))
    assert r.status == 'answer' and r.data['query_completion_recovery']['status'] == 'completed'
    assert r.data['response_realization']['status'] == 'fallback'
    assert r.message.count('BTS') == r.message.count('Coffee') == 1
    assert len(h.model.requests) == 3 and not h.sheets.write_log


def test_row_template_may_use_its_own_qualified_cells_without_losing_coverage():
    h = harness()
    def scoped_layout(args):
        args['document']['blocks'] = [{'kind': 'text', 'template': '{{f1}}'},
            *[{'kind': 'rows', 'record_refs': [ref], 'heading': '',
               'template': '{{'+ref+'_c1}} {{'+ref+'_c2}}'} for ref in ['r1', 'r2']]]
    r = h.send('เดือนตุลาคมแยกยอดตามรายการ',
               read('SELECT item,SUM(amount) AS total FROM e GROUP BY item ORDER BY item'),
               broken(), respond(mutate=scoped_layout))
    assert r.status == 'answer' and r.data['response_realization']['status'] == 'llm'
    assert r.message.count('BTS') == r.message.count('Coffee') == 1


@pytest.mark.parametrize('foreign_query', [False, True])
def test_completion_binds_owner_intent_but_does_not_grant_foreign_query_access(foreign_query):
    h = harness()
    def final(body):
        c = check_completion_tools(body)
        ref = 'read_foreign' if foreign_query else c['read_responses'][-1]['query_ref']
        return response('finish_query_analysis', {'query_refs': [ref], 'operation': 'clarify',
            'question': 'ยังต้องวิเคราะห์ข้อมูลเพิ่มเติมจึงจะตอบเงื่อนไขนี้ได้ครับ',
            'source_refs': ['read_is_not_an_owner_message']})
    r = h.send('รวมและเปรียบเทียบข้อมูล', read(), broken(), final)
    assert r.status == 'clarification' and len(h.model.requests) == 3
    if foreign_query: assert r.data['tool_result']['error_code'] == 'invalid_query_refs'
    else:
        assert 'ต้องวิเคราะห์' in r.message
        call = next(row['call'] for row in h.app.trace if row['call']['name'] == 'finish_query_analysis')
        assert json.loads(call['arguments'])['source_refs'] == [h.app._current['source_ref']]
    assert not h.sheets.write_log and not h.provider.posts
