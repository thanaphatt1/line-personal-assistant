"""Arbitrary predicates use SQL; fixtures and model choices are synthetic."""
from copy import deepcopy
import json
import socket

import pytest
from fastapi.testclient import TestClient

from test_calendar_semantic_queries import SemanticHosted
from test_calendar_text_confirmation_v13 import turn
from test_expense_query_operations import seed, expense
from test_read_queries import plan
from test_relational_queries import sql
from test_calendar_mvp import response
from test_response_realization import generated
from test_query_analysis_loop import results


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k): raise AssertionError('Synthetic SQL filtering only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def sample():
    api = SemanticHosted()
    for index, row in enumerate([
        ['Tablet Aurora', 'Gadget', 'Devices'], ['Rail', 'Transportation', 'Train'],
        ['Metro', 'Transportation', 'Train'], ['Tea', 'Food', 'Drink'],
    ], 3):
        for col, value in enumerate(row, 1): api.google.cells[('Database', index, col)] = value
    seed(api, [('2026-09-01', name, amount) for name, amount in [
        ('Tablet Aurora', 1800), ('Rail', 45), ('Rail', 45), ('Metro', 12), ('Tea', 23)]])
    return api


def aggregate(predicate):
    return sql(lambda rs: f"SELECT COALESCE(SUM(amount),'0') AS total_baht,COUNT(*) AS payments "
               f"FROM {rs[0]['sql_table']} WHERE {predicate}")


def replace_entries(api, rows):
    for key in list(api.google.cells):
        if key[0] == 'All Expenses' and key[1] > 1: del api.google.cells[key]
    seed(api, rows)


@pytest.mark.parametrize('predicate, total, count', [
    ("lower(item) != lower('Tablet Aurora')", '125.00', 4),
    ("item NOT IN ('Tablet Aurora','Rail')", '35.00', 2),
    ("item NOT LIKE '%Aurora%' AND (item='Rail' OR amount<'20')", '102.00', 3),
    ("NOT (amount BETWEEN '100' AND '2000')", '125.00', 4),
    ("category != 'Gadget' AND subcategory='Train'", '102.00', 3),
    ("amount > (SELECT MIN(amount) FROM {table}) AND item NOT LIKE '%Aurora%'", '113.00', 3),
    ("item NOT IN ('Tablet Aurora','Rail','Metro','Tea')", '0', 0),
])
def test_predicates_compose_without_new_operation_handlers(predicate, total, count):
    api = sample()
    query = sql(lambda rs: f"SELECT COALESCE(SUM(amount),'0') AS total_baht,COUNT(*) AS payments "
                f"FROM {rs[0]['sql_table']} WHERE {predicate.format(table=rs[0]['sql_table'])}")
    result, event = turn(api, 'เดือน ก.ย. รวมตามเงื่อนไขที่ขอ',
                         expense(continue_analysis=True, filter=None), query)
    assert result.data['tool_result']['rows'] == [{'total_baht': total, 'payments': count}]
    assert result.data['tool_result']['complete']
    calls = len(api.model_bodies)
    with TestClient(api.app()) as client: assert api.send(client, deepcopy(event)).status_code == 200
    assert len(api.model_bodies) == calls
    assert not api.google.sheet_writes and not api.calendar.posts and not api.snapshot()['actions']


def test_excluded_list_window_totals_use_all_rows_before_display_limit_and_keep_duplicate_purchases():
    api = sample()
    seed(api, [('2026-09-01', 'Rail', 1)] * 25 + [('2026-09-01', 'Tablet Aurora', 1800)])
    query = sql(lambda rs: f"WITH selected AS (SELECT * FROM {rs[0]['sql_table']} "
                "WHERE item != 'Tablet Aurora') SELECT item,amount,SUM(amount) OVER() AS total_baht,"
                "COUNT(*) OVER() AS payments FROM selected ORDER BY amount COLLATE decimal DESC")
    result, _ = turn(api, 'เดือน ก.ย. รายการไม่รวม Tablet Aurora พร้อมยอดรวม',
                     expense(continue_analysis=True, limit=1), query)
    output = result.data['tool_result']
    assert output['count'] == 25 and output['shown'] == 20
    assert all(r == {'item': 'Rail', 'amount': '1.00', 'total_baht': '25.00', 'payments': 25} for r in output['rows'])


def test_sql_scope_survives_cold_followup_correction_and_general_question_clears_it():
    api = sample()
    first, _ = turn(api, 'เดือน ก.ย. ไม่รวม Tablet Aurora', expense(continue_analysis=True),
                    aggregate("item != 'Tablet Aurora'"))
    assert first.data['read_query']['analysis']['sql'].endswith("WHERE item != 'Tablet Aurora'")
    def previous(body):
        c = json.loads(body['input'][0]['content'])
        assert c['previous_query']['start'] == '2026-09-01'
        assert "!= 'Tablet Aurora'" in c['previous_query']['analysis']['sql']
        assert c['relations_catalog'] == []  # previous data/refs never become fresh evidence
        return expense(start=None, end=None, date_mode='previous', continue_analysis=True)(body)
    second, _ = turn(api, 'อันนี้ต้องยกเว้น Tablet Aurora', previous, aggregate("item != 'Tablet Aurora'"))
    assert second.data['tool_result']['rows'] == [{'total_baht': '125.00', 'payments': 4}]
    def reset(body):
        c = json.loads(body['input'][0]['content'])
        assert c['previous_query']['analysis']
        return expense()(body)
    all_rows, _ = turn(api, 'เดือน ก.ย. ใช้อะไรไปบ้าง', reset)
    assert all_rows.data['tool_result']['expense'] == '1925.00'
    assert all_rows.data['tool_result']['count'] == 5
    assert all_rows.data['read_query']['filter'] is None and 'analysis' not in all_rows.data['read_query']


def test_derived_sql_keeps_original_period_for_next_turn_without_reusing_derived_rows():
    api = sample()
    first = sql(lambda rs: f"SELECT * FROM {rs[0]['sql_table']} WHERE item != 'Tablet Aurora'", continue_analysis=True)
    last = sql(lambda rs: f"SELECT SUM(amount) AS total_baht FROM {next(r['sql_table'] for r in rs if r['domain']=='relation')}")
    result, _ = turn(api, 'เดือน ก.ย. รวมโดยยกเว้น Tablet Aurora', expense(continue_analysis=True), first, last)
    assert result.data['tool_result']['rows'] == [{'total_baht': '125.00'}]
    assert result.data['read_query']['start'] == '2026-09-01'
    assert 'Tablet Aurora' in str(result.data['read_query']['analysis']['inputs'])


def test_multiple_period_sql_does_not_invent_one_previous_period():
    api = sample()
    query = sql(lambda rs: f"SELECT SUM(amount) AS total FROM {rs[0]['sql_table']} UNION ALL "
                f"SELECT SUM(amount) AS total FROM {rs[1]['sql_table']}")
    result, _ = turn(api, 'เดือน ก.ย. กับ ต.ค. รวมเท่าไหร่', expense(continue_analysis=True),
                     expense('2026-10-01', '2026-10-31', continue_analysis=True), query)
    assert 'read_query' not in result.data


@pytest.mark.parametrize('field', ['canonical_item', 'category', 'subcategory'])
def test_unknown_mapping_cannot_silently_disappear_through_sql_null_negation(field):
    api = sample()
    replace_entries(api, [('2026-09-01', 'Unmapped snack', 7)])
    result, _ = turn(api, 'เดือน ก.ย. ไม่รวมบางหมวด', expense(continue_analysis=True),
                     aggregate(f"{field} != 'Gadget'"),
                     # The model cannot repair a genuinely missing mapping by guessing it.
                     response(text='ยังยืนยันหมวดของข้อมูลได้ไม่ครบครับ'))
    assert not result.data['tool_result']['ok']
    assert result.data['tool_result']['error_code'] == 'relation_incomplete_field'
    assert 'rows' not in result.data['tool_result']


def test_raw_item_exclusion_keeps_other_unmapped_names_without_requiring_category_guesses():
    api = sample()
    replace_entries(api, [('2026-09-01', 'Unmapped snack', 7), ('2026-09-01', 'Tablet Aurora', 1800)])
    result, _ = turn(api, 'เดือน ก.ย. ไม่รวม Tablet Aurora', expense(continue_analysis=True),
                     aggregate("lower(item) NOT LIKE '%aurora%'"))
    assert result.data['tool_result']['rows'] == [{'total_baht': '7.00', 'payments': 1}]


def test_reply_sees_executed_sql_and_legacy_inclusion_is_a_complete_required_fact():
    api = sample()
    result, _ = turn(api, 'เดือน ก.ย. ไม่รวม Tablet Aurora', expense(continue_analysis=True),
                     aggregate("item != 'Tablet Aurora'"))
    payload = json.loads(api.voice_bodies[-1]['input'][0]['content'])
    assert payload['read_evidence']['sql'].endswith("WHERE item != 'Tablet Aurora'")
    assert 'workbook' not in str(payload['read_evidence']) and 'calendar_read_ids_hash' not in str(payload['read_evidence'])
    assert result.data['response_realization']['status'] == 'llm'
    legacy, _ = turn(api, 'เดือน ก.ย. เฉพาะ Tablet Aurora',
                     plan(start='2026-09-01', end='2026-09-30', kind='item', value='Tablet Aurora'))
    assert legacy.data['query_facts']['selection'] == 'เฉพาะ Tablet Aurora'
    view = json.loads(api.voice_bodies[-1]['input'][0]['content'])['answer_facts']
    assert any(view['facts'][k] == 'เฉพาะ Tablet Aurora' for k in view['required_facts'])


@pytest.mark.parametrize('output_date, accepts', [("date", True), ("'2026-09-02'", False)])
def test_sql_list_date_can_be_omitted_only_when_equal_to_verified_same_day_scope(output_date, accepts):
    api = sample()
    def natural(body):
        payload = json.loads(body['input'][0]['content'])
        view = payload['answer_facts']
        return generated(body, {'blocks': [
            {'kind': 'text', 'template': ' '.join('{{'+k+'}}' for k in view['facts'])},
            {'kind': 'rows', 'record_refs': [r['id'] for r in view['records']], 'heading': '',
             'template': '{{c2}} — {{c3}} บาท'}]})
    api.voice = natural
    query = sql(lambda rs: f"SELECT {output_date} AS date,item,amount AS amount_baht "
                f"FROM {rs[0]['sql_table']} WHERE item != 'Tablet Aurora'")
    result, _ = turn(api, 'วันที่ 1 ก.ย. ไม่รวม Tablet Aurora',
                     expense('2026-09-01', '2026-09-01', continue_analysis=True), query)
    if accepts:
        assert result.data['response_realization']['status'] == 'llm'
        assert result.message.count('1 ก.ย. 2026') == 1
    else:
        assert result.data['response_realization']['validation_code'] == 'answer_cell_coverage'


def test_derived_sql_reply_keeps_original_period_and_executed_source_evidence():
    api = sample()
    first = sql(lambda rs: f"SELECT date,item,amount FROM {rs[0]['sql_table']} WHERE item != 'Tablet Aurora'", continue_analysis=True)
    last = sql(lambda rs: f"SELECT SUM(amount) AS total_baht FROM {next(r['sql_table'] for r in rs if r['domain']=='relation')}")
    result, _ = turn(api, 'เดือน ก.ย. รวมยกเว้น Tablet Aurora', expense(continue_analysis=True), first, last)
    payload = json.loads(api.voice_bodies[-1]['input'][0]['content'])
    assert any('ก.ย. 2026' in v for v in payload['answer_facts']['facts'].values())
    assert 'Tablet Aurora' in str(payload['read_evidence']['source_scopes'])
    assert result.data['response_realization']['status'] == 'llm'


def test_finishing_only_the_sql_result_retains_its_predicate_and_period_for_followup():
    api = sample()
    query = sql(lambda rs: f"SELECT item,amount FROM {rs[0]['sql_table']} WHERE item != 'Tablet Aurora'", continue_analysis=True)
    def finish_sql(body):
        ref = next(v['query_ref'] for v in results(body) if v.get('relational_query'))
        context = json.loads(body['input'][0]['content'])
        return response('finish_query_analysis', {'query_refs': [ref], 'operation': 'report',
                        'source_refs': [context['current_source_ref']]})
    result, _ = turn(api, 'เดือน ก.ย. ไม่รวม Tablet Aurora', expense(continue_analysis=True), query, finish_sql)
    assert "!= 'Tablet Aurora'" in result.data['read_query']['analysis']['sql']
    assert result.data['read_query']['start'] == '2026-09-01'
    def previous(body):
        context = json.loads(body['input'][0]['content'])
        assert context['previous_query']['analysis']['sql'] == result.data['read_query']['analysis']['sql']
        assert context['relations_catalog'] == []
        return expense(start=None, end=None, date_mode='previous')(body)
    next_result, _ = turn(api, 'แล้วทั้งหมดล่ะ', previous)
    assert next_result.data['tool_result']['expense'] == '1925.00'
