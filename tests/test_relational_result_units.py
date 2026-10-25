"""Result names cannot create units or round unrelated structured data."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from line_assistant import answer_composition, relational


def host(unit=None):
    columns=[{'name':'amount','type':'decimal'}, {'name':'label','type':'text'}]
    if unit:columns[0]['unit']=unit
    source={'ok':True,'complete':True,'domain':'relation','count':2,'columns':columns,
            'scope':{'domain':'relation'},'analysis_records':[
                {'amount':'12.3456','label':'000187'}, {'amount':'20.0000','label':'000188'}]}
    return SimpleNamespace(_query_reads={'source':source},_active_event_id='event',
                           _current={'source_ref':'owner'},_owner_sources=lambda refs:[])


def query(h,sql,ref='source'):
    table=relational.materialize(h,ref)['sql_table']
    result=relational.execute(h,{'query_refs':[ref],'sql':sql.format(table=table),
        'source_refs':['owner'],'continue_analysis':False})
    assert result['ok'],result
    return result


@pytest.mark.parametrize('alias',['total','amount','total_baht'])
def test_a_count_is_never_money_even_with_a_money_sounding_alias(alias):
    output=query(host('บาท'),f'SELECT COUNT(*) AS {alias} FROM {{table}}')
    assert output['rows']==[{alias:2}] and not output['columns'][0].get('unit')
    assert 'บาท' not in output['message']
    view=answer_composition.View();answer_composition.add_read(view,output,datetime.now(timezone.utc))
    assert view.records[0]['fields']['c1']['display']=='2'


@pytest.mark.parametrize('sql',[
    'SELECT amount AS total FROM {table}',
    'SELECT SUM(amount) AS total FROM {table}',
    'WITH x AS (SELECT SUM(amount) AS subtotal FROM {table}) SELECT subtotal AS total FROM x',
    'SELECT SUM(amount) OVER () AS total FROM {table}',
    "SELECT COALESCE(SUM(amount),'0') AS total FROM {table}",
    "SELECT COALESCE(SUM(amount),'0') COLLATE decimal AS total FROM {table}",
    'SELECT decimal_div(SUM(amount),COUNT(*),4) AS total FROM {table}',
])
def test_currency_follows_verified_expressions_and_retains_computed_precision(sql):
    output=query(host('บาท'),sql)
    assert output['columns'][0]['unit']=='บาท' and 'บาท' in output['message']
    assert output['rows'][0]['total'] in output['message']
    view=answer_composition.View();answer_composition.add_read(view,output,datetime.now(timezone.utc))
    assert view.records[0]['fields']['c1']['display'].endswith(' บาท')


def test_non_money_units_keep_their_exact_precision_and_survive_another_query():
    h=host('kg');first=query(h,'SELECT SUM(amount) AS total FROM {table}')
    assert first['rows']==[{'total':'32.3456'}] and first['columns'][0]['unit']=='kg'
    second=query(h,'SELECT total AS amount FROM {table}',first['query_ref'])
    assert second['columns'][0]['unit']=='kg' and '32.3456 kg' in second['message'] and 'บาท' not in second['message']


def test_source_provenance_is_carried_only_from_used_relations():
    h=host('kg');h._query_reads['source']['provenance_notes']=['Verified source convention']
    h._query_reads['unused']=host('kg')._query_reads['source']
    h._query_reads['unused']['provenance_notes']=['Unrelated convention']
    output=query(h,'SELECT SUM(amount) AS total FROM {table}')
    assert output['provenance_notes']==['Verified source convention']
    assert 'Unrelated' not in output['message']


def test_a_ratio_of_matching_units_is_dimensionless():
    output=query(host('บาท'),'SELECT decimal_div(amount,SUM(amount) OVER (),4) AS total FROM {table}')
    assert not output['columns'][0].get('unit') and 'บาท' not in output['message']


def test_text_and_unitless_decimal_values_are_not_reformatted_by_their_alias():
    output=query(host(), 'SELECT label AS total, amount AS amount FROM {table}')
    assert output['rows'][0]=={'total':'000187','amount':'12.3456'}
    assert '000187' in output['message'] and '12.3456' in output['message'] and 'บาท' not in output['message']


def test_mixed_source_units_are_not_inferred_as_a_single_currency():
    h=host('บาท');h._query_reads['source']['columns'].append({'name':'weight','type':'decimal','unit':'kg'})
    for row in h._query_reads['source']['analysis_records']:row['weight']='2.0000'
    output=query(h,'SELECT decimal_add(amount,weight) AS total FROM {table}')
    assert not output['columns'][0].get('unit') and 'บาท' not in output['message'] and 'kg' not in output['message']


@pytest.mark.parametrize('other_unit,unit',[('บาท','บาท'),('kg',None)])
def test_union_requires_consistent_units_from_every_branch(other_unit,unit):
    h=host('บาท');h._query_reads['other']=host(other_unit)._query_reads['source']
    a=relational.materialize(h,'source')['sql_table'];b=relational.materialize(h,'other')['sql_table']
    output=relational.execute(h,{'query_refs':['source','other'],
        'sql':f'SELECT amount AS total FROM {a} UNION ALL SELECT amount FROM {b}',
        'source_refs':['owner'],'continue_analysis':False})
    assert output['ok'] and output['columns'][0].get('unit')==unit
