"""One read-only planner action over typed, owner-bound source snapshots.

Source aliases are known before a read. Fetching, caching and SQL execution are
server steps; the model may inspect sources first when their values are needed
to plan. Existing readers and durable result formats remain compatible.
"""
from copy import deepcopy
from datetime import datetime
from typing import Literal
import json

from pydantic import Field, ValidationError

from .history_context import digest
from .model_api import object_schema
from .tables import Strict
from .tool_errors import PlanningError, recovery

NAME = 'query'
LEGACY = {'query_expenses', 'query_calendar', 'query_relations', 'query_sources'}


class Source(Strict):
    source: Literal['expense', 'calendar', 'table', 'result']
    alias: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    parameters: dict | None = None
    query_ref: str | None = None


class Plan(Strict):
    sources: list[Source] = Field(min_length=1, max_length=8)
    sql: str | None = Field(default=None, min_length=1, max_length=32_000)
    source_refs: list[str] = Field(min_length=1, max_length=16)
    continue_analysis: bool = True


def definitions(host):
    from .read_queries import definitions as readers
    specs = {t['name']: t['parameters']['properties'] for t in readers()}
    period = ('date_mode', 'start_date', 'end_date', 'period_text', 'relative_offset')
    variants = []
    for source, tool, fields in [
        ('expense', 'query_expenses', period),
        ('calendar', 'query_calendar', (*period, 'filter_text', 'purpose', 'count_unit', 'upcoming')),
    ]:
        if source == 'calendar' and not host.calendar_flow.enabled:
            continue
        variants.append(object_schema({
            'source': {'type': 'string', 'enum': [source]},
            'alias': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,63}$'},
            'parameters': object_schema({k: deepcopy(specs[tool][k]) for k in fields}),
        }))
    if variants and variants[-1]['properties']['source']['enum']==['calendar']:
        variants[-1]['properties']['parameters']['properties']['purpose']['description']='expense_dates when selecting event dates for a spending question; appointments when reading appointments. SQL can combine declared Calendar and expense sources.'
    if host.table_flow.enabled:
        variants.append(object_schema({
            'source': {'type': 'string', 'enum': ['table']},
            'alias': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,63}$'},
            'parameters': object_schema({'table_id': {'type': 'string'}}),
        }))
    variants.append(object_schema({
        'source': {'type': 'string', 'enum': ['result']},
        'alias': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,63}$'},
        'query_ref': {'type': 'string', 'description': 'Actual current-request result ref; prior-turn refs are unavailable.'},
    }))
    from .relational import Plan as SqlPlan
    sql = SqlPlan.model_fields['sql'].description
    return [{'type': 'function', 'name': NAME, 'strict': True,
        'description': 'Read and analyze configured data in one action. Declare sources with unique SQL aliases, then select/filter/join/aggregate using those aliases. The server fetches complete snapshots and executes SQL internally. Use SQL for an expense answer, including a single named item. sql=null explores source rows/metadata before another query when needed. Use result sources to analyze current results without rereading providers. Calendar conditions retain semantic uncertainty. No writes or new access.',
        'parameters': object_schema({
            'sources': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {'anyOf': variants}},
            'sql': {'anyOf': [{'type': 'null'}, {'type': 'string', 'minLength': 1, 'maxLength': 32_000}],
                    'description': sql + ' Tables are the aliases declared in sources. Null only to inspect sources; use finish_query_analysis to select verified final results or clarify.'},
            'source_refs': {'type': 'array', 'minItems': 1, 'maxItems': 16, 'items': {'type': 'string'}},
            'continue_analysis': {'type': 'boolean', 'enum': [True], 'description': 'Inspect the actual result against owner intent, then finish_query_analysis or continue querying.'},
        })}]


def context(host):
    from .relational import columns
    return {'query_source_schemas': {
        'expense': {'columns': columns(('date', 'date'), ('item', 'text'), ('amount', 'decimal'),
            ('note', 'text'), ('canonical_item', 'text'), ('category', 'text'), ('subcategory', 'text')),
            'mapping_fields': 'canonical_item/category/subcategory require complete actual mappings for the query; use query_catalog choices; unknown mappings are not nonmatches'},
        **({'calendar': {'columns': columns(('read_ref', 'text'), ('title', 'text'), ('start', 'text'),
            ('end', 'text'), ('all_day', 'boolean'), ('start_date', 'date'), ('last_date', 'date')),
            'coverage': 'Only complete, semantically resolved source events can supply SQL. Use EXISTS for spending on event dates to preserve payment occurrences.'}}
           if host.calendar_flow.enabled else {}),
        **({'table': {'schemas': 'Use describe_table for configured table columns; id is stable record identity.'}}
           if host.table_flow.enabled else {}),
    }}


def _plans(host, plan):
    """Validate every descriptor before any provider request."""
    from .read_queries import Expenses, Calendar
    from .table_workflow import TableQuery
    aliases = [s.alias.casefold() for s in plan.sources]
    if len(set(aliases)) != len(aliases):
        raise PlanningError('query_duplicate_alias')
    result = []
    refs = []
    signatures = set()
    for source in plan.sources:
        if source.source == 'result':
            if source.parameters is not None or not source.query_ref:
                raise PlanningError('query_invalid_source')
            if source.query_ref not in host._query_reads:
                raise PlanningError('relation_reference_unavailable')
            refs.append(source.query_ref)
            result.append((source, None, None))
            continue
        if source.parameters is None or source.query_ref is not None:
            raise PlanningError('query_invalid_source')
        if plan.continue_analysis and source.parameters.get('continue_analysis') is False:
            raise PlanningError('query_invalid_source')
        args = {'source_refs': plan.source_refs, 'continue_analysis': True, **source.parameters}
        args['continue_analysis'] = True if plan.sql is not None else plan.continue_analysis
        if source.source == 'expense':
            args = {'filter': None, 'view': 'list', 'measure': 'expense', 'group_by': 'none',
                    'order_by': 'date', 'descending': False, 'limit': 20, 'calendar_query_refs': [], **args}
            parsed = Expenses.model_validate(args)
            tool = 'query_expenses'
        elif source.source == 'calendar':
            if not host.calendar_flow.enabled:
                raise PlanningError('query_source_unavailable', kind='unsupported')
            parsed = Calendar.model_validate(args)
            tool = 'query_calendar'
        else:
            if not host.table_flow.enabled or args.get('for_change'):
                raise PlanningError('query_source_unavailable', kind='unsupported')
            parsed = TableQuery.model_validate(args)
            host.table_flow.registry.get(parsed.table_id)
            tool = 'query_table'
        host._owner_sources(parsed.source_refs)
        signature=digest([tool,parsed.model_dump()])
        if signature in signatures:raise PlanningError('query_duplicate_source')
        signatures.add(signature)
        result.append((source, tool, parsed.model_dump()))
    if len(set(refs)) != len(refs):
        raise PlanningError('relation_duplicate_reference')
    return result


def execute(host, args):
    from .action_trace import emit, ref, span, outcome
    from .query_analysis import remember
    from . import relational
    base = {'relational_query': True, 'query_analysis': True, 'domain': 'relation',
            'read_only': True, 'financial_write_authorized': False, 'calendar_write_authorized': False}
    try:
        plan = Plan.model_validate(args)
        host._owner_sources(plan.source_refs)
        if host._current['source_ref'] not in plan.source_refs:
            raise PlanningError('relation_current_source_required')
        plans = _plans(host, plan)
        outputs = []
        relations = []
        cache = getattr(host, '_query_source_results', None)
        if cache is None:
            cache = host._query_source_results = {}
        for index, (source, tool, parameters) in enumerate(plans):
            if tool is None:
                output = deepcopy(host._query_reads[source.query_ref])
                query_ref = source.query_ref
            else:
                signature = digest([tool, parameters])
                cached = cache.get(signature)
                emit('query_source', source=source.source, source_index=index, cached=cached is not None,
                     arguments_ref=ref(signature))
                if cached is not None:
                    output, query_ref = deepcopy(cached)
                else:
                    with span('query_source_read'):
                        output = host.tool({'name': tool, 'arguments': json.dumps(parameters, ensure_ascii=False)})
                    # A single exploratory source retains the established reader
                    # format; the ordinary planner loop remembers it once.
                    if plan.sql is None and len(plans) == 1:
                        if output.get('scope',{}).get('continue_analysis') or output.get('table_query') and output.get('continue_analysis'):
                            remembered=remember(host,output,datetime.fromisoformat(host._current['at']),['query',len(host.trace),index])
                            query_ref=remembered['query_ref']
                            output=deepcopy(host._query_reads[query_ref])
                            cache[signature]=deepcopy((output,query_ref))
                            output={**output,'query_ref':query_ref}
                        return output
                    if not output.get('ok'):
                        from .read_queries import result as render_read
                        from .table_workflow import render as render_table
                        rendered = render_table(output) if output.get('table_tool') else render_read(output, datetime.fromisoformat(host._current['at']))
                        output = {**output, 'read_failure': True, 'complete': False, 'status': 'clarification',
                                  'message': rendered.message,
                                  'scope': output.get('scope') or {'domain':'table','table_id':parameters.get('table_id')}}
                    remembered = remember(host, output, datetime.fromisoformat(host._current['at']), ['query', len(host.trace), index])
                    query_ref = remembered['query_ref']
                    output=deepcopy(host._query_reads[query_ref])
                    cache[signature] = deepcopy((output, query_ref))
            outputs.append({**deepcopy(output), 'query_ref': query_ref})
            emit('query_source_result', source=source.source, source_index=index, query_ref=ref(query_ref), result=outcome(output))
            if plan.sql is not None and output.get('ok') and output.get('complete'):
                relation = relational.materialize(host, query_ref)
                relation['sql_table'] = source.alias
                relations.append(relation)
        if plan.sql is None:
            # Keep raw records in the current-request registry, not repeated
            # inside the planner's batch output.
            results = [{k: deepcopy(v) for k, v in row.items() if k not in {'analysis_records', 'calendar_candidates'}} for row in outputs]
            return {'read_batch': True, 'ok': all(v.get('ok') for v in outputs),
                    'complete': all(v.get('complete') for v in outputs), 'results': results,
                    'read_only': True, 'financial_write_authorized': False, 'calendar_write_authorized': False}
        if len(relations) != len(plans):
            failed=[(i,v) for i,v in enumerate(outputs) if not v.get('ok') or not v.get('complete')]
            diagnostics=[{'source_index':i,'error_code':v.get('error_code'),
                'recovery':deepcopy(v.get('recovery',{}))} for i,v in failed]
            if failed and all(v.get('recovery',{}).get('kind')=='assistant_arguments' for _,v in failed):
                fields=list(dict.fromkeys(f for _,v in failed for f in v['recovery'].get('invalid_fields',[])))
                return {**base,'ok':False,'complete':False,'status':'clarification',
                    'query_refs':[v['query_ref'] for _,v in failed],
                    'error_code':failed[0][1].get('error_code') or 'query_source_arguments',
                    'source_errors':diagnostics,
                    'message':'พารามิเตอร์อ่านข้อมูลยังใช้ไม่ได้ครับ',
                    'recovery':recovery(kind='assistant_arguments',fields=fields,tools=['act'])}
            return {**base, 'ok': False, 'complete': False, 'status': 'clarification',
                    'query_refs': [v['query_ref'] for v in outputs if not v.get('ok') or not v.get('complete')],
                    'error_code': 'query_incomplete_source',
                    'source_errors':diagnostics,
                    'message': 'ข้อมูลต้นทางยังไม่ครบหรือยังมีเงื่อนไขที่ต้องยืนยัน จึงยังคำนวณผลทั้งหมดไม่ได้ครับ',
                    'recovery': recovery(kind='unavailable', tools=[NAME, 'finish_query_analysis'])}
        with span('query_sql'):
            return relational.execute(host, {'query_refs': [v['query_ref'] for v in outputs],
                'sql': plan.sql, 'source_refs': plan.source_refs, 'continue_analysis': plan.continue_analysis}, relations=relations)
    except (PlanningError, ValidationError, ValueError) as error:
        code = str(error) if isinstance(error, PlanningError) else 'query_invalid_arguments'
        return {**base, 'ok': False, 'complete': False, 'status': 'clarification', 'error_code': code,
                'message': 'แผนค้นหาหรือแหล่งข้อมูลยังใช้ไม่ได้ครับ',
                'recovery': recovery(error, kind=None if isinstance(error, PlanningError) else 'assistant_arguments', tools=[NAME])}
