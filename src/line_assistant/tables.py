"""Metadata-defined records and provider-independent relational operations.

Only configured logical table/field names enter model arguments. Adapters must
return a complete bounded snapshot; display limits never limit aggregation.
"""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal, Protocol
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .history_context import digest
from .language import BANGKOK
from .tool_errors import PlanningError


class TableError(PlanningError):
    """Fixed, model-visible error code, without provider details."""


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Column(Strict):
    name: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=500)
    type: Literal['text', 'integer', 'decimal', 'boolean', 'date', 'enum', 'reference']
    required: bool = False
    choices: list[str] = Field(default_factory=list, max_length=100)
    default: str | int | bool | None = None
    default_today: bool = False
    references: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_]{0,63}$')
    unit: str | None = Field(default=None, max_length=40)
    storage_name: str | None = Field(default=None, min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list,max_length=20)

    @model_validator(mode='after')
    def valid(self):
        if (self.type == 'enum' and (not self.choices or len(set(self.choices)) != len(self.choices))
                or self.type != 'enum' and self.choices
                or self.type == 'reference' and self.references is None
                or self.type != 'reference' and self.references is not None
                or self.default_today and (self.type != 'date' or self.default is not None)):
            raise ValueError('Invalid column metadata')
        if any(not v or len(v) > 200 for v in self.choices):
            raise ValueError('Invalid enum choices')
        if any(not v or len(v)>100 for v in self.aliases):raise ValueError('Invalid column aliases')
        if self.default is not None:
            normalize(self, self.default)
        return self


class Table(Strict):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    columns: list[Column] = Field(min_length=1, max_length=40)
    operations: list[Literal['query', 'insert', 'update', 'delete']] = Field(default_factory=lambda: ['query'])
    examples: list[str] = Field(default_factory=list, max_length=5)
    display_field: str | None = None
    routing_group: str | None = Field(default=None,pattern=r'^[a-z][a-z0-9_]{0,63}$')
    default_for_group: bool = False

    @model_validator(mode='after')
    def valid(self):
        if (len({v.name for v in self.columns}) != len(self.columns)
                or len({v.storage_name or v.name for v in self.columns}) != len(self.columns)
                or any((v.storage_name or v.name) == '_id' for v in self.columns)
                or any(v.name=='id' for v in self.columns)
                or len(set(self.operations)) != len(self.operations) or 'query' not in self.operations
                or any(not v or len(v) > 100 for v in self.aliases)
                or any(not v or len(v) > 500 for v in self.examples)
                or self.display_field is not None and self.display_field not in {c.name for c in self.columns}):
            raise ValueError('Invalid table metadata')
        return self

    def column(self, name):
        if not isinstance(name,str) or not name:raise TableError('unknown_field')
        if name=='id':return Column(name='id',label='รหัสรายการ',type='text',required=True)
        found = next((c for c in self.columns if c.name == name), None)
        if found is None:
            matches=[c for c in self.columns if name in {c.label,c.storage_name,*c.aliases}]
            if len(matches)>1:raise TableError('ambiguous_field')
            found=matches[0] if matches else None
        if found is None:
            raise TableError('unknown_field')
        return found


def normalize(column, value):
    if value is None:
        if column.required:
            raise TableError('required_field:' + column.name)
        return None
    if column.type in {'text', 'enum', 'reference', 'date'}:
        if not isinstance(value, str) or not value.strip() or len(value) > 2000 or '\x00' in value:
            raise TableError('invalid_value:' + column.name)
        if column.type == 'enum' and value not in column.choices:
            raise TableError('invalid_enum:' + column.name)
        if column.type == 'reference' and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
            raise TableError('invalid_reference:' + column.name)
        if column.type == 'date':
            try:
                if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise TableError('invalid_date:' + column.name) from None
        return value
    if column.type == 'boolean':
        if type(value) is not bool:
            raise TableError('invalid_boolean:' + column.name)
        return value
    if column.type == 'integer':
        if type(value) is not int or abs(value) > 10**15:
            raise TableError('invalid_integer:' + column.name)
        return value
    try:
        if type(value) not in {str, int} or len(str(value)) > 80:
            raise ValueError()
        number = Decimal(value)
        if not number.is_finite() or number.copy_abs() >= Decimal('1e24') or number.as_tuple().exponent < -12:
            raise ValueError()
        return format(number, 'f')
    except (ValueError, InvalidOperation):
        raise TableError('invalid_decimal:' + column.name) from None


def validate_values(table, values, *, today=None, defaults=False):
    if not isinstance(values, dict) or set(values) - {c.name for c in table.columns}:
        raise TableError('unknown_field')
    result = {}
    for column in table.columns:
        if column.name in values:
            value = values[column.name]
        elif defaults and column.default_today and today:
            value = today
        elif defaults and column.default is not None:
            value = deepcopy(column.default)
        else:
            value = None
        result[column.name] = normalize(column, value)
    return result


class Store(Protocol):
    scope_id: str
    def scan(self, table: Table) -> list[dict]: ...
    def apply(self, table: Table, change: dict, key: str) -> dict: ...
    def reconcile(self, table: Table, change: dict, key: str) -> dict | None: ...


class Registry:
    def __init__(self, tables=(), stores=None):
        self.tables = {t.id: t.model_copy(deep=True) for t in tables}
        if len(self.tables) != len(tables):
            raise ValueError('Duplicate table ID')
        self.stores = dict(stores or {})
        if set(self.tables) != set(self.stores):
            raise ValueError('Every table requires one bound store')
        validate_routing(list(self.tables.values()))
        for table in self.tables.values():
            if any(c.references and c.references not in self.tables for c in table.columns):
                raise ValueError('Unknown referenced table')

    def get(self, identifier):
        if identifier not in self.tables:
            raise TableError('unknown_table')
        return self.tables[identifier], self.stores[identifier]

    def binding(self, identifier):
        table, store = self.get(identifier)
        return digest([table.model_dump(), store.scope_id])

    def catalog(self):
        return [dict(id=t.id, label=t.label, description=t.description, aliases=t.aliases,
                     operations=t.operations,routing_group=t.routing_group,default_for_group=t.default_for_group) for t in self.tables.values()]

    def scan(self, identifier):
        table, store = self.get(identifier)
        records = store.scan(table)
        seen = set()
        for record in records:
            if (set(record) != {'id', 'version', 'values'} or not isinstance(record['id'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', record['id']) or record['id'] in seen
                    or not isinstance(record['version'], str) or not record['version']):
                raise TableError('invalid_snapshot')
            record['values'] = validate_values(table, record['values'])
            seen.add(record['id'])
        return records

    def references(self, table, values):
        for column in table.columns:
            if column.references and values.get(column.name) is not None:
                if values[column.name] not in {r['id'] for r in self.scan(column.references)}:
                    raise TableError('reference_not_found:' + column.name)


def routing_key(table):
    return table.routing_group or 'schema_'+digest([(c.name,c.type,c.required,c.references) for c in table.columns])


def validate_routing(tables):
    defaults={}
    for table in tables:
        if table.default_for_group:
            group=routing_key(table)
            if group in defaults:raise ValueError('Only one default per routing group is allowed')
            defaults[group]=table.id


class Predicate(Strict):
    field: str
    op: Literal['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'in', 'is_null']
    values: list[str | int | bool | None] = Field(default_factory=list, max_length=1000)


class Aggregate(Strict):
    op: Literal['count', 'sum', 'avg', 'min', 'max']
    field: str | None = None
    alias: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')


class Order(Strict):
    field: str
    descending: bool = False


class Query(Strict):
    table_id: str
    filters: list[Predicate] = Field(default_factory=list, max_length=30)
    select: list[str] = Field(default_factory=list, max_length=40,description='Columns for ordinary record output. MUST be [] when aggregates are requested; aggregate output is group_by plus aggregate aliases.')
    group_by: list[str] = Field(default_factory=list, max_length=10)
    aggregates: list[Aggregate] = Field(default_factory=list, max_length=20)
    order_by: list[Order] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=20, ge=1, le=100)
    calendar_query_refs: list[str] = Field(default_factory=list, max_length=16)
    date_field: str | None = None
    # Semi-join: keep rows whose field is in another complete table result.
    matching_query_ref: str | None = Field(default=None,description='For conditions on a RELATED table: first query that table with continue_analysis=true, then copy its actual query_ref here. The executor uses its complete matched set, including an empty verified set.')
    matching_field: str | None = Field(default=None,description='Column in this target table to match against values from matching_query_ref, such as a reference column. Null without a semi-join.')
    source_field: str | None = Field(default=None,description='Column in the referenced query result, usually virtual id for a reference relationship. Null without a semi-join.')
    continue_analysis: bool = Field(default=False,description='True when a following query needs these results, including a source query for a relationship join. Returns a query_ref and continues read-only planning.')
    source_refs: list[str] = Field(min_length=1, max_length=16)


def comparable(column, value):
    return Decimal(value) if value is not None and column.type in {'integer', 'decimal'} else value


def canonical_query(table,p):
    """Unique declared column labels/aliases resolve to stable logical names."""
    name=lambda value:table.column(value).name
    aliases={a.alias for a in p.aggregates}
    def order_name(value):
        if value in aliases:return value
        try:return name(value)
        except TableError:raise TableError('unknown_order_field') from None
    return p.model_copy(update={
        'filters':[f.model_copy(update={'field':name(f.field)}) for f in p.filters],
        'select':[name(v) for v in p.select], 'group_by':[name(v) for v in p.group_by],
        'aggregates':[a.model_copy(update={'field':name(a.field) if a.field is not None else None}) for a in p.aggregates],
        'order_by':[o.model_copy(update={'field':order_name(o.field)}) for o in p.order_by],
        'date_field':name(p.date_field) if p.date_field is not None else None,
        'matching_field':name(p.matching_field) if p.matching_field is not None else None})


def predicate(table, p):
    column = table.column(p.field)
    if p.op=='in' and not p.values:raise TableError('empty_membership_requires_source')
    if p.op == 'is_null':
        if p.values:
            raise TableError('invalid_filter')
        return lambda row: row[p.field] is None
    if (p.op != 'in' and len(p.values) != 1
            or p.op == 'contains' and column.type != 'text'
            or p.op in {'gt', 'gte', 'lt', 'lte'} and column.type not in {'integer', 'decimal', 'date', 'text'}):
        raise TableError('invalid_filter')
    wanted = [comparable(column, normalize(column, v)) for v in p.values]
    def test(row):
        value = comparable(column, row[p.field])
        if p.op == 'in': return value in wanted
        other = wanted[0]
        if p.op == 'eq': return value == other
        if p.op == 'ne': return value != other
        if value is None or other is None: return False
        if p.op == 'contains': return other.casefold() in value.casefold()
        if p.op == 'gt': return value > other
        if p.op == 'gte': return value >= other
        if p.op == 'lt': return value < other
        return value <= other
    return test


def query(registry, p, *, joined_dates=None, joined_values=None):
    table, _ = registry.get(p.table_id)
    p=canonical_query(table,p)
    for name in p.select + p.group_by:
        table.column(name)
    if len(set(p.group_by)) != len(p.group_by) or len(set(p.select)) != len(p.select):
        raise TableError('duplicate_field')
    tests = [predicate(table, f) for f in p.filters]
    if joined_dates is not None:
        if not p.date_field or table.column(p.date_field).type != 'date':
            raise TableError('date_field_required')
        tests.append(lambda row: row[p.date_field] in joined_dates)
    elif p.date_field is not None or p.calendar_query_refs:
        raise TableError('invalid_calendar_basis')
    if joined_values is not None:
        column = table.column(p.matching_field)
        wanted = {comparable(column, normalize(column, value)) for value in joined_values if value is not None}
        tests.append(lambda row,column=column,wanted=wanted: comparable(column, row[p.matching_field]) in wanted)
    elif p.matching_query_ref or p.matching_field or p.source_field:
        raise TableError('invalid_join')
    aliases = set()
    for aggregate in p.aggregates:
        if aggregate.alias in aliases or aggregate.alias in {c.name for c in table.columns} or aggregate.alias == 'id':
            raise TableError('aggregate_alias')
        aliases.add(aggregate.alias)
        if aggregate.field is None:
            if aggregate.op != 'count': raise TableError('aggregate_field_required')
        else:
            column = table.column(aggregate.field)
            if aggregate.op in {'sum', 'avg'} and column.type not in {'integer', 'decimal'}:
                raise TableError('numeric_field_required')
    if p.select and p.aggregates:
        raise TableError('aggregate_select_conflict')
    if p.group_by and not p.aggregates:
        raise TableError('aggregate_required')
    names = set(p.group_by) | aliases if p.aggregates else {c.name for c in table.columns} | {'id'}
    if any(o.field not in names for o in p.order_by):
        raise TableError('unknown_order_field')
    records = registry.scan(p.table_id)
    matched = [r for r in records if all(test(dict(id=r['id'],**r['values'])) for test in tests)]
    if p.aggregates:
        groups = {}
        for r in matched:
            values=dict(id=r['id'],**r['values'])
            groups.setdefault(tuple(values[name] for name in p.group_by), []).append(values)
        if not p.group_by and not groups: groups[()] = []
        rows = []
        for keys, values in groups.items():
            row = dict(zip(p.group_by, keys))
            for aggregate in p.aggregates:
                column = table.column(aggregate.field) if aggregate.field else None
                numbers = [comparable(column, v[aggregate.field]) for v in values if v[aggregate.field] is not None] if column else []
                if aggregate.op == 'count': result = len(numbers) if column else len(values)
                elif not numbers: result = '0' if aggregate.op == 'sum' else None
                elif aggregate.op in {'sum', 'avg'}:
                    with localcontext() as ctx:
                        ctx.prec = 64
                        result = format(sum(numbers, Decimal(0)) / (len(numbers) if aggregate.op == 'avg' else 1), 'f')
                else:
                    result = min(numbers) if aggregate.op == 'min' else max(numbers)
                    if isinstance(result, Decimal): result = format(result, 'f')
                row[aggregate.alias] = result
            rows.append(row)
    else:
        rows = [dict(id=r['id'], **r['values']) for r in matched]
    def sort_value(row, name):
        value = row.get(name)
        aggregate = next((a for a in p.aggregates if a.alias == name), None)
        numeric = aggregate and (aggregate.op in {'count', 'sum', 'avg'} or table.column(aggregate.field).type in {'integer', 'decimal'})
        if value is not None and (numeric or name != 'id' and name not in aliases and table.column(name).type in {'integer', 'decimal'}):
            value = Decimal(value)
        return value
    # Stable multi-key ordering, with nulls always last in either direction.
    for order in reversed(p.order_by):
        known = [r for r in rows if r.get(order.field) is not None]
        missing = [r for r in rows if r.get(order.field) is None]
        rows = sorted(known, key=lambda r: sort_value(r, order.field), reverse=order.descending) + missing
    total_rows = len(rows)
    shown = rows[:p.limit]
    if p.select:
        shown = [{k: v for k, v in row.items() if k == 'id' or k in p.select} for row in shown]
    return {'table_query': True, 'domain': 'table', 'ok': True, 'complete': True,
            'table_id': table.id, 'label': table.label, 'count': len(matched), 'result_count': total_rows,
            'rows': shown, 'shown': len(shown), 'truncated': total_rows > len(shown),
            'analysis_records': [{**deepcopy(r),'values':{k:v for k,v in r['values'].items() if not p.select or k in p.select}} for r in matched], 'scope': {'domain': 'table', 'table_id': table.id,
                'binding': registry.binding(table.id), 'query': p.model_dump(exclude={'source_refs', 'continue_analysis'})}}


class MemoryTableStore:
    """Explicit synthetic adapter, never selected by production configuration."""
    mocked = True
    def __init__(self, scope_id='synthetic-table', records=()):
        self.scope_id = scope_id
        self.records = {r['id']: deepcopy(r) for r in records}
        self.operations = {}
        self.writes = 0

    def scan(self, table):
        return list(deepcopy(self.records).values())

    def reconcile(self, table, change, key):
        proof = self.operations.get(key)
        if proof and proof['hash'] != digest(change): raise TableError('operation_conflict')
        return deepcopy(proof['receipt']) if proof else None

    def apply(self, table, change, key):
        proof = self.reconcile(table, change, key)
        if proof: return proof
        current = self.records.get(change['id'])
        if (change['operation'] == 'insert' and current is not None
                or change['operation'] != 'insert' and (not current or current['version'] != change['before']['version'])):
            raise TableError('record_changed')
        if change['operation'] == 'delete': self.records.pop(change['id'])
        else: self.records[change['id']] = {'id': change['id'], 'version': digest([key, change]), 'values': deepcopy(change['after'])}
        receipt = {'id': change['id'], 'operation': change['operation']}
        self.operations[key] = {'hash': digest(change), 'receipt': receipt}
        self.writes += 1
        return receipt
