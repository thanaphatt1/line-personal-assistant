"""Only metadata-declared empty annotations can disappear from presentation."""
from types import SimpleNamespace

import pytest

from line_assistant import answer_composition as composition
from line_assistant.relational_metadata import result_columns, display_fields
from line_assistant.relational import message
from test_answer_composition import document, paragraph, rows
from test_calendar_semantic_queries import SemanticHosted
from test_calendar_text_confirmation_v13 import turn
from test_expense_query_operations import seed, expense
from test_relational_queries import sql


def relation(optional=True):
    return {'sql_table':'q', 'columns':[{'name':'item','type':'text'},
        {'name':'note','type':'text',**({'omit_empty':True} if optional else {})},
        {'name':'amount','type':'decimal','unit':'บาท'}]}


@pytest.mark.parametrize('query', ['SELECT item,note AS memo,amount FROM q',
    'WITH x AS (SELECT item,note AS memo,amount FROM q) SELECT item,memo,amount FROM x'])
def test_optional_display_metadata_follows_verified_columns_through_aliases(query):
    cols = result_columns([relation()], query, ['item','memo','amount'], [{'item':'A','memo':'','amount':'15'}])
    assert cols[1]['omit_empty'] is True and cols[2]['unit'] == 'บาท'
    assert display_fields({'item':'A','memo':'','amount':'15'},cols) == ['item','amount']


@pytest.mark.parametrize('expression', ['COUNT(note)', 'MAX(note)', 'SUM(note)', 'CAST(note AS TEXT)',
                                      'LAG(note) OVER (ORDER BY item)'])
def test_computed_results_do_not_inherit_optional_annotation_omission(expression):
    cols = result_columns([relation()], f'SELECT {expression} AS memo FROM q', ['memo'], [{'memo':None}])
    assert not cols[0].get('omit_empty')


@pytest.mark.parametrize('value', [0, False, '0', 'false'])
def test_zero_and_false_are_values_even_for_optional_columns(value):
    assert display_fields({'item':'A','note':value},[{'name':'note','omit_empty':True}]) == ['item','note']


def test_column_names_alone_cannot_hide_blank_generic_data():
    cols = result_columns([relation(False)], 'SELECT note FROM q', ['note'], [{'note':''}])
    assert not cols[0].get('omit_empty')
    assert display_fields({'item':'A','note':''},cols) == ['item','note']


@pytest.mark.parametrize('blank', ['', '   ', None])
def test_blank_annotations_are_suppressed_without_dropping_nonempty_notes_or_row_identity(blank):
    cols = [{'name':'item'}, {'name':'note','omit_empty':True}, {'name':'amount','unit':'บาท'}]
    assert display_fields({'item':'A','note':blank,'amount':'15'},cols) == ['item','amount']
    assert display_fields({'note':blank},cols) == ['note']
    assert display_fields({'item':'A','note':'ขากลับ','amount':'15'},cols) == ['item','note','amount']
    assert 'note:' not in message([{'item':'A','note':blank,'amount':'15'}],1,cols)
    assert 'ขากลับ' in message([{'item':'A','note':'ขากลับ','amount':'15'}],1,cols)


def test_actual_expense_sql_keeps_raw_notes_and_stable_cell_ids_but_omits_blank_labels():
    api = SemanticHosted()
    seed(api,[('2026-09-01','A',15),('2026-09-01','B',16)])
    api.google.cells[('All Expenses',3,4)] = 'ขากลับ'
    result,_ = turn(api,'ขอดูรายการ',expense(continue_analysis=True),
        sql(lambda rs:f"SELECT item,note,amount FROM {rs[0]['sql_table']} ORDER BY item"))
    output = result.data['tool_result']
    assert output['rows'] == [{'item':'A','note':'','amount':'15.00'}, {'item':'B','note':'ขากลับ','amount':'16.00'}]
    view = composition.for_result(SimpleNamespace(_current={'at':'2026-10-16T03:00:00+00:00'}),result)
    assert list(view['records'][0]['fields']) == ['c1','c3']
    assert list(view['records'][1]['fields']) == ['c1','c2','c3']
    answer = composition.render(document(paragraph(' '.join('{{'+k+'}}' for k in view['facts'])),
        rows(['r1'],'{{c1}} — {{c3}}'), rows(['r2'],'{{c1}} — {{c3}} (หมายเหตุ: {{c2}})')),view)
    assert 'A 15 บาท' in answer and 'B 16 บาท (หมายเหตุ: ขากลับ)' in answer
    assert '(หมายเหตุ: )' not in answer and not api.google.sheet_writes
