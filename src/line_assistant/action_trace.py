"""Bounded operational traces. Never log owner text, business values or secrets.

Cloud Run retains these structured log records independently of the business
snapshot. Logging is observational: it cannot authorize, retry or block effects.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib,json,re,sys
from time import monotonic
from threading import RLock
from uuid import uuid4

ACTIVE=ContextVar('assistant_action_trace',default=None)
PROCESS=ContextVar('assistant_trace_process',default=None)
MAX_RECORDS=512
MAX_PRIORITY_RECORDS=64
PRIORITY_PHASES={'tool_proposed','planning_stopped','model_request','model_result','tool_request','tool_result',
                 'query_source_result','planning_transition','reply_delivery','reply_fallback','reply_validation'}
STEPS={'show_choices','begin_new_item','use_existing_item','choose_category','choose_subcategory'}
FIELDS={'item','item_text','amount','date','date_text','date_ref','date_context','note','category','subcategory',
        'existing_item','source_refs','field_sources','previous_price_ref','entry_ref'}


def ref(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24] if value is not None else None


def code(value):
    return value if isinstance(value,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}',value) else 'unclassified'


def fingerprint(value):
    try:return ref(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')))
    except (ValueError,TypeError,OverflowError):return None


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
    result['arguments_ref']=fingerprint(value)
    request=value.get('request')
    if isinstance(request,dict):
        result['intent']=request.get('intent') if request.get('intent') in {'read','inspect','respond'} else 'unclassified'
        result['request']=arguments({'arguments':json.dumps({k:v for k,v in request.items() if k!='request'},ensure_ascii=False)})
    if value.get('response_ref') is not None:result['response_ref']=ref(value['response_ref'])
    for key in ['query_refs','calendar_query_refs']:
        if isinstance(value.get(key),list):result[key]=[ref(v) for v in value[key][:32]]
    if isinstance(value.get('sql'),str):result['sql']=sql_shape(value['sql'])
    if isinstance(value.get('sources'),list):
        result['sources']=[{'source':v.get('source') if v.get('source') in {'expense','calendar','table','result'} else 'unclassified',
            'alias_ref':ref(v.get('alias')),'query_ref':ref(v.get('query_ref')),
            'parameters_ref':fingerprint(v.get('parameters'))}
            for v in value['sources'][:8] if isinstance(v,dict)]
    if isinstance(value.get('continue_analysis'),bool):result['continue_analysis']=value['continue_analysis']
    if isinstance(value.get('entries'),list):
        result['entry_count']=len(value['entries'])
        result['entries']=[fields(v) for v in value['entries'][:5] if isinstance(v,dict)]
    if isinstance(value.get('records'),list):
        result['record_count']=len(value['records'])
        result['records']=[{'entry_ref':ref(v.get('record_ref')),'field_count':len(v.get('changes',[])),
            'source_refs':[ref(c.get('source_ref')) for c in v.get('changes',[])[:40] if isinstance(c,dict)]}
            for v in value['records'][:8] if isinstance(v,dict) and isinstance(v.get('changes',[]),list)]
    return result


def sql_shape(sql):
    # Diagnostic features only, never SQL or identifiers. Strip quoted values,
    # names and comments before looking for fixed keywords. This is not a parser
    # or a validator and cannot influence execution.
    masked=re.sub(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^\]]*\]",' ',sql,flags=re.S)
    tokens=set(re.findall(r'[a-z_]+',masked.casefold()))
    return {'sql_ref':ref(sql),'bytes':len(sql.encode()),
            'features':sorted(tokens&{'select','with','where','join','exists','group','having','order','limit',
                'distinct','union','sum','decimal_sum','count','min','max','over','case','not','and','or'})}


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
    feedback=value.get('validation_feedback')
    if isinstance(value.get('recovery'),dict):result['recovery_kind']=code(value['recovery'].get('kind'))
    if value.get('temporal_ref'):result['temporal_ref']=ref(value['temporal_ref'])
    if isinstance(feedback,dict):
        result['failure_kind']=code(feedback.get('kind'))
        result['next_action']=code(feedback.get('next_action'))
    enrollment=value.get('item_enrollment',{})
    if isinstance(enrollment,dict) and enrollment.get('field') in {'category','subcategory'}:
        result['next_field']=enrollment['field'];result['entry_ref']=ref(enrollment.get('entry_ref'))
    for key in ['complete','source_complete','analysis_input_only','continue_analysis','read_only','read_failure']:
        if type(value.get(key)) is bool:result[key]=value[key]
    for key in ['count','shown','group_count']:
        if type(value.get(key)) is int and value[key]>=0:result[key]=value[key]
    for key in ['query_ref','temporal_ref','clarification_ref']:
        if value.get(key) is not None:result[key]=ref(value[key])
    if isinstance(value.get('query_refs'),list):result['query_refs']=[ref(v) for v in value['query_refs'][:32]]
    for key in ['entries','rows','columns','results']:
        if isinstance(value.get(key),list):result[key+'_count']=len(value[key])
    scope=value.get('scope')
    if isinstance(scope,dict):
        result['scope_ref']=fingerprint(scope)
        result['domain']=scope.get('domain') if scope.get('domain') in {'expense','calendar','table','relation'} else 'unclassified'
    if isinstance(value.get('recovery'),dict):result['recovery_kind']=code(value['recovery'].get('kind'))
    return result


class Trace:
    def __init__(self,event,sink):
        self.turn_ref=ref(event);self.attempt_ref=uuid4().hex;self.sink=sink
        self.started=monotonic();self.count=0;self.dropped=0;self.sink_failures=0
        self.priority_count=0
        self.lock=RLock();self.pending={};self.durations={};self.calls={};self.fallbacks=0
        self.closed=False
        self.status='unknown'
        self.replayed=False

    def emit(self,phase,*,final=False,**fields):
        with self.lock:self._emit(phase,final=final,**fields)

    def _emit(self,phase,*,final=False,**fields):
        if self.closed:return
        now=monotonic()
        pairs={'model_request':('model',fields.get('run_ref')),'tool_request':('tool',fields.get('call_ref'))}
        if phase in pairs:
            kind,key=pairs[phase];label=code(fields.get('role') if kind=='model' else fields.get('tool'))
            self.pending[(kind,key)]=(now,label)
            if not fields.get('cached'):self.calls[kind]=self.calls.get(kind,0)+1
        elif phase in {'model_result','tool_result'}:
            kind='model' if phase=='model_result' else 'tool'
            key=fields.get('run_ref' if kind=='model' else 'call_ref')
            start=self.pending.pop((kind,key),None)
            if start:
                duration=max(0,round((now-start[0])*1000));fields['duration_ms']=duration
                label=kind+'_'+start[1];self.durations[label]=self.durations.get(label,0)+duration
        if phase=='reply_fallback':self.fallbacks+=1
        if phase=='receipt_replay':self.replayed=True
        if phase=='turn_finished':self.status=code(fields.get('status'))
        if phase=='turn_interrupted':self.status='interrupted'
        if phase=='webhook_finished':fields['status']=self.status
        if phase in {'turn_finished','turn_interrupted','webhook_finished'}:
            fields['performance']={'total_ms':max(0,round((now-self.started)*1000)),
                'phase_ms':dict(self.durations),'calls':dict(self.calls),'reply_fallbacks':self.fallbacks,'replayed':self.replayed}
        if self.count>=MAX_RECORDS and not final:
            priority=phase in PRIORITY_PHASES or phase=='process_finished' and fields.get('status')=='failed'
            if not priority or self.priority_count>=MAX_PRIORITY_RECORDS:self.dropped+=1;return
            self.priority_count+=1
        row={'event':'assistant_action_trace','version':1,'turn_ref':self.turn_ref,'attempt_ref':self.attempt_ref,
             'sequence':self.count,'elapsed_ms':round((monotonic()-self.started)*1000),'phase':phase,**fields}
        if PROCESS.get() and 'process_ref' not in row:row['process_ref']=PROCESS.get()
        self.count+=1
        try:self.sink(row)
        except Exception:self.sink_failures+=1


def emit(phase,**fields):
    current=ACTIVE.get()
    if current is not None:current.emit(phase,**fields)


@contextmanager
def span(label):
    """Fixed labels only; no paths, payloads or exception messages in metrics."""
    current=ACTIVE.get();started=monotonic();status='completed';details={}
    process_ref=uuid4().hex;parent=PROCESS.get();token=PROCESS.set(process_ref)
    if current is not None:current.emit('process_started',process=code(label),process_ref=process_ref,parent_process_ref=parent)
    try:yield details
    except BaseException as error:
        status='failed'
        details['error_kind']={'ApiError':'http_error','OutcomeUnknown':'outcome_unknown','LeaseLost':'lease_lost',
            'LeaseBusy':'lease_busy','ValidationError':'validation','ValueError':'validation',
            'LiveDisabled':'disabled','NotReady':'not_ready','BudgetBlocked':'budget_blocked'}.get(type(error).__name__,'internal')
        http_status=getattr(error,'status',None)
        if type(http_status) is int and 100<=http_status<=599:details['http_status']=http_status
        raise
    finally:
        if current is not None:
            with current.lock:
                label=code(label);duration=max(0,round((monotonic()-started)*1000))
                current.durations[label]=current.durations.get(label,0)+duration
                current.calls[label]=current.calls.get(label,0)+1
                current.emit('process_finished',process=label,process_ref=process_ref,parent_process_ref=parent,
                             duration_ms=duration,status=status,**details)
        PROCESS.reset(token)


def observed(label):
    """Observe synchronous work without reading its arguments or return value."""
    def decorate(function):
        @wraps(function)
        def call(*args,**kwargs):
            if ACTIVE.get() is None:return function(*args,**kwargs)
            with span(label):return function(*args,**kwargs)
        return call
    return decorate


def log_record(row):
    # One bare JSON line lets Cloud Logging parse jsonPayload. Uvicorn's
    # ordinary log formatter prefixes INFO and would make this plain text.
    sys.stdout.write(json.dumps(row,separators=(',',':'))+'\n');sys.stdout.flush()


@contextmanager
def turn(host,event,*,boundary='assistant'):
    if ACTIVE.get() is not None:
        yield ACTIVE.get();return
    source=event.get('source',{});event_id=event.get('webhookEventId')
    enabled=(getattr(host,'raw_history_enabled',False) and isinstance(source,dict) and source.get('type')=='user'
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
    finally:
        if boundary=='webhook':trace.emit('webhook_finished',final=True,dropped_records=trace.dropped,sink_failures=trace.sink_failures)
        with trace.lock:trace.closed=True
        ACTIVE.reset(token)


def phase(host,name,key):
    if ACTIVE.get() is None:return
    action=host.state.actions.get(key)
    emit('checkpoint',checkpoint=code(name),action_ref=ref(key) if action else None,
         action_kind=code(action.kind) if action else None,action_status=code(action.status) if action else None)
