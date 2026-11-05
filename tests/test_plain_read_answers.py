"""Natural read experiment: executed evidence/effects stay bound; prose is not verified."""
from copy import deepcopy
from decimal import Decimal
import json
import socket

import pytest
from fastapi.testclient import TestClient

from line_assistant import answer_composition as ac, plain_read_answers as plain, response_realization as voice
from line_assistant.history_cloud_run import HistoryConfig
from line_assistant.runtime_config import ConfigError
from line_assistant.state import Result
from test_calendar_factory import values
from test_calendar_mvp import response
from test_calendar_read_all import AllCalendarsHosted
from test_calendar_text_confirmation_v13 import turn
from test_expense_query_operations import seed
from test_intent_actions import harness, read, respond


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs): raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def answer(text, check=None):
    def choose(body):
        context = json.loads(body['input'][0]['content']); offered = context['read_responses'][-1]
        assert 'read_result' in offered and 'facts' not in offered
        if check: check(offered, body)
        value = response('act', {'request': {'intent': 'respond', 'response_ref': offered['response_ref'], 'answer': text}})
        value['output'][0]['call_id'] = 'respond_' + str(len(body['input']))
        return value
    return choose


def enabled():
    h = harness(); h.app.plain_read_answers_enabled = True
    return h


def no_slots(monkeypatch):
    def fail(*args, **kwargs): raise AssertionError('Read answers must bypass the slot renderer')
    monkeypatch.setattr(ac, 'for_result', fail)
    monkeypatch.setattr(ac, 'render', fail)


def test_native_answer_uses_actual_rows_without_slot_renderer_or_extra_call(monkeypatch):
    no_slots(monkeypatch); h = enabled(); traces = []; h.app.action_trace_sink = traces.append
    def check(offered, body):
        result = offered['read_result']['results'][0]
        assert Decimal(result['rows'][0]['total']) == Decimal('0.30')
        assert result['columns'][0]['unit'] == 'บาท'
        tool = next(t for t in body['tools'] if t['name'] == 'act')
        spec = next(v for v in tool['parameters']['properties']['request']['anyOf'] if v['properties']['intent']['enum'] == ['respond'])
        assert set(spec['properties']) == {'intent', 'response_ref', 'answer'}
    r = h.send('เดือนตุลาคม BTS รวมเท่าไร', read("SELECT SUM(amount) AS total FROM e WHERE item='BTS'"),
               answer('เดือนตุลาคม 2026 ค่า BTS รวม 0.30 บาทครับ', check))
    assert r.status == 'answer' and r.message == 'เดือนตุลาคม 2026 ค่า BTS รวม 0.30 บาทครับ'
    assert r.data['response_realization']['format'] == 'plain_read' and len(h.model.requests) == 2
    assert any(row['phase'] == 'intent_response' and row['status'] == 'composed' for row in traces)
    assert not h.sheets.write_log and not h.provider.posts and not h.state.actions


def test_wrong_model_numbers_are_not_claimed_to_be_code_verified():
    h = enabled(); r = h.send('รวมเท่าไร', read(), answer('รวม 999 บาทครับ'))
    assert r.message == 'รวม 999 บาทครับ'  # Deliberate counterexample: an eval must catch this.
    assert r.data['response_realization']['format'] == 'plain_read'
    assert Decimal(next(v for v in h.app._query_reads.values() if v.get('relational_query'))['rows'][0]['total']) == Decimal('99.30')
    assert not h.sheets.write_log and not h.state.actions


@pytest.mark.parametrize('damage', ['foreign', 'changed', 'new_turn'])
def test_current_result_binding_still_rejects_foreign_changed_or_prior_turn_refs(damage):
    h = enabled()
    first = h.send('รวมเท่าไร', read(), answer('รวม 99.30 บาทครับ')) if damage == 'new_turn' else None
    def bad(body):
        if damage == 'new_turn':
            return response('act', {'request': {'intent': 'respond', 'response_ref': first.data['response_realization']['response_ref'], 'answer': '999 บาท'}})
        value = answer('999 บาท')(body); args = json.loads(value['output'][0]['arguments'])
        if damage == 'foreign': args['request']['response_ref'] = 'answer_other'
        else: next(v for v in h.app._query_reads.values() if v.get('relational_query'))['rows'][0]['total'] = '777'
        value['output'][0]['arguments'] = json.dumps(args); return value
    r = h.send('แล้วล่ะ', *([] if first else [read()]), bad, response(text='ต้องอ่านใหม่ครับ'))
    assert r.status == 'clarification' and r.data['tool_result']['error_code'] == 'response_reference_unavailable'
    assert '999' not in r.message and not h.sheets.write_log


def test_followup_and_receipt_replay_keep_fresh_reads_and_no_extra_effects():
    h = enabled(); first = h.send('รายการไหนบ่อยสุด', read('SELECT item,COUNT(*) AS n FROM e GROUP BY item ORDER BY n DESC LIMIT 1'), answer('BTS บ่อยสุด 2 ครั้งครับ'))
    assert first.data['read_query']['answer_context']['records'] == [{'item': 'BTS', 'n': 2}]
    def fresh(body):
        previous = json.loads(body['input'][0]['content'])['previous_query']
        assert previous['answer_context']['requires_fresh_read'] and previous['answer_context']['records'][0]['item'] == 'BTS'
        return read("SELECT SUM(amount) AS total FROM e WHERE item='BTS'")(body)
    h.model.values = [fresh, answer('ค่า BTS รวม 0.30 บาทครับ')]; event = h.event('รวมเป็นเงินเท่าไหร่')
    r = h.app.handle(event); calls = len(h.model.requests)
    assert h.app.handle(deepcopy(event)) == r and len(h.model.requests) == calls == 4
    assert '0.30' in r.message and not h.sheets.write_log and not h.provider.posts


def test_turning_off_experiment_restores_legacy_read_documents():
    h = enabled(); h.send('รวม', read(), answer('รวม 99.30 บาทครับ'))
    h.app.plain_read_answers_enabled = False
    r = h.send('รวมอีกครั้ง', read(), respond())
    assert r.data['response_realization']['mode'] == 'planner_intent' and 'format' not in r.data['response_realization']
    assert '99.30' in r.message or '99.3' in r.message


class PlainHosted(AllCalendarsHosted):
    def __init__(self):
        super().__init__(); self.extra_config = {'PILOT_PLAIN_READ_ANSWERS_ENABLED': 'true'}
        self.voice = lambda body: envelope('รวมรายจ่าย 50 บาทครับ')


def envelope(text):
    return {'model': 'gpt-6-luna', 'status': 'completed', 'usage': {'input_tokens': 1000, 'output_tokens': 100},
            'output': [{'type': 'message', 'status': 'completed', 'content': [{'type': 'output_text', 'text': json.dumps({'answer': text}, ensure_ascii=False)}]}]}


def test_auxiliary_read_is_reserved_delivered_and_replayed_without_slot_renderer(monkeypatch):
    from test_read_queries import plan
    no_slots(monkeypatch); api = PlainHosted(); seed(api, [('2026-10-15', 'ชา', 20), ('2026-10-15', 'ขนม', 30)])
    r, event = turn(api, 'เมื่อวานรวมเท่าไร', plan(start='2026-10-15'))
    assert r.message == 'รวมรายจ่าย 50 บาทครับ' and r.data['response_realization']['format'] == 'plain_read'
    assert len(api.voice_bodies) == 1 and len(api.model_bodies) == 2
    body = api.voice_bodies[0]; assert voice.validate_body(body)
    assert body['text']['format']['schema'] == plain.schema()
    payload = json.loads(body['input'][0]['content']); assert payload['read_result']['summary']['total'] == '50 บาท'
    assert not api.google.sheet_writes and not api.calendar.posts and not api.snapshot()['actions']
    with TestClient(api.app()) as client: assert api.send(client, deepcopy(event)).status_code == 200
    assert len(api.model_bodies) == 2
    from line_assistant.raw_history import reservation
    assert api.app().state.assistant.guard.reservation_calculator(body) == reservation(body)
    api.extra_config['PILOT_PLAIN_READ_ANSWERS_ENABLED'] = 'false'
    with TestClient(api.app()) as client: assert api.send(client, deepcopy(event)).status_code == 200
    assert len(api.model_bodies) == 2  # Already delivered plain text survives switching off.


@pytest.mark.parametrize('failure', ['json', 'empty', 'length', 'tools', 'refusal', 'incomplete', 'usage'])
def test_bad_auxiliary_transport_falls_back_once_without_repeat_read_or_repair(failure):
    from test_read_queries import plan
    api = PlainHosted(); seed(api, [('2026-10-15', 'ชา', 50)])
    def bad(body):
        value = envelope('รวม 50 บาทครับ')
        if failure == 'json': value['output'][0]['content'][0]['text'] = '{bad'
        elif failure in {'empty', 'length'}: value = envelope('' if failure == 'empty' else 'a' * 4501)
        elif failure == 'tools': value['output'].append({'type': 'function_call', 'name': 'write', 'arguments': '{}'})
        elif failure == 'refusal': value['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'test'}]
        elif failure == 'incomplete': value['status'] = 'incomplete'
        else: value['usage']['input_tokens'] = 999999
        return value
    api.voice = bad; r, _ = turn(api, 'เมื่อวานรวม', plan(start='2026-10-15'))
    assert r.data['response_realization']['status'] == 'fallback' and '50 บาท' in r.message
    assert len(api.voice_bodies) == 1 and len(api.model_bodies) == 2
    assert not api.google.sheet_writes and not api.calendar.posts


@pytest.mark.parametrize('flag', ['TRUE', '', '1'])
def test_flag_requires_explicit_valid_configuration(flag):
    with pytest.raises(ConfigError): HistoryConfig.from_mapping({**values(True), 'PILOT_PLAIN_READ_ANSWERS_ENABLED': flag})


def test_flag_requires_realization_and_is_off_by_default():
    assert not HistoryConfig.from_mapping(values(True)).plain_read_answers_enabled
    with pytest.raises(ConfigError): HistoryConfig.from_mapping({**values(True), 'PILOT_PLAIN_READ_ANSWERS_ENABLED': 'true'})


def test_incomplete_and_write_results_are_not_natural_read_context():
    h = enabled()
    for status, data in [('preview', {'read_only': True}), ('answer', {'read_only': False}),
                         ('answer', {'read_only': True, 'financial_write_authorized': True}),
                         ('answer', {'read_only': True, 'tool_result': {'complete': False}})]:
        assert plain.for_result(h.app, Result(status, 'x', data)) is None


def test_operation_review_keeps_linked_renderer_with_experiment_enabled():
    from test_calendar_batch_factory_v13 import text, proposed
    api = PlainHosted()
    from test_response_realization import generated
    api.voice = generated
    r, _ = turn(api, text(), proposed)
    assert r.status == 'preview' and r.data['response_realization']['status'] == 'llm'
    assert json.loads(api.voice_bodies[0]['input'][0]['content'])['operation_document']
    assert not api.calendar.posts
    saved, _ = turn(api, 'ยืนยันชุดนัดนี้')
    assert saved.status == 'created' and api.calendar.posts == 2
    assert json.loads(api.voice_bodies[-1]['input'][0]['content'])['operation_document']


@pytest.mark.parametrize('budget,expected_rows', [(12000, 39), (1, 20)])
def test_complete_computed_rows_are_preferred_within_bound_without_exposing_provider_data(monkeypatch, budget, expected_rows):
    monkeypatch.setattr(plain, 'MAX_COMPLETE_ROWS_BYTES', budget)
    h = enabled()
    h.app._current = {'at': h.now.isoformat()}
    rows = [{'item': 'Synthetic ' + str(i), 'amount': str(i)} for i in range(39)]
    output = {'domain': 'relation', 'relational_query': True, 'complete': True,
              'scope': {'domain': 'relation', 'sql': 'SELECT item,amount FROM e', 'spreadsheet_id': 'PRIVATE_BINDING'},
              'rows': rows[:20], 'analysis_records': rows, 'count': 39, 'shown': 20,
              'columns': [{'name': 'item'}, {'name': 'amount', 'unit': 'บาท'}]}
    core = Result('answer', 'fallback', {'read_only': True, 'tool_result': output})
    value = plain.for_result(h.app, core)
    assert len(value['rows']) == value['shown'] == expected_rows and value['count'] == 39
    assert value['rows_complete'] is (expected_rows == 39)
    assert 'PRIVATE_BINDING' not in json.dumps(value) and 'analysis_records' not in value
    assert output['shown'] == 20 and len(output['rows']) == 20


def test_plain_format_cannot_be_awarded_placeholder_grounding(monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    from semantic_harness import grounded_document
    result = Result('answer', '999 บาท', {'response_realization': {'format': 'plain_read', 'mode': 'planner_intent'}})
    assert grounded_document(None, result, 0) is None
