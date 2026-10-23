"""Several independent typed reads in one planner round trip, no write tools."""
from copy import deepcopy
from .history_context import digest
from .tool_errors import recovery

NAME='query_sources'


def definition(offered):
    variants=[]
    for tool in offered:
        if tool['name'] not in {'query_expenses','query_calendar','query_table'}:continue
        variants.append({'type':'object','additionalProperties':False,'properties':{
            'tool':{'type':'string','enum':[tool['name']]},'arguments':deepcopy(tool['parameters'])},
            'required':['tool','arguments']})
    return {'type':'function','name':NAME,'strict':True,'description':
        'Batch independent saved-data reads in one planning step, with continue_analysis=true. No writes or dependent refs. Each member retains its own completeness and query_ref; repair failed members before finishing. period_text copies a contiguous original owner span; a coordinated phrase may be cited in full for each member, never reconstructed from separated words.',
        'parameters':{'type':'object','additionalProperties':False,'properties':{
            'reads':{'type':'array','minItems':1,'maxItems':8,'items':{'anyOf':variants}}},'required':['reads']}}


def execute(host,args):
    from .query_analysis import remember
    from .read_queries import Expenses,Calendar
    from .table_workflow import TableQuery
    from .action_trace import emit,code
    import json
    models={'query_expenses':Expenses,'query_calendar':Calendar,'query_table':TableQuery}
    offered={t['name'] for t in host.state.raw_tool_definitions or []}
    try:
        if not isinstance(args,dict) or set(args)!={'reads'} or not isinstance(args['reads'],list) or not 1<=len(args['reads'])<=8:raise ValueError()
        plans=[];seen=set()
        # Validate the entire batch before any read. Query refs are request-local
        # dependencies; a batch cannot consume its own not-yet-known output.
        for row in args['reads']:
            if not isinstance(row,dict) or set(row)!={'tool','arguments'} or row['tool'] not in offered or row['tool'] not in models:raise ValueError()
            p=models[row['tool']].model_validate(row['arguments'])
            if not p.continue_analysis or getattr(p,'for_change',False) or getattr(p,'calendar_query_refs',[]) or getattr(p,'matching_query_ref',None):raise ValueError()
            host._owner_sources(p.source_refs)
            if host._current['source_ref'] not in p.source_refs:raise ValueError()
            key=digest(row)
            if key in seen:raise ValueError()
            seen.add(key);plans.append(row)
    except (ValueError,TypeError,KeyError):
        return {'read_batch':True,'ok':False,'error_code':'invalid_read_batch',
                'recovery':recovery(kind='assistant_arguments',tools=[NAME,'query_expenses','query_calendar','query_table'])}
    results=[]
    for index,row in enumerate(plans):
        output=host.tool({'name':row['tool'],'arguments':json.dumps(row['arguments'],ensure_ascii=False)})
        emit('batch_read',tool=code(row['tool']),status='completed' if output.get('ok') else 'failed')
        if output.get('scope') or output.get('table_tool'):
            from datetime import datetime
            if not output.get('ok'):
                from .read_queries import result as render_read
                from .table_workflow import render as render_table
                rendered=render_table(output) if output.get('table_tool') else render_read(output,datetime.fromisoformat(host._current['at']))
                output={**output,'read_failure':True,'complete':False,'status':'clarification','message':rendered.message,
                        'scope':output.get('scope') or {'domain':'table','table_id':row['arguments']['table_id']}}
            output=remember(host,output,datetime.fromisoformat(host._current['at']),['batch',len(host.trace),index])
        results.append(output)
    return {'read_batch':True,'ok':all(v.get('ok') for v in results),
            'complete':all(v.get('ok') and v.get('complete') for v in results),'results':results,
            'planning_note':'Repair failed members using the original owner spans before comparing. A failed member remains incomplete evidence, never zero or an omitted scope.',
            'read_only':True,'financial_write_authorized':False,'calendar_write_authorized':False}
