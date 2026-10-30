"""Model-selected appointments with typed temporal values and owner quotations.

No clause splitter, keyword router, shared-field propagation or title rewriting.
Semantic selection is the model's responsibility; evidence, versions, provider
limits, review delivery and confirmation remain server responsibilities.
"""
from copy import deepcopy
from datetime import timedelta
from typing import Literal
from pydantic import Field
from .tables import Strict
from .tool_errors import PlanningError
from .history_context import digest
from .calendar_workflow import FIELDS,TEMPORAL,calculate
from .temporal import interpreted_evidence


FieldName=Literal['title','date_text','time_text','end_date_text','end_time_text','duration_text','all_day']

class Quote(Strict):
    source_ref:str=Field(min_length=1,max_length=64)
    text:str=Field(min_length=1,max_length=2000)
    occurrence:int=Field(default=0,ge=0,le=1999)

class FieldQuote(Quote):
    text:str=Field(min_length=1,max_length=200)

class ModeQuote(Quote):
    text:str=Field(min_length=1,max_length=100)

class TextFact(Strict):
    value:str=Field(min_length=1,max_length=200)
    source:FieldQuote

class DurationFact(Strict):
    value:int=Field(ge=1,le=10080,description='Elapsed minutes interpreted from the cited owner wording, never an inferred default or an endpoint clock.')
    source:FieldQuote

class ModeFact(Strict):
    value:bool
    source:ModeQuote

class Facts(Strict):
    title:TextFact|None=Field(default=None,description='Complete literal event name, including its qualifiers. Remove only the request to create it; do not shorten the name or reinterpret parts of it as scheduling fields.')
    date_text:TextFact|None=Field(default=None,description='ISO YYYY-MM-DD at the source timestamp.')
    time_text:TextFact|None=Field(default=None,description='Bangkok HH:MM start.')
    end_date_text:TextFact|None=Field(default=None,description='Explicit ISO end date.')
    end_time_text:TextFact|None=Field(default=None,description='Explicit HH:MM endpoint.')
    duration_text:DurationFact|None=None
    all_day:ModeFact|None=Field(default=None,description='Explicit all-day mode; missing clocks do not imply true.')

class Event(Strict):
    draft_ref:str|None=None
    draft_version:int|None=Field(default=None,ge=1)
    source:Quote|None=None  # Pre-release compatibility; current tools use field quotations.
    source_refs:list[str]=Field(min_length=1,max_length=16,description='Current intent and original fact sources.')
    fields:Facts
    uncertain_fields:list[FieldName]=Field(default_factory=list,max_length=7,description='Unresolved meanings; retain incomplete members.')
    approximate_fields:list[Literal['date_text','time_text','end_date_text','end_time_text','duration_text']]=Field(default_factory=list,max_length=5)
    unsupported_features:list[Literal['recurrence','attendees','reminders']]=Field(default_factory=list,max_length=3)

class Batch(Strict):
    batch_ref:str|None=None
    batch_version:int|None=Field(default=None,ge=1)
    events:list[Event]=Field(min_length=1,max_length=8)


DESCRIPTION=('Prepare ALL requested appointment occurrences for owner review, including incomplete ones; never writes. '
    'You select members and shared/per-event facts. New refs/versions are null. Keep complete literal titles. '
    'Interpret explicit date/clock components directly in fields with original owner quotations. Anchor relative wording at its source timestamp. '
    'Use resolve_temporal when arithmetic or a verified basis is needed. Apply shared facts to each intended member. Null retains facts or remains missing; no defaults. '
    'Mark unresolved meanings and unsupported recurrence/guests/reminders. Updates use offered refs/versions; omitted members remain.')

def compact_schema(model):
    """Keep local JSON Schema refs so repeated quotation shapes cost less."""
    def clean(value):
        if isinstance(value,list):return [clean(v) for v in value]
        if not isinstance(value,dict):return value
        out={k:({name:clean(child) for name,child in v.items()} if k in {'properties','$defs'} else clean(v))
             for k,v in value.items() if k not in {'title','default'}}
        if out.get('type')=='object':
            if {'fields','source_refs'}<=out.get('properties',{}).keys():out['properties'].pop('source',None)
            out['additionalProperties']=False;out['required']=list(out.get('properties',{}))
        return out
    result=clean(model.model_json_schema());defs=result.get('$defs',{});needed=set()
    def refs(value):
        if isinstance(value,list):
            for child in value:refs(child)
        elif isinstance(value,dict):
            if '$ref' in value:
                key=value['$ref'].rsplit('/',1)[-1]
                if key not in needed:needed.add(key);refs(defs[key])
            for key,child in value.items():
                if key!='$defs':refs(child)
    refs(result)
    if defs:result['$defs']={k:v for k,v in defs.items() if k in needed}
    return result

def definitions():
    return [{'type':'function','name':name,'strict':True,'description':DESCRIPTION,'parameters':compact_schema(model)}
            for name,model in [('prepare_calendar_event',Event),('prepare_calendar_events',Batch)]]


def quoted(h,q,field):
    row=h._owner_sources([q.source_ref])[0]
    starts=[i for i in range(len(row['text'])) if row['text'].startswith(q.text,i)]
    if q.occurrence>=len(starts):raise PlanningError('calendar_quote_not_found',fields=[field])
    start=starts[q.occurrence]
    return {**row,'text':q.text,'span':[start,start+len(q.text)]}


def blank(flow,ref):
    at=flow.h._current['at']
    return {'draft_ref':ref,'owner_id':flow.h.settings.owner_id,'calendar_id':flow.calendar_id,'calendar_label':flow.label,
        'version':1,'status':'needs_information','facts':dict.fromkeys(FIELDS),'fact_sources':{},'anchors':{},'source_refs':[],
        'created_at':at,'updated_at':at,'expires_at':(flow.h.clock()+timedelta(seconds=flow.h.settings.pending_ttl_seconds)).isoformat(),
        'start':None,'end':None,'missing_fields':[],'issues':[],'review':None}


def update(flow,p,old=None):
    h=flow.h
    h._owner_sources(p.source_refs)
    if h._current['source_ref'] not in p.source_refs:raise PlanningError('calendar_current_source_required')
    if p.unsupported_features:
        error=PlanningError('calendar_timed_single_creation_only',kind='unsupported')
        error.unsupported_features=list(dict.fromkeys(p.unsupported_features));raise error
    group=quoted(h,p.source,'source') if p.source else None
    if p.source and p.source.source_ref not in p.source_refs:raise PlanningError('calendar_source_required')
    if old:
        if old['status']=='committing' or h._blocked_action():raise PlanningError('calendar_action_pending')
        if p.draft_version!=old['version']:raise PlanningError('calendar_stale_draft_version')
        d=deepcopy(old);d.update(version=old['version']+1,updated_at=h._current['at'],review=None)
    else:
        if p.draft_version is not None:raise PlanningError('calendar_unbound_version')
        ref='cal_'+digest([h.settings.owner_id,flow.calendar_id,h._active_event_id])[:20]
        d=blank(flow,ref)
    evidence={}
    for field in [*FIELDS,'all_day']:
        fact=getattr(p.fields,field)
        if fact is None:continue
        if fact.source.source_ref not in p.source_refs:raise PlanningError('calendar_source_required',fields=[field])
        row=quoted(h,fact.source,field)
        if field=='title':
            if fact.value not in row['text']:raise PlanningError('calendar_title_source',fields=[field])
            evidence[field]={**row,'value':fact.value}
        elif field=='all_day':
            if len(row['text'])>100:raise PlanningError('calendar_mode_quote_too_long',fields=[field])
            evidence[field]={**row,'value':fact.value}
        else:evidence[field]=interpreted_evidence(h,field,fact.value,row)
    group=group or evidence.get('title') or next(iter(evidence.values()),{**h._current,'span':[0,len(h._current['text'])]})
    return apply(flow,d,evidence,p.source_refs,p.uncertain_fields,p.approximate_fields),group


def apply(flow,d,evidence,refs,uncertain=(),approximate=()):
    """Apply only selected, certified fields. Never infer sibling membership."""
    clear=[]
    if 'duration_text' in evidence and 'end_time_text' not in evidence:clear=['end_time_text','end_date_text']
    elif 'end_time_text' in evidence and 'duration_text' not in evidence:clear=['duration_text']
    for field in clear:
        d['facts'][field]=None;d['fact_sources'].pop(field,None);d['anchors'].pop(field,None)
    mode=evidence.get('all_day')
    if mode and (type(mode['value']) is not bool or len(mode['text'])>100):
        raise PlanningError('calendar_mode_quote_too_long',fields=['all_day'])
    if mode and mode['value']!=d.get('all_day',False):
        for field in ['time_text','end_time_text','duration_text','end_date_text']:
            d['facts'][field]=None;d['fact_sources'].pop(field,None);d['anchors'].pop(field,None)
    if mode:
        d['all_day']=mode['value'];d['all_day_anchor']={'value':mode['value'],'source_ref':mode['source_ref'],
            'literal':mode['text'],'at':mode['at']}
        d.pop('all_day_clock_source',None);d.pop('all_day_range_source',None)
    for field,row in evidence.items():
        if field=='all_day':continue
        if field=='title' and (not isinstance(row['value'],str) or not 1<=len(row['value'])<=200):
            raise PlanningError('calendar_title_source',fields=['title'])
        d['facts'][field]=row['value'];d['fact_sources'][field]=[row['source_ref']]
        d['anchors'][field]={'source_ref':row['source_ref'],'at':row['at'],'bare':False,
            **({'temporal':deepcopy(row['temporal'])} if row.get('temporal') else {})}
    previous=set(d.get('semantic_uncertain_fields',()))-set(evidence)
    d['semantic_uncertain_fields']=sorted(previous|set(uncertain))
    prior_approx=set(d.get('approximate_fields',()))-set(evidence)
    qualified=sorted((prior_approx|set(approximate))&set(TEMPORAL)&{f for f in FIELDS if d['facts'][f]})
    if qualified:d['approximate_fields']=qualified
    else:d.pop('approximate_fields',None)
    d['model_interpreted']=True
    d['source_refs']=list(dict.fromkeys([*d['source_refs'],*refs,*(v['source_ref'] for v in evidence.values())]))
    if len(d['source_refs'])>32:raise PlanningError('calendar_source_limit')
    d['start'],d['end'],d['missing_fields'],d['issues']=calculate(d)
    if not d['missing_fields'] and not d['semantic_uncertain_fields']:
        mechanical=set(d['issues'])&{'calendar_end_order','calendar_end_conflict','calendar_temporal_date_pair_required'}
        if mechanical:raise PlanningError(sorted(mechanical)[0],fields=['date_text','time_text','end_date_text','end_time_text','duration_text'])
    return d


def revoke(flow,old,batch=False):
    if old['status']=='committing' or flow.h._blocked_action():raise PlanningError('calendar_action_pending')
    if not old.get('review'):return
    value=deepcopy(old);value.update(version=old['version']+1,updated_at=flow.h._current['at'],status='needs_information',review=None)
    if batch:flow.h.state.calendar_batches[old['batch_ref']]=value;flow.batches.seal(value)
    else:flow.h.state.calendar_drafts[old['draft_ref']]=value;flow.seal(value)
    flow.h.state.checkpoint()


def prepare(flow,name,args):
    h=flow.h
    if name=='prepare_calendar_event':
        p=Event.model_validate(args)
        old=deepcopy(flow.bound(p)) if p.draft_ref else None
        if not old and sum(d['status'] not in {'recorded','cancelled'} for d in h.state.calendar_drafts.values())>=8:
            raise PlanningError('calendar_too_many_drafts')
        revoke(flow,old) if old else None
        d,_=update(flow,p,old)
        return flow.save_review(d)
    p=Batch.model_validate(args)
    old=deepcopy(flow.batches.bound(p.batch_ref,p.batch_version)) if p.batch_ref else None
    if old:revoke(flow,old,batch=True)
    elif p.batch_version is not None:raise PlanningError('calendar_unbound_version')
    at=h._current['at']
    b=deepcopy(old) if old else {'batch_ref':'calset_'+digest([h.settings.owner_id,flow.calendar_id,h._active_event_id])[:20],
        'owner_id':h.settings.owner_id,'calendar_id':flow.calendar_id,'version':0,'members':[],
        'created_at':at,'updated_at':at,'expires_at':(h.clock()+timedelta(seconds=h.settings.pending_ttl_seconds)).isoformat(),
        'status':'needs_information','review':None,'action_key':None}
    if b['status']=='committing' or h._blocked_action():raise PlanningError('calendar_action_pending')
    b.update(version=b['version']+1,updated_at=at,review=None,status='needs_information',model_interpreted=True)
    children={ref:deepcopy(flow.batches.child(ref)) for ref in b['members']};used=set()
    for event in p.events:
        if event.draft_ref:
            if event.draft_ref in used:raise PlanningError('calendar_event_selection')
            if event.draft_ref not in children:
                if old:raise PlanningError('calendar_event_selection')
                children[event.draft_ref]=deepcopy(flow.bound(event));b['members'].append(event.draft_ref)
            prior=children[event.draft_ref]
        else:prior=None
        child,group=update(flow,event,prior)
        if not prior or not child.get('source_unit'):
            index=b['members'].index(child['draft_ref']) if prior else len(b['members'])
            if index>=8:raise PlanningError('calendar_event_capacity')
            unit={'source_ref':group['source_ref'],'start':group['span'][0],'end':group['span'][1],
                'text':group['text'],'at':group['at'],'occurrence':index}
            unit['slot_key']=digest([unit['source_ref'],unit['start'],unit['end'],index])[:20]
            ref=child['draft_ref'] if prior else 'cal_'+digest([b['batch_ref'],unit['slot_key']])[:20]
            child.update(draft_ref=ref,source_unit=unit)
            if not prior:b['members'].append(ref)
        ref=child['draft_ref'];used.add(ref)
        child.update(batch_ref=b['batch_ref'],ordinal=b['members'].index(ref)+1)
        children[ref]=child
    if len(b['members'])>8:raise PlanningError('calendar_event_capacity')
    if not old and len([v for v in h.state.calendar_batches.values() if v['status'] not in {'recorded','cancelled'}])>=8:
        raise PlanningError('calendar_too_many_drafts')
    flow.batches._publish(b,children)
    return flow.batches.review(b)


def patch(flow,p,view,evidences):
    """An offered record/field selection has already been validated by patches."""
    h=flow.h
    # Shared patches already revalidate the offered version and sealed view.
    # A duplicate timestamp check would reject a repair in this same turn.
    b=deepcopy(flow.batches.get(p.draft_ref)) if view['domain']=='calendar_batch' else None
    if b and b['version']!=p.draft_version:raise PlanningError('calendar_batch_stale')
    if b:revoke(flow,b,batch=True)
    children={ref:deepcopy(flow.batches.child(ref)) for ref in b['members']} if b else {}
    for record in p.records:
        d=deepcopy(children[record.record_ref] if b else flow.draft(record.record_ref))
        if d['status']=='committing' or h._blocked_action():raise PlanningError('calendar_action_pending')
        if not b:revoke(flow,d)
        d.update(version=d['version']+1,updated_at=h._current['at'],review=None)
        evidence=evidences[record.record_ref]
        for change in record.changes:
            row=evidence[change.field]
            if row.get('temporal'):continue
            if change.field in TEMPORAL and d.get('model_interpreted'):
                evidence[change.field]=interpreted_evidence(h,change.field,change.value,row)
            else:row['value']=change.value
        d=apply(flow,d,evidence,[h._current['source_ref'],*(v.source_ref for v in record.changes)],
            uncertain=[v.field for v in record.changes if v.certainty=='uncertain'],
            approximate=[v.field for v in record.changes if v.certainty=='approximate'])
        if not b:return flow.save_review(d)
        children[d['draft_ref']]=d
    b.update(version=b['version']+1,updated_at=h._current['at'],review=None,model_interpreted=True)
    flow.batches._publish(b,children);return flow.batches.review(b)
