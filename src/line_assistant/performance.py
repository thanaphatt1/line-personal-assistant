"""Aggregate sanitized action traces locally; no provider or business data reads."""
from collections import Counter,defaultdict
import math


def percentile(values, fraction):
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*fraction)-1)] if values else None


def summarize(records,*,include_replays=False):
    # Webhook scope includes delivery. Prefer it over the nested assistant row.
    attempts={}
    for record in records:
        row=record.get('jsonPayload',record) if isinstance(record,dict) else {}
        if (row.get('event')!='assistant_action_trace' or row.get('phase') not in
                {'turn_finished','webhook_finished','turn_interrupted'} or not isinstance(row.get('performance'),dict)):
            continue
        key=row.get('attempt_ref')
        if not isinstance(key,str) or not key:continue
        old=attempts.get(key)
        if old is None or row['phase']=='webhook_finished':attempts[key]=row
    timings=defaultdict(list);statuses=Counter();calls=Counter();fallback_turns=0
    scopes=Counter();replays=0
    def valid(value):return type(value) is int and 0<=value<=86_400_000
    for row in attempts.values():
        perf=row['performance'];total=perf.get('total_ms')
        if not valid(total):continue
        if perf.get('replayed') is True:
            replays+=1
            if not include_replays:continue
        timings['total'].append(total);scopes['webhook' if row['phase']=='webhook_finished' else 'assistant']+=1
        status=row.get('status','interrupted' if row['phase']=='turn_interrupted' else 'unknown')
        from .action_trace import code
        statuses[code(status)]+=1
        for name,value in perf.get('phase_ms',{}).items():
            if valid(value):timings[code(name)].append(value)
        for name,value in perf.get('calls',{}).items():
            if valid(value):calls[code(name)]+=value
        if valid(perf.get('reply_fallbacks')) and perf['reply_fallbacks']>0:fallback_turns+=1
    count=len(timings['total'])
    return {'attempts':count,'replay_attempts':replays,'includes_replays':include_replays,'scopes':dict(scopes),'statuses':dict(statuses),'calls':dict(calls),
        'reply_fallback_turns':fallback_turns,'reply_fallback_rate':fallback_turns/count if count else None,
        'latency_ms':{name:{'samples':len(values),'p50':percentile(values,.5),'p95':percentile(values,.95)}
                      for name,values in sorted(timings.items())},
        'note':'Phase durations may overlap. Webhook includes reply delivery; assistant does not. Older traces may lack replay markers.'}
