"""Independent synthetic outcome oracle. No SQL, planner review or LLM calls.

Gold labels are reviewer input, never runtime/model context. Passing this oracle
proves only the tested observations on these fixtures, not arbitrary SQL meaning.
"""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from datetime import date,datetime
from zoneinfo import ZoneInfo
import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def cell(value):
    """Exact numeric equivalence, with text and null kept distinct."""
    if value is None:return ('null','')
    if isinstance(value,bool):return ('bool',str(value))
    try:
        number=Decimal(str(value))
        if number.is_finite():return ('number',str(number.normalize()))
    except InvalidOperation:pass
    return ('text',str(value))


def rows_key(rows, *, ordered=False):
    # Aliases and projection order are not semantic labels. Repeated equal rows
    # remain repeated; field values/types and per-row association are preserved.
    normalized=[tuple(sorted(cell(v) for v in (row.values() if isinstance(row,dict) else row))) for row in rows]
    return normalized if ordered else Counter(normalized)


def calendar_date(event):
    """Normalize native Calendar start values without requiring a SQL projection."""
    value=event.get('start')
    if not isinstance(value,str):return None
    try:
        if len(value)==10:return date.fromisoformat(value).isoformat()
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        return stamp.astimezone(ZoneInfo('Asia/Bangkok')).date().isoformat() if stamp.utcoffset() is not None else None
    except ValueError:return None


def expected_rows(fixtures, gold):
    """Compute labels with Decimal/Python only; never execute candidate SQL."""
    rows=fixtures[gold['domain']]
    start,end=gold['period']
    rows=[r for r in rows if start<=r['date']<=end]
    if 'event_title' in gold:
        days={r['date'] for r in fixtures['calendar'] if start<=r['date']<=end and r['title']==gold['event_title']}
        rows=[r for r in rows if r['date'] in days]
    for key,value in gold.get('where',{}).items():rows=[r for r in rows if r[key]==value]
    for key,values in gold.get('exclude',{}).items():rows=[r for r in rows if r[key] not in values]
    operation=gold['operation']
    if operation=='count':return [[len(rows)]]
    if operation=='sum':return [[str(sum((Decimal(r['amount']) for r in rows),Decimal(0)))]]
    if operation=='rows':
        for key,direction in reversed(gold.get('sort',[])):rows=sorted(rows,key=lambda r:r[key],reverse=direction=='desc')
        return [[r[k] for k in gold['fields']] for r in rows[:gold.get('limit',len(rows))]]
    if operation=='maximum':
        if not rows:return []
        maximum=max(Decimal(r['amount']) for r in rows)
        return [[r[k] for k in gold['fields']] for r in rows if Decimal(r['amount'])==maximum]
    groups=defaultdict(list)
    for row in rows:groups[row[gold['group_by']]].append(row)
    if operation=='group_sum':result=[[k,str(sum((Decimal(r['amount']) for r in v),Decimal(0)))] for k,v in groups.items()]
    elif operation=='frequency':result=[[k,len(v)] for k,v in groups.items()]
    else:raise ValueError('unknown gold operation')
    result.sort(key=lambda r:(-Decimal(r[1]),r[0]))
    return result[:gold.get('limit',len(result))]


def score_turn(fixtures, gold, observation):
    """Four independent observations. None means unavailable/not applicable.

    Structural grounding is the server renderer's evidence, not an independent
    prose/style judgment. An answer must have a successful *reported* result;
    unrelated earlier successful reads cannot satisfy the outcome oracle.
    """
    actual=observation['result'];data=actual.get('data',{});output=data.get('tool_result',{})
    status=actual['status'];kind=gold['kind'];reported=observation.get('reported_results',[])
    execution=None;outcome=None;grounding=None
    if kind=='query':
        execution=bool(status=='answer' and output.get('ok') is True and output.get('complete') is True and reported and all(r.get('ok') is True and r.get('complete') is True for r in reported))
        wanted=expected_rows(fixtures,gold)
        result_rows=[]
        for result in reported:
            if 'rows' in result:result_rows.extend(result['rows'])
            elif 'events' in result:
                if gold['operation']=='count':
                    count=result.get('count')
                    if type(count) is int and count>=0:result_rows.append([count])
                else:
                    result_rows.extend([{k:calendar_date(r) if k=='date' else r.get(k) for k in gold['fields']} for r in result['events']])
        outcome=bool(execution and rows_key(result_rows,ordered=gold.get('ordered',False))==rows_key(wanted,ordered=gold.get('ordered',False)))
        realization=data.get('response_realization',{})
        grounding=bool(execution and observation.get('grounded_document') is True and observation.get('delivered') is True) if realization.get('mode')=='planner_intent' else None
    elif kind in {'clarification','unavailable'}:
        execution=bool(status=='clarification' and output.get('complete') is not True)
        if kind=='clarification':execution=execution and output.get('query_clarification') is True
        if 'error_code' in gold:execution=execution and output.get('error_code')==gold['error_code']
        if gold.get('provider_fault_required'):execution=execution and observation.get('provider_faults',0)>0
        outcome=None if kind=='clarification' else execution
        # Free prose/clarification quality needs human review, no automatic pass.
    else:
        execution=status in gold['statuses'];outcome=execution
        if 'calendar_preview' in gold:
            view=data.get('calendar_view',{})
            preview={'title':view.get('facts',{}).get('title'),'start':view.get('start'),'end':view.get('end')}
            outcome=bool(execution and preview==gold['calendar_preview'])
    before=observation['before'];after=observation['after']
    delta_calendar=[r for k,r in after['calendar'].items() if k not in before['calendar']]
    same_existing=all(after['calendar'].get(k)==v for k,v in before['calendar'].items())
    expected_effects=gold.get('calendar_added',[])
    effects=bool(same_existing and before['expense']==after['expense'] and before['catalog']==after['catalog'] and rows_key(delta_calendar)==rows_key(expected_effects) and observation['write_delta']==gold.get('writes',0))
    if kind=='replay':effects=effects and observation['model_calls']==0 and observation['provider_reads']==0
    return {'intent_outcome':outcome,'execution_contract':execution,'structural_grounding':grounding,
            'authorized_effects':effects,'human_answer_review':'pending'}


def aggregate(attempts):
    """Scenario counts and repeated attempts stay separate; NA is never a zero."""
    dimensions=['intent_outcome','execution_contract','structural_grounding','authorized_effects']
    result={}
    for name in dimensions:
        values=[t['scores'][name] for a in attempts for t in a['turns']]
        result[name]={'pass':sum(v is True for v in values),'fail':sum(v is False for v in values),
                      'not_applicable':sum(v is None for v in values)}
    result['scenario_count']=len({a['scenario_id'] for a in attempts});result['attempts']=len(attempts)
    result['failed_attempts']=sum(a.get('status')=='failed' for a in attempts)
    result['turns']=sum(len(a['turns']) for a in attempts)
    scenario_pass=defaultdict(list)
    for attempt in attempts:
        scenario_pass[attempt['scenario_id']].append(attempt.get('status')!='failed' and bool(attempt['turns']) and all(v is not False for t in attempt['turns'] for k,v in t['scores'].items() if k!='human_answer_review'))
    result['scenarios_all_observed_checks_passed']=sum(all(values) for values in scenario_pass.values())
    result['human_answer_review']='pending'
    result['realization_modes']=dict(Counter(t['observation'].get('realization_mode','unknown') for a in attempts for t in a['turns']))
    return result
