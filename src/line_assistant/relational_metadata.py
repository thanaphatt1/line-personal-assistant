"""Conservative result units from source schemas and SQL expressions.

This inspects a copy of a SELECT after SQLite has executed it. It never runs or
rewrites the provider query, authorizes SQL, or infers units from output names.
Unresolved expressions keep their exact value without a guessed unit.
"""
from dataclasses import dataclass

from sqlglot import exp, parse_one
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope


@dataclass(frozen=True)
class Quantity:
    unit: str | None = None
    scalar: bool = False
    zero: bool = False
    omit_empty: bool = False


UNKNOWN = Quantity()
SCALAR = Quantity(scalar=True)
ZERO = Quantity(scalar=True, zero=True)


def same(values):
    values = [v for v in values if not v.zero]
    if not values:
        return ZERO
    if all(v.unit and v.unit == values[0].unit for v in values):
        return Quantity(values[0].unit)
    return SCALAR if all(v.scalar for v in values) else UNKNOWN


def result_columns(relations, sql, names, records):
    """Attach a unit only when the expression preserves verified source units."""
    inferred = []
    try:
        sources = {r['sql_table'].casefold(): {
            c['name'].casefold(): Quantity(c.get('unit'),omit_empty=c.get('omit_empty') is True) for c in r['columns']
        } for r in relations}
        schema = {r['sql_table']: {c['name']: 'INT' if c['type'] in {'integer', 'boolean'} else 'TEXT'
                                  for c in r['columns']} for r in relations}
        tree = parse_one(sql, read='sqlite')
        if sum(1 for _ in tree.walk()) > 4000:
            raise ValueError('metadata_expression_bound')
        tree = qualify(tree, dialect='sqlite', schema=schema, identify=False,
                       quote_identifiers=False, validate_qualify_columns=False, expand_alias_refs=False)
        if sum(1 for _ in tree.walk()) > 8000:
            raise ValueError('metadata_projection_bound')
        resolved = {}
        for scope in traverse_scope(tree):
            available = {}
            for alias, source in scope.sources.items():
                if isinstance(source, Scope):
                    available[alias.casefold()] = dict(resolved.get(id(source), []))
                elif isinstance(source, exp.Table):
                    available[alias.casefold()] = sources.get(source.name.casefold(), {})

            def value(node):
                if node is None or isinstance(node, exp.Null):
                    return ZERO
                if isinstance(node, (exp.Alias, exp.Paren, exp.Collate)):
                    return value(node.this)
                if isinstance(node, (exp.Window, exp.Cast)):
                    q=value(node.this)
                    return Quantity(q.unit,q.scalar,q.zero)
                if isinstance(node, exp.Column):
                    if node.table:
                        return available.get(node.table.casefold(), {}).get(node.name.casefold(), UNKNOWN)
                    matches = [cols[node.name.casefold()] for cols in available.values() if node.name.casefold() in cols]
                    return matches[0] if len(matches) == 1 else UNKNOWN
                if isinstance(node, exp.Literal):
                    if node.is_number:
                        return ZERO if node.this == '0' else SCALAR
                    return ZERO if node.this in {'0', '0.0', '0.00'} else UNKNOWN
                if isinstance(node, exp.Count):
                    return SCALAR
                if isinstance(node, exp.Distinct):
                    return value(node.expressions[0]) if len(node.expressions) == 1 else UNKNOWN
                if isinstance(node, (exp.Sum, exp.Avg, exp.Abs, exp.Neg)):
                    q=value(node.this)
                    return Quantity(q.unit,q.scalar,q.zero)
                if isinstance(node, (exp.Min, exp.Max, exp.Coalesce)):
                    return same([value(node.this), *[value(n) for n in node.expressions]])
                if isinstance(node, exp.Case):
                    return same([value(n.args.get('true')) for n in node.args.get('ifs', [])]
                                + [value(node.args.get('default'))])
                if isinstance(node, exp.If):
                    return same([value(node.args.get('true')), value(node.args.get('false'))])
                if isinstance(node, (exp.Add, exp.Sub)):
                    return same([value(node.this), value(node.expression)])
                if isinstance(node, (exp.Mul, exp.Div)):
                    a, b = value(node.this), value(node.expression)
                    if isinstance(node, exp.Div) and a.unit and a.unit == b.unit:
                        return SCALAR
                    if a.unit and b.scalar:
                        return Quantity(a.unit)
                    if isinstance(node, exp.Mul) and b.unit and a.scalar:
                        return Quantity(b.unit)
                    return SCALAR if a.scalar and b.scalar else UNKNOWN
                if isinstance(node, exp.Anonymous):
                    args = node.expressions; name = node.name.casefold()
                    if name in {'decimal_sum', 'lag', 'lead', 'first_value', 'last_value', 'nth_value'} and args:
                        q=value(args[0])
                        return Quantity(q.unit,q.scalar,q.zero)
                    if name in {'decimal_add', 'decimal_sub'} and len(args) == 2:
                        return same([value(n) for n in args])
                    if name in {'decimal_mul', 'decimal_div'} and len(args) >= 2:
                        a, b = value(args[0]), value(args[1])
                        if name == 'decimal_div' and a.unit and a.unit == b.unit:
                            return SCALAR
                        if a.unit and b.scalar:
                            return Quantity(a.unit)
                        if name == 'decimal_mul' and b.unit and a.scalar:
                            return Quantity(b.unit)
                        return SCALAR if a.scalar and b.scalar else UNKNOWN
                return UNKNOWN

            if scope.set_operation_scopes:
                branches = [resolved.get(id(s), []) for s in scope.set_operation_scopes]
                rows = [(name, same([branch[i][1] for branch in branches]))
                        for i, (name, _) in enumerate(branches[0])
                        if all(len(branch) == len(branches[0]) for branch in branches)] if branches else []
            else:
                rows = [(node.alias_or_name.casefold(), value(node)) for node in scope.expression.selects]
            if scope.outer_columns and len(scope.outer_columns) == len(rows):
                rows = [(name.casefold(), row[1]) for name, row in zip(scope.outer_columns, rows)]
            resolved[id(scope)] = rows
            inferred = [info for _, info in rows]
    except Exception:
        # Metadata parsing is not an alternative SQL validator. A supported
        # SQLite query keeps working even if its result lineage is unknown.
        inferred = []
    if len(inferred) != len(names):
        inferred = [UNKNOWN] * len(names)
    return [{'name': name, 'type': 'integer' if any(type(r[name]) is int for r in records) else 'text',
             **({'unit': info.unit} if info.unit else {}),
             **({'omit_empty':True} if info.omit_empty else {})} for name, info in zip(names, inferred)]


def display_fields(row, columns):
    """Suppress empty optional annotations, retaining raw results and rows.

    Metadata must explicitly permit omission. Zero, false, unrelated nulls and
    blank-only results remain visible; output names never determine policy.
    """
    optional={c['name'] for c in columns if c.get('omit_empty') is True}
    selected=[k for k,v in row.items() if k not in optional or not (v is None or isinstance(v,str) and not v.strip())]
    return selected or list(row)
