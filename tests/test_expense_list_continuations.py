"""Synthetic list punctuation, repeated payments and atomic pending corrections."""
from copy import deepcopy
import json

import pytest

from test_catalog_names_v09 import harness, entry, prepare
from test_pending_records import patch
from test_history_first import response
from test_raw_history import context


def payments(h):
    return next(d for d in h.state.drafts.values() if d['status'] != 'recorded')['entries']


@pytest.mark.parametrize('separator', [', ', ',\t', ' กับ ', ' และ ', ' / '])
def test_one_label_multiple_prices_keeps_duplicate_occurrences_and_original_spans(separator):
    h = harness(('Metro',))
    text = 'เมื่อวาน Metro ' + separator.join(['61', '61', '19'])
    result = prepare(h, text, [entry(item='Metro', amount=v) for v in [61, 61, 19]])
    assert result.status == 'preview', result
    rows = payments(h)
    assert [(e['amount'], e['date']) for e in rows] == [(str(v), '2026-10-15') for v in [61, 61, 19]]
    assert len({e['amount_occurrence']['occurrence_ref'] for e in rows}) == 3
    assert [text[slice(*e['amount_occurrence']['span'])] for e in rows] == ['61', '61', '19']
    assert len(h.port.requests) == 1 and not h.sheets.entries
    event = h.event(result.data['confirm_command'])
    assert h.service.handle(event).status == 'recorded' and len(h.sheets.entries) == 3
    assert h.service.handle(deepcopy(event)).status == 'recorded' and len(h.sheets.entries) == 3


@pytest.mark.parametrize('prefix', ['', '3 ครั้ง ', 'สามครั้ง '])
@pytest.mark.parametrize('separator', [' ', ', ', ' กับ '])
def test_each_price_can_declare_its_own_occurrence_count(prefix, separator):
    h = harness(('Metro',))
    text = 'Metro ' + prefix + '61 2 ครั้ง' + separator + '19 1 ครั้ง เมื่อวาน'
    result = prepare(h, text, [entry(item='Metro', amount=v) for v in [61, 61, 19]])
    assert result.status == 'preview', result
    rows = payments(h)
    assert [e['amount'] for e in rows] == ['61', '61', '19']
    assert [e['amount_occurrence']['ordinal'] for e in rows] == [0, 1, 0]
    assert len({e['amount_occurrence']['occurrence_ref'] for e in rows}) == 3
    assert not h.sheets.entries and len(h.port.requests) == 1


@pytest.mark.parametrize('text', ['Metro 4 ครั้ง 61 2 ครั้ง 19 1 ครั้ง',
                                 'Metro 61 0 ครั้ง 19 1 ครั้ง',
                                 'Metro 61 2.5 ครั้ง 19 1 ครั้ง',
                                 'Metro 61 2 ครั้ง รวม 19 1 ครั้ง'])
def test_inconsistent_or_total_like_rate_groups_do_not_authorize_a_review(text):
    h = harness(('Metro',))
    result = prepare(h, text, [entry(item='Metro', amount=v) for v in [61, 61, 19]])
    assert result.status != 'preview' and not h.sheets.entries
    assert all(d['review'] is None for d in h.state.drafts.values())


@pytest.mark.parametrize('quote', ['31', 'แก้รายการ 2 เป็น 31', 'ทั้งคู่คือเมื่อวาน แก้รายการ 2 เป็น 31'])
def test_shared_date_and_one_ordinal_amount_correction_accept_full_owner_evidence(quote):
    h = harness(('รถรับส่ง',))
    prepare(h, 'รถรับส่ง 34 กับ 34 บาท', [entry(item='รถรับส่ง', amount=34)] * 2)
    before = deepcopy(payments(h))
    result = h.send('ทั้งคู่คือเมื่อวาน แก้รายการ 2 เป็น 31', patch({
        0: [('date_text', 'เมื่อวาน', 'เมื่อวาน')],
        1: [('date_text', 'เมื่อวาน', 'เมื่อวาน'), ('amount', 31, quote)],
    }), response(text='synthetic fallback'))
    assert result.status == 'preview', result
    rows = payments(h)
    assert [(e['amount'], e['date']) for e in rows] == [('34', '2026-10-15'), ('31', '2026-10-15')]
    assert rows[0]['fact_sources']['amount'] == before[0]['fact_sources']['amount']
    assert not h.sheets.entries


@pytest.mark.parametrize('raw', ['1,23', '1,,000', '1.234', '-61'])
def test_list_punctuation_does_not_repair_malformed_money(raw):
    h = harness(('Metro',))
    result = prepare(h, f'Metro {raw}, 19', [entry(item='Metro', amount=61), entry(item='Metro', amount=19)])
    assert result.status != 'preview' and not h.sheets.entries


def test_thousands_separator_remains_one_amount_inside_a_list():
    h = harness(('Metro',))
    result = prepare(h, 'Metro 1,261.50, 19', [entry(item='Metro', amount='1261.50'), entry(item='Metro', amount=19)])
    assert result.status == 'preview', result
    assert [e['amount'] for e in payments(h)] == ['1261.5', '19']


def propose(values):
    def decide(body):
        source = context(body)['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref': None, 'draft_version': None,
            'entries': [entry(item=name, amount=amount, source_refs=[source]) for name, amount in values]})
    return decide


def test_incomplete_model_batch_is_repaired_in_the_same_turn_without_owner_retyping():
    h = harness(('Metro', 'Tram'))
    def repair(body):
        feedback = json.loads([r for r in body['input'] if r.get('type') == 'function_call_output'][-1]['output'])
        assert feedback['error'] == 'expense_batch_incomplete'
        assert set(feedback['repair_feedback']['allowed_tools']) == {'prepare_expenses', 'resolve_temporal'}
        assert not h.state.drafts and not h.sheets.entries
        return propose([('Metro', 61), ('Tram', 19)])(body)
    result = h.send('เมื่อวาน Metro 61, Tram 19', propose([('Metro', 61)]), repair)
    assert result.status == 'preview', result
    assert [(e['item'], e['amount']) for e in payments(h)] == [('Metro', '61'), ('Tram', '19')]
    assert len(h.port.requests) == 2 and not h.sheets.entries


def test_incomplete_model_batch_repeated_failure_stops_without_partial_review():
    h = harness(('Metro', 'Tram'))
    incomplete = propose([('Metro', 61)])
    result = h.send('Metro 61, Tram 19', incomplete, incomplete)
    assert result.status != 'preview' and not h.state.drafts and not h.sheets.entries
    assert len(h.port.requests) == 2
    assert h.service.trace[-1]['result']['error'] == 'expense_batch_incomplete'


def test_patch_amount_evidence_failure_repairs_the_same_owned_rows_atomically():
    h = harness(('รถรับส่ง',))
    prepare(h, 'รถรับส่ง 34 กับ 34 บาท', [entry(item='รถรับส่ง', amount=34)] * 2)
    before = deepcopy(payments(h))
    def repair(body):
        feedback = json.loads([r for r in body['input'] if r.get('type') == 'function_call_output'][-1]['output'])
        assert feedback['invalid_fields'] == ['entries[1].amount_source']
        assert set(feedback['repair_feedback']['allowed_tools']) == {'patch_pending_records', 'resolve_temporal'}
        assert 'prepare_expenses' not in {t['name'] for t in body['tools']}
        assert payments(h) == before and not h.sheets.entries
        return patch({0: [('date_text', 'เมื่อวาน', 'เมื่อวาน')],
                      1: [('date_text', 'เมื่อวาน', 'เมื่อวาน'), ('amount', 31, '31')]})(body)
    result = h.send('ทั้งคู่เมื่อวาน แก้รายการ 2 เป็น 31 แล้วดูยอด 37 ไว้ก่อน',
        patch({0: [('date_text', 'เมื่อวาน', 'เมื่อวาน')],
               1: [('date_text', 'เมื่อวาน', 'เมื่อวาน'), ('amount', 31, '31 แล้วดูยอด 37 ไว้ก่อน')]}), repair)
    assert result.status == 'preview', result
    assert [e['amount'] for e in payments(h)] == ['34', '31']
    assert not h.sheets.entries


def test_unrelated_failed_batch_does_not_block_a_fresh_single_unknown_name_or_invent_a_date():
    h = harness(('Metro', 'Tram'))
    incomplete = propose([('Metro', 61)])
    h.send('Metro 61, Tram 19', incomplete, incomplete)
    result = h.send('Diner Aurora 742', propose([('Diner Aurora', 742)]))
    assert result.status == 'clarification'
    assert [(e['item'], e['amount'], e['date']) for e in payments(h)] == [('Diner Aurora', '742', '2026-10-16')]
    assert 'วันที่ ยังไม่ระบุ' not in result.message
    assert not h.sheets.entries


def test_counted_group_expansion_and_original_proofs_survive_cold_restore():
    from line_assistant.history_context import HistoryState
    from line_assistant.history_persistence import history_snapshot, restore_history
    h = harness(('Metro',))
    result = prepare(h, 'Metro 3 ครั้ง 61 2 ครั้ง 19 1 ครั้ง', [entry(item='Metro', amount=61)])
    assert result.status == 'preview', result
    saved = history_snapshot(h.state)
    state = HistoryState()
    state.clock = lambda: h.now
    state.enable_raw_history(journal=deepcopy(h.state.raw_journal))
    restore_history(state, saved, 'mock-owner')
    assert state.drafts == h.state.drafts and [e['amount'] for e in payments(h)] == ['61', '61', '19']


@pytest.mark.parametrize('draft_capacity', [1, 8])
def test_invented_date_wording_repairs_creation_without_asking_for_a_known_default(monkeypatch, draft_capacity):
    monkeypatch.setattr('line_assistant.history_first.MAX_DRAFTS', draft_capacity)
    h = harness(('Metro',))
    def wrong(body):
        result = propose([('Diner Aurora', 742)])(body)
        args = json.loads(result['output'][0]['arguments'])
        args['entries'][0]['date_text'] = 'เมื่อวาน'
        result['output'][0]['arguments'] = json.dumps(args)
        return result
    def repair(body):
        feedback = json.loads([r for r in body['input'] if r.get('type') == 'function_call_output'][-1]['output'])
        assert feedback['error'] == 'invalid_fields'
        assert feedback['invalid_fields'] == ['entries[0].date_text_source']
        assert payments(h)[0]['amount'] == '742' and not h.sheets.entries
        assert 'patch_pending_records' not in {t['name'] for t in body['tools']}
        return propose([('Diner Aurora', 742)])(body)
    result = h.send('Diner Aurora 742', wrong, repair)
    assert result.status == 'clarification', result
    assert len(h.port.requests) == 2 and len(h.state.drafts) == 1
    assert next(iter(h.state.drafts.values()))['version'] == 2
    assert payments(h)[0]['date'] == '2026-10-16'
    assert 'วันที่ ยังไม่ระบุ' not in result.message and not h.sheets.entries


def test_failed_creation_repair_retains_verified_facts_and_cannot_replace_work_on_another_turn():
    h = harness(('Metro',))
    def wrong(body):
        result = propose([('Metro', 61)])(body)
        args = json.loads(result['output'][0]['arguments'])
        args['entries'][0]['date_text'] = 'เมื่อวาน'
        result['output'][0]['arguments'] = json.dumps(args)
        return result
    result = h.send('Metro 61', wrong, wrong)
    assert result.status != 'preview' and payments(h)[0]['amount'] == '61'
    before = deepcopy(h.state.drafts)
    def unrelated(body):
        refs = next(iter(before.values()))['entries'][0]['source_refs']
        return response(name='prepare_expenses', args={'draft_ref': None, 'draft_version': None,
            'entries': [entry(item='Metro', amount=61, source_refs=refs)]})
    h.send('ขอดูรายการ', unrelated, response(text='synthetic fallback'))
    assert h.state.drafts == before and not h.sheets.entries


def test_count_clarification_can_cite_an_earlier_name_without_repeating_it():
    h = harness(('Metro',))
    h.send('Metro', response(text='synthetic question about payment amounts'))
    name_ref = h.service._current['source_ref']
    def clarify(body):
        current = context(body)['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref': None, 'draft_version': None,
            'entries': [entry(item='Metro', amount=v, source_refs=[name_ref, current],
                field_sources={'item': [name_ref], 'amount': [current], 'date': [current], 'note': []})
                for v in [61, 61, 19]]})
    result = h.send('มีสามครั้ง 61 2 ครั้ง 19 1 ครั้ง เมื่อวาน', clarify)
    assert result.status == 'preview', result
    rows = payments(h)
    assert [(e['amount'], e['date']) for e in rows] == [('61', '2026-10-15'), ('61', '2026-10-15'), ('19', '2026-10-15')]
    assert all(e['fact_sources']['item'] == [name_ref] for e in rows)
    assert all(e['amount_occurrence']['source_ref'] == h.service._current['source_ref'] for e in rows)
    assert not h.sheets.entries


def test_cited_amount_group_cannot_borrow_another_named_items_price():
    h = harness(('Metro', 'Tram'))
    h.send('Metro', response(text='synthetic question about payment amounts'))
    name_ref = h.service._current['source_ref']
    def wrong(body):
        current = context(body)['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref': None, 'draft_version': None,
            'entries': [entry(item='Metro', amount=v, source_refs=[name_ref, current],
                field_sources={'item': [name_ref], 'amount': [current], 'date': [], 'note': []})
                for v in [61, 61, 19]]})
    result = h.send('Tram 61 2 ครั้ง 19 1 ครั้ง', wrong, wrong)
    assert result.status != 'preview' and not h.sheets.entries


def test_reused_amount_feedback_identifies_the_exact_proposal_row_for_repair():
    h = harness(('Metro',))
    def repair(body):
        feedback = json.loads([r for r in body['input'] if r.get('type') == 'function_call_output'][-1]['output'])
        assert feedback['repair_feedback']['invalid_fields'] == ['entries[2].amount']
        assert feedback['repair_feedback']['required_inputs'] == ['unused_original_amount_occurrence']
        return propose([('Metro', 61), ('Metro', 61), ('Metro', 19)])(body)
    result = h.send('Metro 61, 61, 19', propose([('Metro', 61)] * 3), repair)
    assert result.status == 'preview', result
    assert [e['amount'] for e in payments(h)] == ['61', '61', '19']
    assert len(h.port.requests) == 2 and not h.sheets.entries


@pytest.mark.parametrize('ordinal', ['2', '๒', 'สอง'])
def test_ordinal_script_does_not_change_combined_patch_money_roles(ordinal):
    h = harness(('รถรับส่ง',))
    prepare(h, 'รถรับส่ง 34 กับ 34 บาท', [entry(item='รถรับส่ง', amount=34)] * 2)
    text = f'ทั้งคู่คือเมื่อวาน แก้รายการ {ordinal} เป็น 31'
    result = h.send(text, patch({0: [('date_text', 'เมื่อวาน', 'เมื่อวาน')],
        1: [('date_text', 'เมื่อวาน', 'เมื่อวาน'), ('amount', 31, text)]}))
    assert result.status == 'preview', result
    assert [e['amount'] for e in payments(h)] == ['34', '31']
    assert not h.sheets.entries


@pytest.mark.parametrize('amount', ['5', '5.50', '1,005'])
@pytest.mark.parametrize('label', ['ยอด ', 'ยอดเงิน '])
def test_monetary_introducers_do_not_turn_small_amounts_into_row_ordinals(label, amount):
    from decimal import Decimal
    h = harness(('Metro',))
    result = prepare(h, f'Metro {label}{amount} บาท',
        [entry(item='Metro', amount=str(Decimal(amount.replace(',', ''))))])
    assert result.status == 'preview', result
    assert len(h.port.requests) == 1 and not h.sheets.entries
