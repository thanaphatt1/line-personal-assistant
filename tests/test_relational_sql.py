"""Actual SQLite execution, precision and read isolation; synthetic data only."""
from copy import deepcopy
import sqlite3
import pytest
from line_assistant.relational_sql import execute,SqlError


def relation():
    return {'sql_table':'observed','columns':[{'name':'name','type':'text'},{'name':'team','type':'text'},
        {'name':'amount','type':'decimal'}], 'records':[
            {'name':'A','team':'x','amount':'0.10'}, {'name':'B','team':'x','amount':'0.20'},
            {'name':'C','team':'y','amount':'90.00'}, {'name':'D','team':'y','amount':'20.00'}]}


def run(sql,**kwargs):return execute([relation()],sql,**kwargs)[1]


def test_cte_case_having_and_window_are_composable_without_new_operations():
    sql="""WITH groups AS (
      SELECT team,SUM(amount) AS total,COUNT(*) AS n FROM observed GROUP BY team HAVING COUNT(*)>1)
      SELECT team,total,CASE WHEN total COLLATE decimal > '100' THEN 'large' ELSE 'small' END AS band,
             DENSE_RANK() OVER (ORDER BY total COLLATE decimal DESC) AS position
      FROM groups ORDER BY total COLLATE decimal DESC"""
    assert run(sql)==[{'team':'y','total':'110.00','band':'large','position':1},
                     {'team':'x','total':'0.30','band':'small','position':2}]


def test_decimal_order_filter_sum_product_and_explicitly_rounded_average():
    assert run("SELECT amount FROM observed WHERE amount>'1' ORDER BY amount DESC")==[{'amount':'90.00'},{'amount':'20.00'}]
    assert run("SELECT SUM(amount) AS total, decimal_div(SUM(amount),COUNT(*),2) AS mean, decimal_mul(SUM(amount),'0.1') AS tenth FROM observed")==[
        {'total':'110.30','mean':'27.58','tenth':'11.030'}]


def test_decimal_sum_window_keeps_exact_running_values():
    assert run('SELECT name,SUM(amount) OVER(ORDER BY name ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS total FROM observed ORDER BY name')==[
        {'name':'A','total':'0.10'},{'name':'B','total':'0.30'},{'name':'C','total':'90.20'},{'name':'D','total':'110.00'}]


def test_decimal_values_above_binary64_precision_stay_exact():
    r=relation();r['records']=[{'name':'a','team':'x','amount':'9007199254740993.01'},{'name':'b','team':'x','amount':'0.02'}]
    assert execute([r],'SELECT SUM(amount) AS total FROM observed')[1]==[{'total':'9007199254740993.03'}]


@pytest.mark.parametrize('sql',[
    'DELETE FROM observed','UPDATE observed SET amount=0','DROP TABLE observed','CREATE TABLE other(x)',
    "ATTACH DATABASE '/tmp/unoffered.db' AS secret",'VACUUM','PRAGMA database_list',
    'SELECT * FROM sqlite_master','SELECT * FROM unoffered',"SELECT load_extension('unknown') FROM observed",
    "SELECT readfile('/etc/passwd') FROM observed",'SELECT randomblob(100000000) FROM observed',
    'EXPLAIN SELECT * FROM observed','/* comment */ EXPLAIN QUERY PLAN SELECT * FROM observed',
    'SELECT * FROM observed; DELETE FROM observed',
])
def test_no_files_schema_or_mutations_can_be_requested(sql):
    r=relation();before=deepcopy(r)
    with pytest.raises(SqlError):execute([r],sql)
    assert r==before


@pytest.mark.parametrize('sql',['SELECT 999 AS total','SELECT 1','SELECT * FROM pragma_database_list()'])
def test_unobserved_constant_or_metadata_queries_are_not_data_reads(sql):
    with pytest.raises(SqlError):run(sql)


@pytest.mark.parametrize('sql',["SELECT 0.1 AS invented_fraction FROM observed",'SELECT amount+0.0 AS imprecise FROM observed',
    'SELECT SUM(amount+0.0) AS imprecise FROM observed','SELECT AVG(amount) AS mean FROM observed',
    'SELECT decimal_div(amount,0,2) AS broken FROM observed'])
def test_implicit_floats_and_invalid_decimal_calculations_are_not_reported_as_exact(sql):
    with pytest.raises(SqlError):run(sql)


@pytest.mark.parametrize('other',['value','VALUE'])
def test_duplicate_output_names_are_rejected_before_rows_are_overwritten(other):
    with pytest.raises(SqlError,match='duplicate_output_column'):run(f'SELECT name AS value,amount AS {other} FROM observed')


def test_output_limit_never_returns_a_partial_total():
    with pytest.raises(SqlError,match='resource_limit'):run('SELECT * FROM observed',max_bytes=20)


def test_expensive_cross_join_is_interrupted_and_connection_is_not_reused():
    with pytest.raises(SqlError,match='resource_limit'):
        run('SELECT COUNT(*) FROM observed a, observed b, observed c, observed d, observed e, observed f, observed g',steps=1000)
    assert run('SELECT COUNT(*) AS n FROM observed')==[{'n':4}]


def test_truncated_details_do_not_prevent_counting_but_cannot_filter_membership():
    r=relation();r['unavailable_fields']=['name']
    assert execute([r],'SELECT COUNT(*) AS n FROM observed')[1]==[{'n':4}]
    with pytest.raises(SqlError,match='incomplete_field'):execute([r],"SELECT COUNT(*) FROM observed WHERE name='A'")


def test_recursive_reads_are_allowed_but_still_resource_bounded():
    assert run("WITH RECURSIVE n(x) AS(SELECT 1 FROM observed LIMIT 1), r(x) AS(SELECT x FROM n UNION ALL SELECT x+1 FROM r WHERE x<3) SELECT x FROM r")==[
        {'x':1},{'x':2},{'x':3}]


def test_blob_output_is_a_planning_error_not_an_unserializable_result():
    with pytest.raises(SqlError,match='output_type'):run('SELECT CAST(name AS BLOB) AS raw FROM observed')


def test_leading_comments_do_not_change_valid_select_semantics():
    assert run('-- synthetic comment\n /* data */ SELECT COUNT(*) AS n FROM observed')==[{'n':4}]
