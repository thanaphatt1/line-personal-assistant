"""Aggregate redacted traces by observed workflow, revision and boundary.

This reports execution evidence, never classifies owner prose or contacts a
provider. Unknown metadata and incomplete trace exports stay explicit.
"""
from collections import Counter, defaultdict
import math
import re

TERMINALS={'turn_finished':1,'turn_interrupted':2,'webhook_finished':3}


def percentile(values, fraction):
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*fraction)-1)] if values else None


def _valid(value):
    return type(value) is int and 0<=value<=86_400_000


def _revision(record):
    resource=record.get('resource');labels=resource.get('labels') if isinstance(resource,dict) else None
    value=labels.get('revision_name') if isinstance(labels,dict) else None
    return value if isinstance(value,str) and re.fullmatch(r'[a-z][a-z0-9-]{0,62}',value) else None


def _codes(value):
    from .action_trace import code
    if not isinstance(value,dict):return {}
    return {code(k):v for k,v in value.items() if _valid(v)}


def _distribution(values):
    return {'samples':len(values),'p50':percentile(values,.5),'p95':percentile(values,.95)}


def _aggregate(items):
    from .action_trace import code
    timings=defaultdict(list);calls=Counter();statuses=Counter();scopes=Counter()
    model=[];provider=[];fallbacks=0;fallback_samples=0;repairs=[];stops=Counter();reasons=Counter();failures=Counter()
    dropped=0;sink_failures=0;diagnostics_complete=0;workflow_basis=Counter()
    tokens=Counter()
    for item in items:
        row=item['terminal'];perf=row['performance'];timings['total'].append(perf['total_ms'])
        scopes[item['boundary']]+=1;statuses[code(row.get('status','unknown'))]+=1
        workflow_basis[item['basis']]+=1
        usage=perf.get('token_usage')
        if isinstance(usage,dict):
            for name in ('responses','input_tokens','output_tokens','cached_input_tokens','cache_write_tokens','reasoning_tokens',
                         'input_tokens_samples','output_tokens_samples','cached_input_tokens_samples','cache_write_tokens_samples','reasoning_tokens_samples'):
                if _valid(usage.get(name)):tokens[name]+=usage[name]
        phases=perf.get('phase_ms');phases=phases if isinstance(phases,dict) else {}
        for name,value in phases.items():
            if _valid(value):timings[code(name)].append(value)
        call_map=perf.get('calls')
        if isinstance(call_map,dict):
            for name,value in call_map.items():
                if _valid(value):calls[code(name)]+=value
            if _valid(call_map.get('model',0)):model.append(call_map.get('model',0))
            rpc=[v for k,v in call_map.items() if isinstance(k,str) and k.startswith('provider_')]
            if all(_valid(v) for v in rpc):provider.append(sum(rpc))
        if _valid(perf.get('reply_fallbacks')):
            fallback_samples+=1
            if perf['reply_fallbacks']>0:fallbacks+=1
        drops=row.get('dropped_records',0);sink=row.get('sink_failures',0)
        if _valid(drops) and drops:dropped+=1
        if _valid(sink) and sink:sink_failures+=1
        complete=_valid(perf.get('repairs')) and all(isinstance(perf.get(k),dict) and all(_valid(v) for v in perf[k].values()) for k in ['stop_reasons','fallback_reasons','model_failures'])
        if complete:
            count=perf['repairs'];stop=perf['stop_reasons'];reason=perf['fallback_reasons'];failure=perf['model_failures']
        elif not drops and not sink:
            count=item['repairs'];stop=item['stops'];reason=item['fallbacks'];failure=item['failures']
        else:
            count=None;stop=reason=failure={}
        if complete or not drops and not sink:diagnostics_complete+=1
        if _valid(count):repairs.append(count)
        stops.update(_codes(stop));reasons.update(_codes(reason));failures.update(_codes(failure))
    n=len(items)
    return {'attempts':n,'scopes':dict(scopes),'statuses':dict(statuses),'calls':dict(calls),
        'reply_fallback_turns':fallbacks,'reply_fallback_samples':fallback_samples,'reply_fallback_rate':fallbacks/fallback_samples if fallback_samples else None,
        'latency_ms':{name:_distribution(values) for name,values in sorted(timings.items())},
        'model_calls_per_turn':_distribution(model),'provider_rpcs_per_turn':_distribution(provider),
        'repair_calls_per_turn':_distribution(repairs),'stop_reasons':dict(stops),
        'fallback_reasons':dict(reasons),'model_failure_reasons':dict(failures),
        'trace_dropped_attempts':dropped,'trace_sink_failure_attempts':sink_failures,
        'diagnostics_complete_attempts':diagnostics_complete,'workflow_basis':dict(workflow_basis),
        'token_usage':{**{name:tokens[name] if tokens[name+'_samples'] else None for name in ('input_tokens','output_tokens','cached_input_tokens','cache_write_tokens','reasoning_tokens')},
            **{name+'_samples':tokens[name+'_samples'] for name in ('input_tokens','output_tokens','cached_input_tokens','cache_write_tokens','reasoning_tokens')},
            'response_metadata_samples':tokens['responses'],
            'model_calls_without_input_usage':max(sum(model)-tokens['input_tokens_samples'],0) if len(model)==n else None},
        'usage_cost':'unavailable_without_versioned_pricing_and_billing_context; token samples may be partial; not zero or an invoice'}


def summarize(records,*,include_replays=False,revision=None,workflow=None,boundary=None):
    from .action_trace import WORKFLOWS, workflow_labels, workflow_name, code
    if revision is not None and not re.fullmatch(r'[a-z][a-z0-9-]{0,62}',revision):raise ValueError('revision')
    if workflow is not None and workflow not in WORKFLOWS:raise ValueError('workflow')
    if boundary not in {None,'assistant','webhook','interrupted'}:raise ValueError('boundary')
    attempts={}
    for record in records:
        if not isinstance(record,dict):continue
        row=record.get('jsonPayload',record)
        if not isinstance(row,dict) or row.get('event')!='assistant_action_trace':continue
        key=row.get('attempt_ref')
        if not isinstance(key,str) or not key:continue
        item=attempts.setdefault(key,{'terminal':None,'revisions':set(),'labels':set(),'seen':set(),
            'repairs':0,'stops':Counter(),'fallbacks':Counter(),'failures':Counter()})
        rev=_revision(record)
        if rev:item['revisions'].add(rev)
        phase=row.get('phase')
        if not isinstance(phase,str):continue
        sequence=row.get('sequence')
        signature=(sequence,phase)
        duplicate=_valid(sequence) and signature in item['seen']
        if _valid(sequence):item['seen'].add(signature)
        if not duplicate:
            if phase=='tool_request':item['labels'].update(workflow_labels(row.get('tool'),row.get('arguments')))
            if phase=='checkpoint':item['labels'].update(workflow_labels(None,action_kind=row.get('action_kind')))
            if phase=='planning_repair':item['repairs']+=1
            if phase=='planning_stopped':item['stops'][code(row.get('reason'))]+=1
            if phase=='reply_fallback':item['fallbacks'][code(row.get('reason'))]+=1
            if phase=='model_result' and row.get('failure_kind'):item['failures'][code(row['failure_kind'])]+=1
        if phase not in TERMINALS or not isinstance(row.get('performance'),dict):continue
        rank=(TERMINALS[phase],sequence if _valid(sequence) else -1)
        if item['terminal'] is None or rank>item['rank']:
            item.update(terminal=row,rank=rank)
    selected=[];replays=0;incomplete=0;invalid=0
    for item in attempts.values():
        row=item['terminal']
        if row is None:incomplete+=1;continue
        perf=row['performance']
        if not _valid(perf.get('total_ms')):invalid+=1;continue
        rev=next(iter(item['revisions'])) if len(item['revisions'])==1 else 'unknown'
        observed=perf.get('workflow')
        if isinstance(observed,str) and observed in WORKFLOWS:
            name=observed;basis='terminal' if name!='unknown' else 'unknown'
        elif row.get('dropped_records',0) or row.get('sink_failures',0):name='unknown';basis='unknown'
        else:name=workflow_name(item['labels']);basis='inferred_events' if name!='unknown' else 'unknown'
        scope='webhook' if row['phase']=='webhook_finished' else 'interrupted' if row['phase']=='turn_interrupted' else 'assistant'
        if revision is not None and rev!=revision or workflow is not None and name!=workflow or boundary is not None and scope!=boundary:continue
        if perf.get('replayed') is True:
            replays+=1
            if not include_replays:continue
        item.update(revision=rev,workflow=name,boundary=scope,basis=basis)
        selected.append(item)
    groups=defaultdict(list)
    for item in selected:groups[(item['revision'],item['workflow'],item['boundary'])].append(item)
    result=_aggregate(selected)
    result.update(replay_attempts=replays,includes_replays=include_replays,
        incomplete_attempts=incomplete,invalid_terminal_attempts=invalid,
        groups=[{'revision':rev,'workflow':name,'boundary':scope,**_aggregate(items)}
                for (rev,name,scope),items in sorted(groups.items())],
        note='Workflows describe observed tools/sources, not proven owner intent. Phase durations overlap. Cold start, tokens and cost are not inferred. Samples with missing metadata remain unknown. Incomplete/invalid counts describe the whole input before filters.')
    return result


def markdown(report):
    """Small shareable aggregate; never include event/attempt IDs or payloads."""
    lines=['| Revision | Workflow | Boundary | n | p50 ms | p95 ms | Model calls p50/p95 | Fallback rate |',
           '|---|---|---|---:|---:|---:|---:|---:|']
    def show(value):return 'unknown' if value is None else str(value)
    for group in report['groups']:
        latency=group['latency_ms'].get('total',{});calls=group['model_calls_per_turn']
        rate=group['reply_fallback_rate']
        lines.append('| '+' | '.join([group['revision'],group['workflow'],group['boundary'],str(group['attempts']),
            show(latency.get('p50')),show(latency.get('p95')),show(calls['p50'])+'/'+show(calls['p95']),
            'unknown' if rate is None else f'{rate:.1%}'])+' |')
    lines+=['',f"Incomplete attempts: {report['incomplete_attempts']}; dropped-trace attempts: {report['trace_dropped_attempts']}.",
            'Observed workflow labels do not establish semantic correctness. Cold start and billed cost are unknown.']
    return '\n'.join(lines)+'\n'
