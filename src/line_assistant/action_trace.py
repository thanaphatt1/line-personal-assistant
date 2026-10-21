"""Bounded operational traces. Never log owner text, business values or secrets.

Cloud Run retains these structured log records independently of the business
snapshot. Logging is observational: it cannot authorize, retry or block effects.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib,json,re,sys
from time import monotonic
from uuid import uuid4

ACTIVE=ContextVar('assistant_action_trace',default=None)
MAX_RECORDS=96
STEPS={'show_choices','begin_new_item','use_existing_item','choose_category','choose_subcategory'}
FIELDS={'item','item_text','amount','date','date_text','date_ref','date_context','note','category','subcategory',
        'existing_item','source_refs','field_sources','previous_price_ref','entry_ref'}


def ref(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24] if value is not None else None


def code(value):
    return value if isinstance(value,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}',value) else 'unclassified'


def arguments(call):
    try:value=json.loads(call['arguments'])
    except (KeyError,ValueError,TypeError):return {'valid_object':False}
    if not isinstance(value,dict):return {'valid_object':False}
    result={'valid_object':True,'draft_ref':ref(value.get('draft_ref')),
            'entry_ref':ref(value.get('entry_ref')),'entry_ref_supplied':value.get('entry_ref') is not None}
    if type(value.get('draft_version')) is int:result['draft_version']=value['draft_version']
    if value.get('resolution_step') in STEPS:result['resolution_step']=value['resolution_step']
    def fields(row):
        selected=sorted(k for k in FIELDS if row.get(k) is not None and row.get(k)!=[])
        return {'entry_ref':ref(row.get('entry_ref')),'supplied_fields':selected,
                'source_refs':[ref(v) for v in row.get('source_refs',[])[:16]] if isinstance(row.get('source_refs',[]),list) else []}
    result.update(fields(value))
    if isinstance(value.get('entries'),list):
        result['entry_count']=len(value['entries'])
        result['entries']=[fields(v) for v in value['entries'][:5] if isinstance(v,dict)]
    return result


def snapshot(host):
    state=host.state
    return {'drafts':[{'draft_ref':ref(k),'version':d.get('version'),'status':code(d.get('status')),
                       'entry_count':len(d.get('entries',[])),'missing_count':len(d.get('missing_fields',[])),
                       'review_present':bool(d.get('review'))} for k,d in list(state.drafts.items())[:8]],
            'actions':{status:sum(a.status==status for a in state.actions.values())
                       for status in ['prepared','executing','uncertain','completed']}}


def outcome(value):
    if not isinstance(value,dict):return {'status':'unclassified'}
    result={k:value[k] for k in ['ok','confirmation_required','name_question_resumed'] if type(value.get(k)) is bool}
    for key in ['status','error','error_code']:
        if key in value:result[key]=code(value[key])
    for key in ['missing_fields','invalid_fields','validation_issues']:
        rows=value.get(key,[])
        if isinstance(rows,list):
            result[key]=[v for v in rows[:20] if isinstance(v,str) and re.fullmatch(r'(?:entries\[[0-4]\]\.)?[a-z_]+(?:\.[a-z_]+)?',v)]
    for key in ['draft_ref','catalog_entry_ref']:
        if value.get(key) is not None:result[key]=ref(value[key])
    if type(value.get('draft_version')) is int:result['draft_version']=value['draft_version']
    enrollment=value.get('item_enrollment',{})
    if isinstance(enrollment,dict) and enrollment.get('field') in {'category','subcategory'}:
        result['next_field']=enrollment['field'];result['entry_ref']=ref(enrollment.get('entry_ref'))
    return result


class Trace:
    def __init__(self,event,sink):
        self.turn_ref=ref(event);self.attempt_ref=uuid4().hex;self.sink=sink
        self.started=monotonic();self.count=0;self.dropped=0;self.sink_failures=0

    def emit(self,phase,*,final=False,**fields):
        if self.count>=MAX_RECORDS and not final:self.dropped+=1;return
        row={'event':'assistant_action_trace','version':1,'turn_ref':self.turn_ref,'attempt_ref':self.attempt_ref,
             'sequence':self.count,'elapsed_ms':round((monotonic()-self.started)*1000),'phase':phase,**fields}
        self.count+=1
        try:self.sink(row)
        except Exception:self.sink_failures+=1


def emit(phase,**fields):
    current=ACTIVE.get()
    if current is not None:current.emit(phase,**fields)


def log_record(row):
    # One bare JSON line lets Cloud Logging parse jsonPayload. Uvicorn's
    # ordinary log formatter prefixes INFO and would make this plain text.
    sys.stdout.write(json.dumps(row,separators=(',',':'))+'\n');sys.stdout.flush()


@contextmanager
def turn(host,event):
    source=event.get('source',{});event_id=event.get('webhookEventId')
    enabled=(host.raw_history_enabled and isinstance(source,dict) and source.get('type')=='user'
             and source.get('userId')==host.settings.owner_id and isinstance(event_id,str)
             and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',event_id))
    sink=getattr(host,'action_trace_sink',None)
    if sink is None and getattr(host,'production',False):sink=log_record
    if not enabled or sink is None:yield None;return
    trace=Trace(event_id,sink);token=ACTIVE.set(trace)
    try:
        trace.emit('turn_started')
        yield trace
    except BaseException:
        trace.emit('turn_interrupted',final=True,dropped_records=trace.dropped,sink_failures=trace.sink_failures)
        raise
    finally:ACTIVE.reset(token)


def phase(host,name,key):
    if ACTIVE.get() is None:return
    action=host.state.actions.get(key)
    emit('checkpoint',checkpoint=code(name),action_ref=ref(key) if action else None,
         action_kind=code(action.kind) if action else None,action_status=code(action.status) if action else None)
