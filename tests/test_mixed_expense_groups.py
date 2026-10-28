"""Synthetic mixed-label lists: appending a sibling preserves each price group."""
from copy import deepcopy
import json

import pytest

from test_catalog_names_v09 import harness, entry, prepare
from test_expense_list_continuations import payments, propose
from test_raw_history import context
from test_history_first import response


@pytest.mark.parametrize('names', [('Metro', 'รถรับส่ง'), ('Polar Ride', 'Tram')])
@pytest.mark.parametrize('join', [' แล้วก็ ', ' และ ', ' กับ ', ', ', '; ', ' ', '\n'])
@pytest.mark.parametrize('separator', [', ', ' บาท ', ' บาท และ '])
def test_mixed_names_do_not_steal_the_last_price_from_a_repeated_group(names, join, separator):
    h = harness(names)
    if join == '\n': join = '\nเมื่อวาน '
    text = 'เมื่อวาน ' + names[0] + ' ' + separator.join(['61', '61', '19']) + join + names[1] + ' 27 บาท'
    expected = [(names[0], 61), (names[0], 61), (names[0], 19), (names[1], 27)]
    result = prepare(h, text, [entry(item=name, amount=amount) for name, amount in expected])
    assert result.status == 'preview', result
    rows = payments(h)
    assert [(e['item'], e['amount']) for e in rows] == [(name, str(amount)) for name, amount in expected]
    assert {e['date'] for e in rows} == {'2026-10-15'}
    assert len(h.port.requests) == 1 and not h.sheets.entries
    assert [text[slice(*e['amount_occurrence']['span'])] for e in rows[:3]] == ['61', '61', '19']
    event = h.event(result.data['confirm_command'])
    assert h.service.handle(event).status == 'recorded' and len(h.sheets.entries) == 4
    assert h.service.handle(deepcopy(event)).status == 'recorded' and len(h.sheets.entries) == 4


@pytest.mark.parametrize('text', ['Metro 61 บาท 61 บาท และ 19 บาท', 'Metro 61บาท61บาท19บาท',
                                 'Metro 61 บาท, 61 บาท, 19 บาท'])
def test_currency_marks_can_separate_repeated_payments_without_a_conjunction(text):
    h = harness(('Metro',))
    result = prepare(h, text, [entry(item='Metro', amount=v) for v in [61, 61, 19]])
    assert result.status == 'preview', result
    assert [e['amount'] for e in payments(h)] == ['61', '61', '19']
    assert len(h.port.requests) == 1 and not h.sheets.entries


def test_three_named_groups_preserve_all_five_original_price_occurrences():
    h = harness(('Metro', 'รถรับส่ง', 'Tram'))
    expected = [('Metro', 61), ('Metro', 19), ('รถรับส่ง', 27), ('รถรับส่ง', 31), ('Tram', 13)]
    result = prepare(h, 'Metro 61, 19 แล้วก็ รถรับส่ง 27, 31 แล้วก็ Tram 13',
        [entry(item=name, amount=amount) for name, amount in expected])
    assert result.status == 'preview', result
    assert [(e['item'], e['amount']) for e in payments(h)] == [(name, str(amount)) for name, amount in expected]
    assert not h.sheets.entries


@pytest.mark.parametrize('values', [[61, 61, 27, 19], [61, 61, 19, 19], [61, 19, 19, 27]])
def test_mixed_group_validation_cannot_reassign_a_siblings_price(values):
    h = harness(('Metro', 'รถรับส่ง'))
    result = prepare(h, 'Metro 61, 61, 19 แล้วก็ รถรับส่ง 27',
        [entry(item=name, amount=v) for name, v in zip(['Metro'] * 3 + ['รถรับส่ง'], values)])
    assert result.status != 'preview' and not h.sheets.entries


def test_followup_can_complete_a_failed_batch_without_repeating_the_other_name():
    h = harness(('Metro', 'รถรับส่ง'))
    wrong = propose([('Metro', 61), ('Metro', 61), ('Metro', 61), ('รถรับส่ง', 27)])
    failed = h.send('เมื่อวาน Metro 61, 61, 19 แล้วก็ รถรับส่ง 27', wrong, wrong)
    source = h.service._current['source_ref']
    assert failed.status != 'preview' and not h.state.drafts
    assert source in failed.data['tool_result']['failed_expense_sources']
    def resume(body):
        current = context(body)['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref': None, 'draft_version': None,
            'entries': [entry(item=name, amount=amount, date_text='เมื่อวาน', source_refs=[source, current])
                for name, amount in [('Metro', 61), ('Metro', 61), ('Metro', 19), ('รถรับส่ง', 27)]]})
    result = h.send('เมื่อวาน Metro 61 บาท 61 บาท และ 19 บาท', resume)
    assert result.status == 'preview', result
    assert [(e['item'], e['amount']) for e in payments(h)] == [('Metro','61'),('Metro','61'),('Metro','19'),('รถรับส่ง','27')]
    assert {e['date'] for e in payments(h)} == {'2026-10-15'}
    assert not h.sheets.entries


def test_failed_batch_permission_does_not_admit_unrelated_historical_siblings():
    h = harness(('Metro', 'Tram'))
    wrong = propose([('Tram', 29)])
    h.send('Tram 27', wrong, wrong)
    source = h.service._current['source_ref']
    def unrelated(body):
        current = context(body)['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref': None, 'draft_version': None,
            'entries': [entry(item='Metro', amount=61, source_refs=[current]),
                        entry(item='Tram', amount=27, source_refs=[source, current])]})
    result = h.send('Metro 61', unrelated, unrelated)
    assert result.status != 'preview' and not h.sheets.entries
    assert h.service.trace[-1]['result']['error'] == 'item_source_conflicts_with_current_expense'


def test_a_successful_old_batch_cannot_be_reused_as_failed_work():
    h = harness(('Metro', 'Tram'))
    review = prepare(h, 'Metro 61 กับ Tram 27', [entry(item='Metro', amount=61), entry(item='Tram', amount=27)])
    source = h.service._current['source_ref']
    assert h.send(review.data['confirm_command']).status == 'recorded'
    saved = deepcopy(h.sheets.entries)
    def unrelated(body):
        current = context(body)['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref': None, 'draft_version': None,
            'entries': [entry(item='Metro', amount=61, source_refs=[current]),
                        entry(item='Tram', amount=27, source_refs=[source, current])]})
    result = h.send('Metro 61', unrelated, unrelated)
    assert result.status != 'preview' and h.sheets.entries == saved


def test_signed_cold_factory_can_resume_a_delivered_failed_group_and_save_once():
    from fastapi.testclient import TestClient
    from test_production_history import Hosted
    api = Hosted()
    for i,name in enumerate(['Metro','รถรับส่ง'],25):
        for col,value in enumerate([name,'Transportation',''],1):api.google.cells[('Database',i,col)] = value
    with TestClient(api.app(raw_history_enabled=True)) as client:
        wrong = propose([('Metro',61),('Metro',61),('Metro',61),('รถรับส่ง',27)])
        failed,event = api.send_text(client,'Metro 61, 61, 19 แล้วก็ รถรับส่ง 27',wrong,wrong)
    assert failed.status != 'preview' and not api.google.sheet_writes
    source = failed.data['tool_result']['failed_expense_sources'][0]
    def resume(body):
        current = context(body)['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref':None,'draft_version':None,'entries':[
            entry(item=name,amount=amount,source_refs=[source,current])
            for name,amount in [('Metro',61),('Metro',61),('Metro',19),('รถรับส่ง',27)]]})
    with TestClient(api.app(raw_history_enabled=True)) as client:
        reviewed,_ = api.send_text(client,'Metro 61 บาท 61 บาท และ 19 บาท',resume)
    assert reviewed.status == 'preview', reviewed
    assert len(reviewed.data['tool_result']['entries']) == 4 and not api.google.sheet_writes
    with TestClient(api.app(raw_history_enabled=True)) as client:
        saved,confirmation = api.send_text(client,reviewed.data['confirm_command'])
    assert saved.status == 'recorded' and api.google.sheet_writes == 1
    with TestClient(api.app(raw_history_enabled=True)) as client:
        assert api.send(client,deepcopy(confirmation)).status_code == 200
    assert api.google.sheet_writes == 1


def test_prior_runtime_failure_receipts_can_resume_without_new_metadata(monkeypatch):
    monkeypatch.setattr('line_assistant.expense_evidence.failed_request_sources', lambda h,call,output:output)
    h = harness(('Metro','รถรับส่ง'))
    wrong = propose([('Metro',61),('Metro',61),('Metro',61),('รถรับส่ง',27)])
    initial = h.send('เมื่อวาน Metro 61, 61, 19 แล้วก็ รถรับส่ง 27',wrong,wrong)
    source = h.service._current['source_ref']
    assert 'failed_expense_sources' not in initial.data['tool_result']
    def resume(body):
        current = context(body)['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref':None,'draft_version':None,'entries':[
            entry(item=name,amount=value,date_text='เมื่อวาน',source_refs=[source,current])
            for name,value in [('Metro',61),('Metro',61),('Metro',19),('รถรับส่ง',27)]]})
    result = h.send('เมื่อวาน Metro 61 บาท 61 บาท และ 19 บาท',resume)
    assert result.status == 'preview',result
    assert len(payments(h)) == 4 and not h.sheets.entries
