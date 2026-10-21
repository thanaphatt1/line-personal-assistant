"""Schema-defined nonfinancial/financial datasets and full-input operations."""
from copy import deepcopy
from pathlib import Path
import json
import pytest

from line_assistant.tables import Column, Table, Registry, MemoryTableStore, Query, TableError, normalize, query
from line_assistant.table_storage import parse_registry

FIXTURE = Path(__file__).parent / 'fixtures/table_registry.json'


def registry():
    tables = [r.table for r in parse_registry(FIXTURE.read_text())]
    return Registry(tables, {t.id: MemoryTableStore('mock-' + t.id) for t in tables})


def seed(reg, name, values):
    store = reg.stores[name]
    store.records = {f'row{i}': {'id': f'row{i}', 'version': f'v{i}', 'values': deepcopy(value)} for i, value in enumerate(values)}


def run(reg, name='people', **kwargs):
    return query(reg, Query(table_id=name, source_refs=['mock-source'], **kwargs))


def test_people_have_no_implicit_money_or_date_columns():
    reg = registry()
    seed(reg, 'people', [{'name': 'คุณจำลอง ก', 'phone': '00000123', 'status': 'pending'},
                         {'name': 'คุณจำลอง ข', 'phone': None, 'status': 'called'}])
    result = run(reg, filters=[{'field': 'status', 'op': 'eq', 'values': ['pending']}])
    assert result['count'] == 1 and result['rows'][0]['phone'] == '00000123'
    assert not {'date', 'amount'} & set(result['rows'][0])


def test_an_entirely_new_schema_uses_same_engine_without_registration_code():
    reg = registry()
    table = Table(id='books', label='หนังสือ', description='หนังสือที่กำลังอ่าน', columns=[
        Column(name='title', label='ชื่อเรื่อง', type='text', required=True),
        Column(name='pages', label='จำนวนหน้า', type='integer')])
    store = MemoryTableStore('mock-books', [{'id': 'b1', 'version': '1', 'values': {'title': 'Example', 'pages': 123}}])
    reg = Registry([table], {'books': store})
    assert run(reg, 'books', filters=[{'field': 'pages', 'op': 'gt', 'values': [100]}])['count'] == 1


@pytest.mark.parametrize('op,value,expected', [('eq', 'pending', 1), ('ne', 'pending', 1), ('in', 'pending', 1)])
def test_enum_filters(op, value, expected):
    reg = registry()
    seed(reg, 'people', [{'name': 'A', 'phone': None, 'status': 'pending'}, {'name': 'B', 'phone': None, 'status': 'called'}])
    assert run(reg, filters=[{'field': 'status', 'op': op, 'values': [value]}])['count'] == expected


def test_sort_group_and_aggregates_use_all_records_before_limit_and_exact_decimal():
    reg = registry()
    seed(reg, 'family_income', [{'date': '2026-12-01', 'amount': str(i), 'customer': None, 'note': None} for i in range(1, 78)])
    result = run(reg, 'family_income', order_by=[{'field': 'amount', 'descending': True}], limit=2)
    assert [v['amount'] for v in result['rows']] == ['77', '76'] and result['count'] == 77
    assert result['truncated']
    result = run(reg, 'family_income', group_by=['date'], aggregates=[{'op': 'sum', 'field': 'amount', 'alias': 'total'}, {'op': 'count', 'alias': 'payments'}], limit=1)
    assert result['rows'] == [{'date': '2026-12-01', 'total': '3003', 'payments': 77}]
    seed(reg, 'family_income', [{'date': '2026-12-01', 'amount': v, 'customer': None, 'note': None} for v in ['0.10', '0.20']])
    assert run(reg, 'family_income', aggregates=[{'op': 'sum', 'field': 'amount', 'alias': 'total'}])['rows'] == [{'total': '0.30'}]


@pytest.mark.parametrize('op,expected', [('sum', '30'), ('avg', '15'), ('min', '10'), ('max', '20'), ('count', 2)])
def test_numeric_aggregates(op, expected):
    reg = registry()
    seed(reg, 'family_income', [{'date': '2026-12-01', 'amount': v, 'customer': None, 'note': None} for v in ['10', '20']])
    assert run(reg, 'family_income', aggregates=[{'op': op, 'field': 'amount', 'alias': 'value'}])['rows'][0]['value'] == expected


def test_null_counts_and_ordering_both_directions():
    reg = registry()
    seed(reg, 'people', [{'name': 'C', 'phone': None, 'status': 'pending'}, {'name': 'B', 'phone': '02', 'status': 'called'}, {'name': 'A', 'phone': '01', 'status': 'pending'}])
    for descending in (True, False):
        assert run(reg, order_by=[{'field': 'phone', 'descending': descending}])['rows'][-1]['phone'] is None
    result = run(reg, aggregates=[{'op': 'count', 'field': 'phone', 'alias': 'with_phone'}])
    assert result['rows'] == [{'with_phone': 2}]
    assert run(reg, filters=[{'field': 'phone', 'op': 'is_null'}])['count'] == 1


def test_empty_is_verified_zero_count_sum_and_null_avg():
    result = run(registry(), 'family_income', aggregates=[{'op': 'sum', 'field': 'amount', 'alias': 'total'}, {'op': 'avg', 'field': 'amount', 'alias': 'average'}, {'op': 'count', 'alias': 'records'}])
    assert result['complete'] and result['rows'] == [{'total': '0', 'average': None, 'records': 0}]


@pytest.mark.parametrize('kwargs,code', [
    ({'filters': [{'field': 'amount', 'op': 'eq', 'values': ['10']}]}, 'unknown_field'),
    ({'aggregates': [{'op': 'sum', 'field': 'name', 'alias': 'total'}]}, 'numeric_field_required'),
    ({'order_by': [{'field': 'imagined'}]}, 'unknown_order_field'),
    ({'group_by': ['name']}, 'aggregate_required'),
    ({'aggregates': [{'op': 'count', 'alias': 'name'}]}, 'aggregate_alias'),
    ({'filters': [{'field': 'status', 'op': 'contains', 'values': ['pending']}]}, 'invalid_filter'),
])
def test_invalid_plans_fail_before_provider_scan(kwargs, code, monkeypatch):
    reg = registry()
    monkeypatch.setattr(reg.stores['people'], 'scan', lambda table: pytest.fail('invalid plan scanned'))
    with pytest.raises(TableError, match=code): run(reg, **kwargs)


@pytest.mark.parametrize('kind,value', [('decimal', 'NaN'), ('decimal', 'Infinity'), ('decimal','1e9999999'), ('decimal', True), ('integer', True), ('integer', '3'), ('boolean', 'true'), ('date', '2026-02-30'), ('text', 1)])
def test_invalid_types_are_rejected(kind, value):
    with pytest.raises(TableError): normalize(Column(name='field', label='Field', type=kind), value)


def test_duplicate_records_and_missing_required_data_fail_complete_read():
    reg = registry()
    seed(reg, 'people', [{'name': None, 'phone': None, 'status': 'pending'}])
    with pytest.raises(TableError, match='required_field'): run(reg)
    seed(reg, 'people', [{'name': 'A', 'phone': None, 'status': 'pending'}])
    row = reg.stores['people'].records['row0']
    reg.stores['people'].scan = lambda t: [deepcopy(row), deepcopy(row)]
    with pytest.raises(TableError, match='invalid_snapshot'): run(reg)


def test_calendar_and_table_semijoins_keep_union_without_double_counting():
    reg = registry()
    seed(reg, 'family_income', [{'date': '2026-12-01', 'amount': '100', 'customer': 'c1', 'note': None}, {'date': '2026-12-02', 'amount': '200', 'customer': 'c2', 'note': None}])
    p = Query(table_id='family_income', source_refs=['s'], date_field='date', calendar_query_refs=['read1'], matching_field='customer', matching_query_ref='people_read', source_field='id')
    result = query(reg, p, joined_dates={'2026-12-01', '2026-12-02'}, joined_values=['c1', 'c1'])
    assert result['count'] == 1 and result['rows'][0]['amount'] == '100'


def test_registry_rejects_duplicate_ids_unknown_relationships_and_unreviewed_writers():
    raw = json.loads(FIXTURE.read_text())
    for mutate in [lambda r: r.append(deepcopy(r[0])),
                   lambda r: r[1]['table']['columns'][2].update(references='unknown'),
                   lambda r: r[0]['storage'].update(single_writer_reviewed=False),
                   lambda r: r[0]['table']['columns'][0].update(default=42),
                   lambda r: r[1]['storage'].update(collection_path='assistant_pilot_states')]:
        value = deepcopy(raw); mutate(value)
        with pytest.raises(ValueError, match='Invalid table registry'): parse_registry(json.dumps(value))


def test_ambiguous_column_labels_fail_and_null_join_keys_do_not_match():
    reg=registry()
    reg.tables['people'].columns[0].label=reg.tables['people'].columns[1].label='ซ้ำ'
    with pytest.raises(TableError,match='ambiguous_field'):run(reg,select=['ซ้ำ'])
    seed(reg,'family_income',[{'date':'2026-10-16','amount':'100','customer':None,'note':None}])
    p=Query(table_id='family_income',matching_query_ref='source',matching_field='customer',source_field='phone',source_refs=['s'])
    assert query(reg,p,joined_values=[None])['count']==0
    one=Table(id='one',label='One',description='One field',columns=[Column(name='name',label='Name',type='text')])
    with pytest.raises(TableError,match='unknown_field'):one.column(None)
