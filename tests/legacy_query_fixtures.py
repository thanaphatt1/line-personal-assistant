"""Express legacy reader regression scenarios through the unified planner API.

Old fixture builders deliberately exercise legacy reader parameters, which the
server handlers still accept. New unified-query tests supply the public compact
schema directly. Production code never translates unoffered model tool names.
"""
from copy import deepcopy
import json

from line_assistant.history_context import digest


def adapt_response(body, value):
    tools={t['name'] for t in body.get('tools', [])}
    if not tools & {'query','act'}:
        return value
    value = deepcopy(value)
    for call in value.get('output', []):
        name = call.get('name')
        if call.get('type')=='function_call' and 'act' in tools and name in {'query','inspect_data_sources'}:
            args=json.loads(call['arguments'])
            call.update(name='act',arguments=json.dumps({'request':{'intent':'read' if name=='query' else 'inspect',**args}},ensure_ascii=False))
            continue
        if call.get('type') != 'function_call' or name not in {'query_expenses', 'query_calendar', 'query_relations', 'query_sources'}:
            continue
        args = json.loads(call['arguments'])
        current = json.loads(body['input'][0]['content'])['current_source_ref']
        def source(tool, parameters, alias):
            return {'source': {'query_expenses': 'expense', 'query_calendar': 'calendar', 'query_table': 'table'}.get(tool, 'invalid'),
                    'alias': alias, 'parameters': deepcopy(parameters)}
        if name in {'query_expenses', 'query_calendar'}:
            plan = {'sources': [source(name, args, 'source')], 'sql': None,
                    'source_refs': args.get('source_refs', [current]), 'continue_analysis': args.get('continue_analysis', False)}
        elif name == 'query_relations':
            plan = {'sources': [{'source': 'result', 'alias': 'q_' + digest(ref)[:16], 'query_ref': ref}
                                for ref in args.get('query_refs', [])],
                    'sql': args.get('sql'), 'source_refs': args.get('source_refs', []),
                    'continue_analysis': args.get('continue_analysis', False)}
        else:
            plan = {'sources': [source(row.get('tool'), row.get('arguments', {}), 'source_' + str(index))
                                for index, row in enumerate(args.get('reads', []))],
                    'sql': None, 'source_refs': [current], 'continue_analysis': True}
        call.update(name='act' if 'act' in tools else 'query', arguments=json.dumps(
            {'request':{'intent':'read',**plan}} if 'act' in tools else plan, ensure_ascii=False))
    return value
