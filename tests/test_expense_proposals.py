"""Current model contract: explicit groups/quotes, no sentence regrouping."""
from copy import deepcopy
from decimal import Decimal
import json
import socket

import pytest

from line_assistant.expense_proposals import Proposal
from line_assistant import expense_evidence, expense_sources  # load original aliases before patching
from line_assistant.history_context import source_ref
from line_assistant.history_persistence import history_snapshot, restore_history
from test_catalog_names_v09 import harness
from test_history_first import response
from test_raw_history import context
from test_pending_records import patch


@pytest.fixture(autouse=True)
def offline_and_no_group_parser(monkeypatch):
    def deny(*a, **k): raise AssertionError('Current proposals must not use network or old grouping grammar')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    for module, names in [('expense_sources', ['clauses']),
                          ('expense_evidence', ['occurrences', 'assign', 'expand_counted_entries'])]:
        for name in names: monkeypatch.setattr('line_assistant.' + module + '.' + name, deny)


def q(text, occurrence=0, ref='CURRENT'):
    return {'source_ref': ref, 'text': text, 'occurrence': occurrence}


def group(ref, name, portion, *, item=None, source='CURRENT'):
    return {'group_ref': ref, 'item': item or name,
        'item_source': q(name, ref=source), 'sources': [q(portion, ref=source)]}


def record(ref, amount, *, occurrence=0, date=None, source='CURRENT', **kw):
    return {'group_ref': ref, 'amount': amount,
        'amount_source': q(str(amount), occurrence, source) if amount is not None else None,
        'reuse_previous_amount':False,
        'date_source': q(date, ref=source) if date else None,
        'date_value': None, 'date_ambiguous':False, 'date_ref': None, 'note': None, 'note_source': None, **kw}


def proposal(groups, entries, *, counts=(), context_numbers=()):
    return {'draft_ref': None, 'draft_version': None, 'groups': groups, 'entries': entries,
        'counts': list(counts), 'context_numbers': list(context_numbers), 'only_if_absent': False}


def call(args, *, inspect=None):
    def decide(body):
        c = context(body)
        def refs(v):
            if isinstance(v, dict): return {k: c['current_source_ref'] if k == 'source_ref' and x == 'CURRENT' else refs(x) for k, x in v.items()}
            if isinstance(v, list): return [refs(x) for x in v]
            return v
        value = refs(deepcopy(args))
        # All scripted current-protocol calls must match the actual active
        # strict schema, unlike legacy compatibility test helpers.
        offered = next(t for t in body['tools'] if t['name'] == 'prepare_expenses')
        assert set(value) == set(offered['parameters']['properties'])
        assert all(set(e) == set(offered['parameters']['properties']['entries']['items']['properties']) for e in value['entries'])
        if inspect: inspect(body, value)
        return response(name='prepare_expenses', args=value)
    return decide


def send(h, text, args):
    return h.send(text, call(args), call(args), response(text='synthetic stopped failure'))


def mixed(separator=', ', join=' แล้วก็ '):
    first = 'Metro ' + separator.join(['61', '61', '19'])
    second = 'Shuttle 27'
    text = 'เมื่อวาน ' + first + join + second
    args = proposal([group('a', 'Metro', first), group('b', 'Shuttle', second)],
        [record('a', 61, occurrence=0, date='เมื่อวาน'), record('a', 61, occurrence=1, date='เมื่อวาน'),
         record('a', 19, date='เมื่อวาน'), record('b', 27, date='เมื่อวาน')])
    return text, args


@pytest.mark.parametrize('separator', [', ', ',', ' บาท ', 'บาท', ' / ', '\n', ' แล้วราคาอีกยอด ', ' → '])
@pytest.mark.parametrize('join', [' แล้วก็ ', ' | ', '\n'])
def test_llm_boundaries_handle_lists_without_a_connector_grammar(separator, join):
    h = harness(('Metro', 'Shuttle')); text, args = mixed(separator, join)
    result = send(h, text, args)
    assert result.status == 'preview', result
    rows = h.draft()['entries']
    assert [(e['item'], e['amount']) for e in rows] == [('Metro', '61'), ('Metro', '61'), ('Metro', '19'), ('Shuttle', '27')]
    assert {e['date'] for e in rows} == {'2026-10-15'}
    assert [text[slice(*e['amount_occurrence']['span'])] for e in rows] == ['61', '61', '19', '27']
    assert len(h.port.requests) == 1 and not h.sheets.entries
    event = h.event(result.data['confirm_command'])
    assert h.service.handle(event).status == 'recorded' and len(h.sheets.entries) == 4
    assert h.service.handle(deepcopy(event)).status == 'recorded' and len(h.sheets.entries) == 4


def test_actual_tool_schema_is_closed_required_and_only_offers_new_protocol():
    from line_assistant.expense_proposals import definition
    tool = definition([{'date_ref': 'date_example'}]); p = tool['parameters']
    def check(v):
        if isinstance(v, dict):
            if v.get('type') == 'object':
                assert v['additionalProperties'] is False and set(v['required']) == set(v['properties'])
            for child in v.values(): check(child)
        elif isinstance(v, list):
            for child in v: check(child)
    check(p)
    assert not {'source_refs', 'field_sources', 'item_text', 'date_text'} & set(p['properties']['entries']['items']['properties'])
    assert p['properties']['entries']['items']['properties']['date_ref']['enum'] == [None, 'date_example']
    h = harness(('Metro',)); args = proposal([group('g', 'Metro', 'Metro 61')], [record('g', 61)])
    def inspect(body, values):
        assert len([t for t in body['tools'] if t['name'] == 'prepare_expenses']) == 1
        Proposal.model_validate(values)
        assert next(t for t in body['tools'] if t['name'] == 'prepare_expenses')['parameters'] == definition()['parameters']
    assert h.send('Metro 61', call(args, inspect=inspect)).status == 'preview'


@pytest.mark.parametrize('mutate,code', [
    (lambda p: p['entries'].pop(2), 'expense_unaccounted_number'),
    (lambda p: p['entries'][1]['amount_source'].update(occurrence=0), 'expense_amount_reused_without_count'),
    (lambda p: p['entries'][0].update(amount=62), 'expense_amount_mismatch'),
    (lambda p: p['entries'][0]['amount_source'].update(text='6'), 'expense_partial_numeric_quote'),
    (lambda p: p['entries'][0]['amount_source'].update(text='99'), 'expense_quote_not_found'),
    (lambda p: p['entries'][0]['amount_source'].update(source_ref='msg_unavailable'), 'source_not_in_visible_context'),
    (lambda p: p['entries'][0].update(group_ref='missing'), 'expense_unknown_group'),
    (lambda p: p['groups'][1]['sources'][0].update(text='Metro 61, 61, 19 แล้วก็ Shuttle 27'), 'expense_groups_overlap'),
    (lambda p: p['entries'][2].update(group_ref='b'), 'expense_field_outside_group'),
    (lambda p: p['entries'][0].update(note='invented', note_source=q('Metro')), 'expense_field_overlap'),
])
def test_invalid_proposal_repairs_before_any_review_or_state_change(mutate, code):
    h = harness(('Metro', 'Shuttle')); text, args = mixed(); mutate(args)
    result = send(h, text, args)
    assert result.status != 'preview' and not h.sheets.entries and not h.state.drafts
    assert any(s['result'].get('error') == code for s in h.service.trace), h.service.trace
    if code.startswith('expense_'):
        assert result.data['tool_result']['validation_feedback']['kind'] == 'model_arguments'
        assert 'ยังบอกไม่ได้' not in result.data['tool_result']['validation_feedback']['facts'].get('reason', '')
    assert h.service._literal_expense is None if hasattr(h.service, '_literal_expense') else True


def test_missing_amount_is_a_partial_fact_but_never_silently_drops_a_visible_amount():
    h = harness(('Metro',)); args = proposal([group('g', 'Metro', 'Metro')], [record('g', None)])
    result = send(h, 'จด Metro ให้หน่อย', args)
    assert result.status == 'clarification' and h.draft()['entries'][0]['amount'] is None
    assert h.draft()['entries'][0]['date'] == '2026-10-16' and h.draft()['review'] is None
    other = harness(('Metro',)); result = send(other, 'Metro 61', args)
    assert not other.state.drafts and result.status != 'preview'


@pytest.mark.parametrize('count', ['3', '๓', 'สาม'])
def test_explicit_repetition_is_model_selected_and_has_distinct_ordinals(count):
    h = harness(('Metro',)); text = 'Metro ครั้งละ 61 บาท ' + count + ' ครั้ง'
    args = proposal([group('g', 'Metro', text)], [record('g', 61) for _ in range(3)],
        counts=[{'source': q(count), 'entries': [0, 1, 2], 'kind': 'occurrences'}])
    result = send(h, text, args)
    assert result.status == 'preview', result
    assert [e['amount_occurrence']['ordinal'] for e in h.draft()['entries']] == [0, 1, 2]
    assert len({e['amount_occurrence']['occurrence_ref'] for e in h.draft()['entries']}) == 3


@pytest.mark.parametrize('kind,indexes', [('batch', [0, 1]), ('occurrences', [0]), ('occurrences', [0, 0])])
def test_invalid_count_cannot_authorize_duplicate_amount(kind, indexes):
    h = harness(('Metro',)); text = 'Metro 61 บาท 2 ครั้ง'
    args = proposal([group('g', 'Metro', text)], [record('g', 61), record('g', 61)],
        counts=[{'source': q('2'), 'entries': indexes, 'kind': kind}])
    assert send(h, text, args).status != 'preview' and not h.state.drafts


@pytest.mark.parametrize('amount,quote_text', [('1240.50', '1,240.50'), ('61', '๖๑'), ('61', 'หกสิบเอ็ด')])
def test_numeric_evidence_normalizes_without_inferring_its_payment_role(amount, quote_text):
    h = harness(('Metro',)); text = 'Metro ' + quote_text
    args = proposal([group('g', 'Metro', text)], [record('g', amount, amount_source=q(quote_text))])
    result = send(h, text, args)
    assert result.status == 'preview' and Decimal(h.draft()['entries'][0]['amount']) == Decimal(amount)


def test_numeric_item_names_and_literal_notes_are_separate_from_money():
    h = harness(('Bus 56',)); text = 'Bus 56 61 บาท note จุดที่ 9'
    args = proposal([group('g', 'Bus 56', 'Bus 56 61 บาท')],
        [record('g', 61, note='จุดที่ 9', note_source=q('จุดที่ 9'))])
    result = send(h, text, args)
    assert result.status == 'preview', result
    row = h.draft()['entries'][0]
    assert (row['item'], row['amount'], row['note']) == ('Bus 56', '61', 'จุดที่ 9')


def test_explicit_nonpayment_numbers_are_disclosed_in_the_bound_review():
    h = harness(('Metro',)); text = 'รายการก่อนที่ 2 เพิ่ม Metro 61'
    args = proposal([group('g', 'Metro', 'Metro 61')], [record('g', 61)],
        context_numbers=[{'source': q('2'), 'reason': 'อ้างถึงลำดับรายการก่อนหน้า'}])
    result = send(h, text, args)
    assert result.status == 'preview' and 'ไม่ได้ลงเป็นยอดรายจ่าย: 2' in result.message
    assert result.data['review_notices'] and h.draft()['entries'][0]['context_numbers']


def test_repair_feedback_locates_the_uncovered_number_and_same_turn_can_fix_it():
    h = harness(('Metro', 'Shuttle')); text, args = mixed(); wrong = deepcopy(args); wrong['entries'].pop(2)
    def repair(body):
        feedback = json.loads(body['input'][-1]['output'])['repair_feedback']
        assert feedback['error_code'] == 'expense_unaccounted_number'
        assert any("'19'" in v and '[' in v for v in feedback['required_inputs'])
        return call(args)(body)
    result = h.send(text, call(wrong), repair)
    assert result.status == 'preview' and len(h.draft()['entries']) == 4 and len(h.port.requests) == 2
    assert not h.sheets.entries and h.service._literal_expense is None


def test_unknown_name_can_be_enrolled_and_pending_fields_patched_without_grouping():
    h = harness(('Metro',)); text = 'ร้านทดสอบใหม่ 61'
    args = proposal([group('g', 'ร้านทดสอบใหม่', text)], [record('g', 61)])
    result = send(h, text, args)
    assert result.status == 'clarification' and not h.draft()['review']
    result = h.send('จัดเข้าหมวดเดินทาง', patch({0: [('category', 'Transportation', 'เดินทาง')]}))
    assert result.status == 'preview', result
    result = h.send('เปลี่ยนเป็นเมื่อวานแล้วแก้ยอดเป็น 63', patch({0: [('date_text', 'เมื่อวาน', 'เมื่อวาน'), ('amount', '63', '63')]}))
    assert result.status == 'preview', result
    row = h.draft()['entries'][0]
    assert (row['date'], row['amount'], row['mapping']['category']) == ('2026-10-15', '63', 'Transportation')
    assert not h.sheets.entries


def test_original_evidence_survives_history_roundtrip_and_fresh_confirmation():
    h = harness(('Metro', 'Shuttle')); text, args = mixed()
    result = send(h, text, args); before = deepcopy(h.draft())
    assert result.status == 'preview'
    restore_history(h.state, history_snapshot(h.state), h.service.settings.owner_id)
    assert h.draft() == before
    assert all(e['source_quote'] == text and e['fact_sources']['amount'] == [source_ref('history-1')] for e in before['entries'])
    confirmed = h.service.handle(h.event(result.data['confirm_command']))
    assert confirmed.status == 'recorded' and len(h.sheets.entries) == 4


def test_signed_cold_factory_save_and_replay_use_new_protocol():
    from fastapi.testclient import TestClient
    from test_production_history import Hosted
    api = Hosted()
    for i, name in enumerate(['Metro', 'Shuttle'], 25):
        for col, value in enumerate([name, 'Transportation', ''], 1): api.google.cells[('Database', i, col)] = value
    text, args = mixed()
    with TestClient(api.app(raw_history_enabled=True)) as client:
        reviewed, _ = api.send_text(client, text, call(args))
    assert reviewed.status == 'preview', reviewed
    assert not api.google.sheet_writes
    with TestClient(api.app(raw_history_enabled=True)) as client:
        saved, event = api.send_text(client, reviewed.data['confirm_command'])
    assert saved.status == 'recorded' and api.google.sheet_writes == 1
    with TestClient(api.app(raw_history_enabled=True)) as client:
        assert api.send(client, deepcopy(event)).status_code == 200
    assert api.google.sheet_writes == 1


def test_missing_amount_offer_remains_available_through_the_pending_schema():
    from test_previous_expense_prices import PriceHosted, turn
    api = PriceHosted(); args = proposal([group('g', 'Spotify', 'Spotify')], [record('g', None)])
    offered, _ = turn(api, 'จด Spotify ให้หน่อย', call(args))
    assert offered.status == 'clarification', offered
    assert offered.data['tool_result']['entries'][0]['previous_price']['state'] == 'offered'
    def accept(body):
        c = context(body); view = c['pending_records'][0]; row = view['records'][0]
        choice = next(f for f in row['fields'] if f['name'] == 'previous_price_ref')['choices'][0]
        return response(name='patch_pending_records', args={'draft_ref': view['draft_ref'], 'draft_version': view['version'],
            'records': [{'record_ref': row['record_ref'], 'changes': [{'field': 'previous_price_ref', 'value': choice,
                'source_text': 'ใช้ยอดเดิม', 'source_ref': c['current_source_ref'], 'temporal_ref': None}]}]})
    reviewed, _ = turn(api, 'ใช้ยอดเดิม', accept)
    assert reviewed.status == 'preview', reviewed
    row = reviewed.data['tool_result']['entries'][0]
    assert row['previous_price']['state'] == 'accepted' and row['amount'] == '169.00'
    assert not api.google.sheet_writes


def test_name_and_prices_can_come_from_different_original_messages():
    h = harness(('Metro',))
    h.send('รายการที่พูดถึงคือ Metro', response(text='synthetic context'))
    previous = h.service._current['source_ref']
    g = group('g', 'Metro', 'Metro', source=previous)
    g['sources'].append(q('61, 61, 19'))
    args = proposal([g], [record('g', 61), record('g', 61, occurrence=1), record('g', 19)])
    result = send(h, 'จดอันข้างบน 61, 61, 19', args)
    assert result.status == 'preview', result
    assert len(h.draft()['entries']) == 3 and not h.sheets.entries
    assert all(e['fact_sources']['item'] == [previous] for e in h.draft()['entries'])


def test_followup_after_failed_batch_can_select_historical_sibling_without_keyword_guard():
    h = harness(('Metro', 'Shuttle')); text, original = mixed(); wrong = deepcopy(original)
    wrong['entries'][1]['amount_source']['occurrence'] = 0
    result = send(h, text, wrong)
    assert result.status != 'preview' and not h.state.drafts
    previous = h.service._current['source_ref']
    args = deepcopy(original)
    def change_refs(v):
        if isinstance(v, dict):
            if v.get('source_ref') == 'CURRENT': v['source_ref'] = previous
            for child in v.values(): change_refs(child)
        elif isinstance(v, list):
            for child in v: change_refs(child)
    change_refs(args)
    # Repeated new source numbers need their own evidence; select current
    # Metro facts and the referenced sibling/date from the original request.
    args['groups'][0]['sources'] = [q('Metro 61 บาท 61 บาท 19 บาท')]
    args['groups'][0]['item_source'] = q('Metro')
    for i, n in enumerate([61, 61, 19]): args['entries'][i]['amount_source'] = q(str(n), 1 if i == 1 else 0)
    result = send(h, 'Metro 61 บาท 61 บาท 19 บาท ที่คุยเมื่อกี้', args)
    assert result.status == 'preview', result
    assert [(e['item'], e['amount']) for e in h.draft()['entries']] == [('Metro', '61'), ('Metro', '61'), ('Metro', '19'), ('Shuttle', '27')]
    assert not h.sheets.entries


def test_new_payment_can_reuse_recorded_literal_facts_but_needs_new_consent():
    h = harness(('Metro',)); text = 'Metro 61'
    args = proposal([group('g', 'Metro', text)], [record('g', 61)])
    first = send(h, text, args); previous = h.service._current['source_ref']
    assert h.send(first.data['confirm_command']).status == 'recorded'
    args = proposal([group('g', 'Metro', text, source=previous)],
        [record('g', 61, source=previous, date_source=q('วันที่ 12'))])
    second = send(h, 'ลงยอดนั้นอีกครั้งวันที่ 12', args)
    assert second.status == 'preview', second
    assert len(h.sheets.entries) == 1 and second.data['confirm_command'] != first.data['confirm_command']
    assert second.data['tool_result']['entries'][0]['date'] == '2026-10-12'
    assert h.send(second.data['confirm_command']).status == 'recorded' and len(h.sheets.entries) == 2


def test_model_can_select_an_offered_date_without_a_fixed_reference_phrase():
    h = harness(('Metro', 'Shuttle'))
    args = proposal([group('g', 'Metro', 'Metro 61')], [record('g', 61, date='เมื่อวาน')])
    first = send(h, 'เมื่อวาน Metro 61', args)
    assert h.send(first.data['confirm_command']).status == 'recorded'
    args = proposal([group('g', 'Shuttle', 'Shuttle 27')], [record('g', 27)])
    def choose_date(body, values):
        values['entries'][0]['date_ref'] = context(body)['date_references'][0]['date_ref']
    result = h.send('แล้วก็ Shuttle 27 อีกเที่ยว', call(args, inspect=choose_date))
    assert result.status == 'preview', result
    assert result.data['tool_result']['entries'][0]['date'] == '2026-10-15'
    assert 'ใช้วันที่เดียวกับรายจ่ายที่ยืนยันก่อนหน้า' in result.message
    assert len(h.sheets.entries) == 1


def test_groups_can_be_interleaved_without_requiring_adjacent_prices():
    h = harness(('Metro', 'Shuttle'))
    text = 'Metro จ่าย 61 / Shuttle จ่าย 27 / ส่วนอีก 19 เป็นของชื่อแรก'
    a = group('a', 'Metro', 'Metro จ่าย 61'); a['sources'].append(q('ส่วนอีก 19 เป็นของชื่อแรก'))
    args = proposal([a, group('b', 'Shuttle', 'Shuttle จ่าย 27')],
        [record('a', 61), record('b', 27), record('a', 19)])
    result = send(h, text, args)
    assert result.status == 'preview', result
    assert [(e['item'], e['amount']) for e in h.draft()['entries']] == [('Metro', '61'), ('Shuttle', '27'), ('Metro', '19')]
    assert not h.sheets.entries
