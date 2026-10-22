"""Isolated in-memory SQLite over supplied snapshots; never provider SQL.

Only SELECT/read/function authorizer actions are permitted after loading.
Decimal text has numeric collation and explicit decimal arithmetic; SQLite's
implicit binary64 arithmetic is rejected rather than reported as exact money.
"""
from decimal import Decimal, InvalidOperation, Inexact, localcontext, ROUND_HALF_EVEN
import sqlite3
import re
from time import monotonic
from .tool_errors import PlanningError


class SqlError(PlanningError):pass


FUNCTIONS=set('abs char coalesce concat concat_ws format glob hex if ifnull iif instr length like likelihood likely lower ltrim max min nullif octet_length printf quote replace round rtrim sign soundex substr substring trim typeof unhex unicode unlikely upper date time datetime julianday unixepoch strftime timediff count group_concat string_agg row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value sum decimal_sum decimal_add decimal_sub decimal_mul decimal_div decimal_cmp json json_array json_array_length json_error_position json_extract json_insert json_object json_patch json_quote json_remove json_replace json_set json_type json_valid'.split())


def number(value):
    if isinstance(value,float):raise SqlError('relation_inexact_numeric')
    try:value=Decimal(str(value))
    except (InvalidOperation,ValueError):raise SqlError('relation_invalid_decimal') from None
    if not value.is_finite() or len(value.as_tuple().digits)>512 or abs(value.adjusted())>512:
        raise SqlError('relation_invalid_decimal')
    return value


def decimal_operation(op,*args):
    if any(v is None for v in args):return None
    with localcontext() as ctx:
        ctx.prec=1024;ctx.traps[Inexact]=True
        values=[number(v) for v in args]
        if op=='add':value=values[0]+values[1]
        elif op=='sub':value=values[0]-values[1]
        elif op=='mul':value=values[0]*values[1]
        else:
            places=int(values[2])
            if values[2]!=places or not 0<=places<=64:raise SqlError('relation_decimal_places')
            ctx.traps[Inexact]=False
            value=(values[0]/values[1]).quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_EVEN)
        return format(value,'f')


class DecimalSum:
    def __init__(self):self.total=Decimal(0);self.count=0
    def step(self,value):
        if value is not None:
            self.total=Decimal(decimal_operation('add',self.total,number(value)));self.count+=1
    def inverse(self,value):
        if value is not None:
            self.total=Decimal(decimal_operation('sub',self.total,number(value)));self.count-=1
    def value(self):return format(self.total,'f') if self.count else None
    def finalize(self):return self.value()


def quote(value):return '"'+value.replace('"','""')+'"'


def execute(relations,sql,*,seconds=2,steps=2_000_000,max_bytes=4_000_000):
    """Return complete computed rows or an error, never a resource-limited subtotal."""
    head=re.sub(r'\A(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*','',sql,flags=re.S)
    if not re.match(r'(?:SELECT|WITH)\b',head,re.I):raise SqlError('relation_select_required')
    connection=sqlite3.connect(':memory:')
    failure=[];read=set();started=monotonic();ticks=0;used=0
    def checked(fn):
        def call(*args):
            try:return fn(*args)
            except Exception as error:
                failure.append(str(error) if isinstance(error,SqlError) else 'relation_decimal_error')
                raise
        return call
    class Sum(DecimalSum):
        def step(self,value):return checked(super().step)(value)
        def inverse(self,value):return checked(super().inverse)(value)
    def progress():
        nonlocal ticks
        ticks+=1000
        return int(ticks>steps or monotonic()-started>seconds)
    names={r['sql_table']:r for r in relations}
    def authorize(action,arg1,arg2,db,trigger):
        if action in {sqlite3.SQLITE_SELECT,sqlite3.SQLITE_RECURSIVE}:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_READ:
            # SQLite's COUNT(*) optimization can omit the database name.
            if db not in {'main',None} or arg1 not in names:return sqlite3.SQLITE_DENY
            if arg2 in names[arg1].get('unavailable_fields',[]):
                failure.append('relation_incomplete_field');return sqlite3.SQLITE_DENY
            read.add(arg1);return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_FUNCTION:
            if (arg2 or '').lower() not in FUNCTIONS:
                failure.append('relation_function_unavailable');return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    try:
        connection.enable_load_extension(False)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,max_bytes)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,32_000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN,200)
        connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH,80)
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.create_collation('decimal',lambda a,b:(number(a)>number(b))-(number(a)<number(b)))
        for name,op in [('decimal_add','add'),('decimal_sub','sub'),('decimal_mul','mul'),('decimal_div','div')]:
            connection.create_function(name,3 if op=='div' else 2,checked(lambda *v,op=op:decimal_operation(op,*v)),deterministic=True)
        connection.create_function('decimal_cmp',2,checked(lambda a,b:None if a is None or b is None else (number(a)>number(b))-(number(a)<number(b))),deterministic=True)
        for name in ['sum','decimal_sum']:connection.create_window_function(name,1,Sum)
        for relation in relations:
            columns=relation['columns'];table=quote(relation['sql_table'])
            declarations=[quote(c['name'])+(' INTEGER' if c['type'] in {'integer','boolean'} else ' TEXT')+
                (' COLLATE decimal' if c['type']=='decimal' else '') for c in columns]
            connection.execute('CREATE TABLE '+table+' ('+', '.join(declarations)+')')
            connection.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in columns)+')',
                [[row.get(c['name']) for c in columns] for row in relation['records']])
        connection.commit();connection.execute('PRAGMA query_only=ON')
        connection.set_authorizer(authorize);connection.set_progress_handler(progress,1000)
        cursor=connection.execute(sql)
        if cursor.description is None:raise SqlError('relation_select_required')
        columns=[d[0] for d in cursor.description]
        if len({c.casefold() for c in columns})!=len(columns):raise SqlError('relation_duplicate_output_column')
        rows=[]
        for values in cursor:
            if any(isinstance(v,float) for v in values):raise SqlError('relation_inexact_numeric')
            if any(type(v) not in {str,int,type(None)} for v in values):raise SqlError('relation_output_type')
            used+=sum(len(str(v).encode()) for v in values)
            if used>max_bytes or monotonic()-started>seconds:raise SqlError('relation_resource_limit',kind='unsupported')
            rows.append(dict(zip(columns,values)))
        if not read:raise SqlError('relation_source_required')
        return columns,rows,sorted(read)
    except sqlite3.Error as error:
        if failure:raise SqlError(failure[0]) from None
        if 'interrupted' in str(error) or isinstance(error,sqlite3.DataError):raise SqlError('relation_resource_limit',kind='unsupported') from None
        # SQLite syntax/column diagnostics contain only the model's SQL or
        # offered logical identifiers, never provider settings or row values.
        detail=str(error)[:200]
        match=re.search(r'no such column: (.+)',detail)
        code=('relation_unknown_column' if match else 'relation_unknown_table' if 'no such table:' in detail
            else 'relation_unknown_function' if 'no such function:' in detail else 'relation_sql_syntax')
        problem=SqlError(code,fields=[match[1].split('.')[-1]] if match else ['sql'])
        problem.detail=detail
        raise problem from None
    finally:connection.close()
