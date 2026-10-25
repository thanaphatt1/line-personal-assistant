"""Reproduce a short confirmation after an uncertain activity/spending query.

Dates, event titles and amounts are synthetic. Model choices are scripted: the
original failing tool arguments were not retained, so the malformed calls below
are a mechanism reproduction, not a replay of captured model decisions.

Passing controls establish history/grounding and repair contracts only; no
fresh inference is run.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_calendar_mvp import response
from test_calendar_semantic_queries import SemanticHosted
from test_calendar_text_confirmation_v13 import turn, mutate
from test_expense_query_operations import expense, seed
from test_query_analysis_loop import finish, read
from test_read_queries import event, plan
from line_assistant.reply_display import money


CASE = json.loads((Path(__file__).parent / 'fixtures/activity_expense_followup.json').read_text())


class FollowupContractError(AssertionError):
    """A completed follow-up does not satisfy the selected-day oracle."""


def assert_selected_day(result, case=CASE):
    """Independent numeric/date oracle, also usable for future model evaluations."""
    expected = case['expect']
    scope = result.data.get('read_query', {})
    output = result.data.get('tool_result', {})
    checks = [
        (result.status in {'answer', 'summary'}, 'Confirmation must continue the expense question'),
        (output.get('complete') is True, 'Selected-day read must be complete'),
        (output.get('domain') == 'expense', 'The original expense goal must survive'),
        (scope.get('start') == scope.get('end') == expected['selected_day'],
         'Resolve the day in the previous month; do not reuse the whole month'),
        (output.get('expense') == expected['expense'], 'Include only selected-day spending'),
        (output.get('count') == expected['count'], 'Count both selected-day payments'),
        (money(expected['expense']) + ' บาท' in result.message, 'Render the grounded subtotal'),
        ('ต้องการดูช่วงวันไหน' not in result.message, 'Do not ask again for the known period'),
    ]
    for valid, message in checks:
        if not valid:
            raise FollowupContractError(message)


def assert_read_only(api):
    assert not api.google.sheet_writes
    assert not api.calendar.posts
    assert not api.snapshot()['actions']


@pytest.fixture
def uncertain_report():
    api = SemanticHosted()
    api.now = datetime.fromisoformat(CASE['clock'])
    api.calendar.events = {
        row['id']: event(row['id'], row['start'], row['end'], summary=row['title'])
        for row in CASE['calendar_events']
    }
    api.labels = {'e1': 'uncertain', 'e2': 'uncertain'}
    seed(api, CASE['expense_rows'])
    expected = CASE['expect']
    first, original = turn(
        api, CASE['question'],
        read('calendar', expected['month_start'], date_mode='calendar_month', filter_text='คุมสอบ'),
        # Invalid total/sort combination reproduces the reported generic tail
        # while retaining valid month dates. It is not the captured original call.
        expense(expected['month_start'], expected['month_end'], view='total',
                order_by='amount', continue_analysis=True),
        finish(),
    )
    assert first.status == 'clarification'
    # Fact check: nothing confirmed yet, both candidates are shown as possible matches.
    assert first.data['read_followup']['confirmed'] == [] and 'นัดที่อาจเป็นคุมสอบ' in first.message
    assert all(row['title'] in first.message for row in CASE['calendar_events'])
    assert 'ข้อมูลที่ใช้ค้นหายังไม่ชัดเจน' not in first.message
    assert 'เป็นคุมสอบไหมครับ?' in first.message or 'นัดไหนเป็นคุมสอบครับ?' in first.message
    assert first.data['read_followup']['goal'] == 'expense'
    assert first.data['read_query']['start'] == expected['month_start']
    assert first.data['read_query']['end'] == expected['month_end']
    assert_read_only(api)
    return api, first, original


def assert_history(body, first, confirmation):
    context = json.loads(body['input'][0]['content'])
    assert context['message'] == confirmation
    assert context['history_window_hours'] == 24
    assert context['history_policy'] == 'raw_both_speakers_24h'
    assert [(row['role'], row['text']) for row in context['history']] == [
        ('user', CASE['question']), ('assistant', first.message)]
    assert context['previous_query']['start'] == CASE['expect']['month_start']
    assert context['previous_query']['end'] == CASE['expect']['month_end']
    assert {'query_calendar', 'query_expenses'} <= set(context['available_tools'])
    pending = context['pending_query']
    assert pending['original_question'] == CASE['question']
    assert pending['start'] == CASE['expect']['month_start']
    assert pending['end'] == CASE['expect']['month_end']
    assert pending['activity'] == 'คุมสอบ'
    assert len(pending['candidates']) == 2
    assert 'resolve_activity_dates' in context['available_tools']
    return context


def resolve(body):
    context = json.loads(body['input'][0]['content'])
    pending = context['pending_query']
    selected = [row['candidate_ref'] for row in pending['candidates']
                if row['start'].startswith(CASE['expect']['selected_day'])]
    return response('resolve_activity_dates', {
        'clarification_ref': pending['clarification_ref'], 'candidate_refs': selected,
        'decision': 'confirm',
        'selection_text': context['message'], 'source_refs': [context['current_source_ref']]})


@pytest.mark.parametrize('confirmation', CASE['confirmations'])
def test_cold_followup_has_original_question_delivered_candidates_and_verified_month(uncertain_report, confirmation):
    api, first, _ = uncertain_report

    def inspect(body):
        assert_history(body, first, confirmation)
        # turn() creates a new production factory. Only the speaker history and
        # previous scope cross this boundary; old function exchanges do not.
        assert not any(row.get('type') == 'function_call_output' for row in body['input'])
        return response(text='ขอตรวจรายจ่ายของวันที่ยืนยันครับ')

    turn(api, confirmation, inspect)
    assert_read_only(api)


def missing_date_call(body):
    # Deliberately missing ISO dates, despite a literal day in the owner text.
    # Tests server behavior for this failure mechanism, not language planning.
    return plan('expense', start=None, end=None)(body)


def test_missing_tool_dates_are_repaired_with_pending_selection_and_replay_is_idle(uncertain_report):
    api, first, _ = uncertain_report

    def choose(body):
        assert_history(body, first, 'มีวันที่ 14')
        return missing_date_call(body)

    def repair(body):
        feedback = json.loads(body['input'][-1]['output'])
        assert feedback['error_code'] == 'date_required'
        assert feedback['pending_query']['activity'] == 'คุมสอบ'
        assert not {'workbook', 'calendar', 'calendar_read_ids_hash'} & feedback['scope'].keys()
        assert {t['name'] for t in body['tools']} <= {
            'query_sources','query_expenses', 'query_calendar', 'inspect_data_sources', 'finish_query_analysis', 'resolve_activity_dates','resolve_temporal','query_relations'}
        return resolve(body)
    result, original = turn(api, 'มีวันที่ 14', choose, repair)
    assert_selected_day(result)
    before = (len(api.model_bodies), len(api.calendar_requests), len(api.extra_requests))
    with TestClient(api.app()) as client:
        assert api.send(client, deepcopy(original)).status_code == 200
    assert before == (len(api.model_bodies), len(api.calendar_requests), len(api.extra_requests))
    assert_read_only(api)


def test_remaining_candidate_can_be_confirmed_after_restart_and_dates_are_unioned(uncertain_report):
    api, _, _ = uncertain_report
    first, _ = turn(api, 'มีวันที่ 14', resolve)
    assert_selected_day(first)
    assert 'อีก 1 นัด' in first.message and 'Mock subject TA (Online)' in first.message

    def second(body):
        context = json.loads(body['input'][0]['content'])
        pending = context['pending_query']
        assert pending['confirmed_count'] == 1 and pending['unconfirmed_count'] == 1
        assert pending['original_question'] == CASE['question']
        assert len(pending['candidates']) == 1
        return response('resolve_activity_dates', {
            'clarification_ref': pending['clarification_ref'],
            'candidate_refs': [pending['candidates'][0]['candidate_ref']],
            'selection_text': 'วันที่ 29 ด้วย', 'source_refs': [context['current_source_ref']]})

    result, _ = turn(api, 'วันที่ 29 ด้วย', second)
    output = result.data['tool_result']
    assert output['expense'] == '760.00' and output['count'] == 3
    assert result.data['read_query']['calendar_basis']['dates'] == ['2026-11-14', '2026-11-29']
    assert result.data['read_query']['calendar_basis']['coverage'] == 'confirmed_dates_only'
    assert 'read_followup' not in result.data
    assert_read_only(api)


@pytest.mark.parametrize('fault', ['unknown_candidate', 'old_clarification', 'duplicate', 'invented_quote', 'old_source_only'])
def test_invalid_selections_never_read_calendar_or_expenses(uncertain_report, fault):
    api, _, _ = uncertain_report
    before = (len(api.calendar_requests), len(api.extra_requests))

    def invalid(body):
        value = resolve(body)
        call = value['output'][0]
        args = json.loads(call['arguments'])
        context = json.loads(body['input'][0]['content'])
        if fault == 'unknown_candidate': args['candidate_refs'] = ['candidate_invented']
        elif fault == 'old_clarification': args['clarification_ref'] = 'follow_expired'
        elif fault == 'duplicate': args['candidate_refs'] *= 2
        elif fault == 'invented_quote': args['selection_text'] = 'วันที่ 29'
        else: args['source_refs'] = [context['history'][0]['source_ref']]
        call['arguments'] = json.dumps(args)
        return value

    result, _ = turn(api, 'มีวันที่ 14', invalid)
    assert result.data['tool_result']['error_code'] == 'invalid_selection'
    assert before == (len(api.calendar_requests), len(api.extra_requests))
    assert 'expense' not in result.data['tool_result']
    assert_read_only(api)


@pytest.mark.parametrize('fault', ['deleted', 'title_changed', 'date_moved', 'end_changed'])
def test_changed_confirmed_event_requires_a_new_read_instead_of_a_stale_subtotal(uncertain_report, fault):
    api, _, _ = uncertain_report
    saved = api.calendar.events['mock-exam']
    if fault == 'deleted': del api.calendar.events['mock-exam']
    elif fault == 'title_changed': saved['summary'] = 'Different mock appointment'
    elif fault == 'date_moved': saved['start']['dateTime'] = '2026-11-15T07:30:00+07:00'
    else: saved['end']['dateTime'] = '2026-11-14T13:30:00+07:00'
    result, _ = turn(api, 'มีวันที่ 14', resolve)
    assert result.data['tool_result']['error_code'] in {'calendar_changed', 'calendar_unavailable'}
    assert 'expense' not in result.data['tool_result'] and '0.00 บาท' not in result.message
    assert_read_only(api)


def test_incomplete_calendar_reread_cannot_become_a_zero_or_partial_financial_total(uncertain_report):
    from test_calendar_read_all import PERSONAL
    api, _, _ = uncertain_report
    api.event_pages[PERSONAL][''] = 403
    result, _ = turn(api, 'มีวันที่ 14', resolve)
    assert result.data['tool_result']['error_code'] == 'calendar_unavailable'
    assert 'expense' not in result.data['tool_result'] and '0.00 บาท' not in result.message
    assert_read_only(api)


@pytest.mark.parametrize('fault', ['expired', 'undelivered', 'different_source'])
def test_expired_unverified_or_foreign_source_context_is_not_offered_for_resolution(uncertain_report, fault):
    api, _, original = uncertain_report
    if fault == 'expired': api.now += timedelta(hours=24, seconds=1)
    elif fault == 'undelivered':
        mutate(api, lambda state: setattr(state.replies[original['webhookEventId']], 'status', 'failed'))
    else: api.read_all = False

    def inspect(body):
        context = json.loads(body['input'][0]['content'])
        assert context['pending_query'] is None
        assert 'resolve_activity_dates' not in context['available_tools']
        return response(text='ขอรายละเอียดนัดที่ต้องการตรวจครับ')

    before = (len(api.calendar_requests), len(api.extra_requests))
    turn(api, 'มีวันที่ 14', inspect)
    assert before == (len(api.calendar_requests), len(api.extra_requests))
    assert_read_only(api)


def test_new_explicit_query_replaces_old_pending_activity_without_inheriting_it(uncertain_report):
    api, _, _ = uncertain_report
    result, _ = turn(api, '14 ธ.ค. ใช้เท่าไหร่', plan('expense', '2026-12-14'))
    assert result.data['tool_result']['expense'] == '9000.00'

    def inspect(body):
        context = json.loads(body['input'][0]['content'])
        assert context['pending_query'] is None
        assert context['previous_query']['start'] == '2026-12-14'
        return response(text='รับทราบครับ')

    turn(api, 'โอเค', inspect)
    assert_read_only(api)


def test_chat_preserves_pending_activity_and_failed_repair_asks_about_shown_candidates(uncertain_report):
    api, _, _ = uncertain_report
    turn(api, 'สวัสดี', response(text='สวัสดีครับ'))
    result, _ = turn(api, 'มีวันที่ 14', missing_date_call,
                     response(text='ต้องการดูช่วงวันไหนครับ?'))
    assert 'ต้องการดูช่วงวันไหน' not in result.message
    assert all(row['title'] in result.message for row in CASE['calendar_events'])
    assert result.data['read_followup']['question'] == CASE['question']
    assert_read_only(api)


def test_expense_read_failure_preserves_selection_context_without_publishing_a_zero(uncertain_report, monkeypatch):
    from line_assistant.sheets_api import SheetsApi
    from line_assistant.transport import ApiError
    api, _, _ = uncertain_report
    def unavailable(*args, **kwargs):
        raise ApiError(403)
    monkeypatch.setattr(SheetsApi, 'saved_entries', unavailable)
    result, _ = turn(api, 'มีวันที่ 14', resolve)
    assert result.data['tool_result']['error_code'] == 'expense_unavailable'
    assert result.data['tool_result']['expense_error'] == 'provider_failure'
    assert 'expense' not in result.data['tool_result'] and '0.00 บาท' not in result.message
    assert len(result.data['read_followup']['candidates']) == 2
    assert_read_only(api)


@pytest.mark.parametrize('all_day', [True, False])
def test_confirmed_spanning_event_uses_bangkok_dates_and_exclusive_midnight_end(all_day):
    api = SemanticHosted()
    api.now = datetime.fromisoformat(CASE['clock'])
    start, end = ('2026-11-14', '2026-11-16') if all_day else ('2026-11-13T17:00:00Z', '2026-11-15T17:00:00Z')
    api.calendar.events = {'span': event('span', start, end, all_day=all_day, summary='Mock exam span')}
    api.labels = {'e1': 'uncertain'}
    seed(api, [('2026-11-14', 'Mock A', 35), ('2026-11-15', 'Mock B', 45), ('2026-11-16', 'Mock outside', 999)])
    turn(api, CASE['question'],
         read('calendar', '2026-11-01', date_mode='calendar_month', filter_text='คุมสอบ'),
         expense(view='total', start='2026-11-01', end='2026-11-30', order_by='amount', continue_analysis=True), finish())
    result, _ = turn(api, 'มีวันที่ 14', resolve)
    assert result.data['tool_result']['expense'] == '80.00'
    assert result.data['read_query']['calendar_basis']['dates'] == ['2026-11-14', '2026-11-15']
    assert_read_only(api)


def test_two_confirmed_events_on_one_day_do_not_duplicate_expenses(uncertain_report):
    api, _, _ = uncertain_report
    # Read a new question with two distinct appointments on the selected date.
    api.calendar.events['mock-ta']['start']['dateTime'] = '2026-11-14T13:00:00+07:00'
    api.calendar.events['mock-ta']['end']['dateTime'] = '2026-11-14T16:00:00+07:00'
    turn(api, CASE['question'],
         read('calendar', '2026-11-01', date_mode='calendar_month', filter_text='คุมสอบ'),
         expense(view='total', start='2026-11-01', end='2026-11-30', order_by='amount', continue_analysis=True), finish())
    result, _ = turn(api, 'ทั้งสองนัดวันที่ 14 ครับ', resolve)
    assert_selected_day(result)
    assert result.data['read_query']['calendar_basis']['event_count'] == 2
    assert result.data['read_query']['calendar_basis']['dates'] == ['2026-11-14']
    assert_read_only(api)


def test_unshown_uncertain_candidates_require_a_fresh_read_and_cannot_be_guessed(monkeypatch):
    from line_assistant import calendar_inference
    monkeypatch.setattr(calendar_inference, 'MAX_UNCERTAIN', 5)  # more uncertain events than are shown
    api = SemanticHosted()
    api.now = datetime.fromisoformat(CASE['clock'])
    api.calendar.events = {
        str(i): event(str(i), f'2026-11-{14+i}', f'2026-11-{15+i}', all_day=True, summary=f'Mock exam {i}')
        for i in range(6)}
    api.labels = {f'e{i}': 'uncertain' for i in range(1, 7)}
    seed(api, CASE['expense_rows'])
    first, _ = turn(api, CASE['question'],
         read('calendar', '2026-11-01', date_mode='calendar_month', filter_text='คุมสอบ'),
         expense(view='total', start='2026-11-01', end='2026-11-30', order_by='amount', continue_analysis=True), finish())
    assert len(first.data['read_followup']['candidates']) == 5

    def select_shown(body):
        context = json.loads(body['input'][0]['content'])
        pending = context['pending_query']
        return response('resolve_activity_dates', {
            'clarification_ref': pending['clarification_ref'],
            'candidate_refs': [row['candidate_ref'] for row in pending['candidates']],
            'selection_text': context['message'], 'source_refs': [context['current_source_ref']]})

    result, _ = turn(api, 'ห้านัดที่แสดงเป็นคุมสอบทั้งหมดครับ', select_shown)
    assert result.data['tool_result']['expense'] == '60.00'
    assert result.data['read_followup']['unconfirmed_count'] == 1
    assert 'ต้องค้นปฏิทินช่วงเดิมเพิ่ม' in result.message

    def inspect(body):
        context = json.loads(body['input'][0]['content'])
        assert context['pending_query']['unconfirmed_count'] == 1
        assert context['pending_query']['candidates'] == []
        assert 'resolve_activity_dates' not in context['available_tools']
        return response(text='กำลังรอค้นนัดที่เหลือครับ')

    turn(api, 'ที่เหลือล่ะ', inspect)
    assert_read_only(api)


def test_rejecting_one_candidate_does_not_confirm_the_other_and_survives_restart(uncertain_report):
    api, _, _ = uncertain_report
    def reject(body):
        value = resolve(body)
        call = value['output'][0]
        args = json.loads(call['arguments'])
        args['decision'] = 'exclude'
        call['arguments'] = json.dumps(args, ensure_ascii=False)
        return value
    rejected, _ = turn(api, 'วันที่ 14 ไม่ใช่คุมสอบครับ', reject)
    assert 'expense' not in rejected.data['tool_result']
    assert rejected.data['read_followup']['unconfirmed_count'] == 1
    assert rejected.data['read_followup']['confirmed'] == []
    assert len(rejected.data['read_followup']['excluded']) == 1
    assert 'ยังไม่ได้รับการยืนยัน' in rejected.message

    def confirm_remaining(body):
        context = json.loads(body['input'][0]['content'])
        pending = context['pending_query']
        assert pending['excluded_count'] == 1 and pending['confirmed_count'] == 0
        assert len(pending['candidates']) == 1
        return response('resolve_activity_dates', {
            'clarification_ref': pending['clarification_ref'], 'candidate_refs': [pending['candidates'][0]['candidate_ref']],
            'decision': 'confirm', 'selection_text': 'วันที่ 29 ใช่ครับ', 'source_refs': [context['current_source_ref']]})
    result, _ = turn(api, 'วันที่ 29 ใช่ครับ', confirm_remaining)
    assert result.data['tool_result']['expense'] == '700.00'
    assert result.data['read_query']['calendar_basis']['dates'] == ['2026-11-29']
    assert_read_only(api)


def test_real_model_failure_trace_cannot_confirm_day29_with_a_day14_rejection(uncertain_report):
    api, _, _ = uncertain_report
    before = (len(api.calendar_requests), len(api.extra_requests))
    def wrong_complement(body):
        context = json.loads(body['input'][0]['content'])
        pending = context['pending_query']
        other = next(e for e in pending['candidates'] if e['start'].startswith('2026-11-29'))
        return response('resolve_activity_dates', {
            'clarification_ref': pending['clarification_ref'], 'candidate_refs': [other['candidate_ref']],
            'decision': 'confirm', 'selection_text': context['message'], 'source_refs': [context['current_source_ref']]})
    result, _ = turn(api, 'วันที่ 14 ไม่ใช่คุมสอบครับ', wrong_complement)
    assert result.data['tool_result']['error_code'] == 'invalid_selection'
    assert 'expense' not in result.data['tool_result']
    assert before == (len(api.calendar_requests), len(api.extra_requests))
    assert_read_only(api)


@pytest.mark.parametrize('confirmation', ['มีวันที่ ๑๔', '14', 'วันที่ 14 พ.ย. 2026 ครับ'])
def test_literal_date_grounding_preserves_thai_digits_bare_day_and_explicit_month(uncertain_report, confirmation):
    api, _, _ = uncertain_report
    result, _ = turn(api, confirmation, resolve)
    assert_selected_day(result)
    assert_read_only(api)


@pytest.mark.parametrize('confirmation', CASE['confirmations'])
def test_short_confirmation_must_continue_selected_day_instead_of_reasking_period(uncertain_report, confirmation):
    api, first, _ = uncertain_report

    def choose(body):
        assert_history(body, first, confirmation)
        return resolve(body)

    result, _ = turn(api, confirmation, choose)
    assert_read_only(api)
    assert_selected_day(result)


def test_guided_control_can_reread_confirmed_event_and_sum_only_its_day(uncertain_report):
    api, first, _ = uncertain_report
    selected = CASE['expect']['selected_day']
    api.labels = {'e1': 'match'}

    def confirmed_calendar(body):
        context = assert_history(body, first, 'มีวันที่ 14')
        value = read('calendar', selected, filter_text='คุมสอบ')(body)
        call = value['output'][0]
        args = json.loads(call['arguments'])
        # The activity is in the earlier owner message; the current message
        # supplies the day. Cite both originals, never an assistant quotation.
        args['source_refs'].append(context['history'][0]['source_ref'])
        call['arguments'] = json.dumps(args, ensure_ascii=False)
        return value

    result, _ = turn(api, 'มีวันที่ 14', confirmed_calendar,
                     expense(start=None, end=None, date_mode='previous', view='total', join=True))
    assert_selected_day(result)
    assert result.data['read_query']['calendar_basis']['dates'] == [selected]
    assert result.data['read_query']['calendar_basis']['event_count'] == 1
    assert 'ไม่ได้แยกว่าเป็นค่าใช้จ่ายของนัดโดยตรง' in result.message
    selection = json.loads(api.selection_bodies[-1]['input'][0]['content'])
    assert [row['text'] for row in selection['owner_context']] == [CASE['question'], 'มีวันที่ 14']
    assert len(selection['calendar_selection']['events']) == 1
    assert_read_only(api)


def test_unconditional_previous_month_is_blocked_then_repaired_to_selected_day(uncertain_report):
    api, _, _ = uncertain_report
    result, _ = turn(api, 'มีวันที่ 14',
                     expense(start=None, end=None, date_mode='previous', view='total'), resolve)
    assert_selected_day(result)
    assert CASE['expect']['unconditional_month_expense'] not in result.message
    assert_read_only(api)


def test_followup_cannot_reuse_old_calendar_query_refs_as_current_evidence(uncertain_report):
    api, first, _ = uncertain_report
    old_ref = first.data['tool_result']['query_refs'][0]

    def stale_join(body):
        value = expense(start=None, end=None, date_mode='previous', view='total')(body)
        call = value['output'][0]
        args = json.loads(call['arguments'])
        args['calendar_query_refs'] = [old_ref]
        call['arguments'] = json.dumps(args)
        return value

    def repair(body):
        assert json.loads(body['input'][-1]['output'])['error_code'] == 'invalid_calendar_refs'
        return resolve(body)
    result, _ = turn(api, 'มีวันที่ 14', stale_join, repair)
    assert_selected_day(result)
    assert_read_only(api)
