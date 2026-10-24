"""Default-off local SQLite boundary and atomic fake provider; no cloud routes."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,date
from pathlib import Path
import fcntl,json,os

from .history_context import HistoryState,draft_fingerprint,MAX_HISTORY_BYTES,digest
from .persistence import SQLiteStore,state_snapshot,restore_state
from .conversation_mocks import ConversationSheets
from .contracts import Transaction
from .adapters import Reconciliation
from .snapshot_codec import MAX_EXPANDED_BYTES

MAX_LOCAL_SNAPSHOT_BYTES=800000

def aware(value):
    at=datetime.fromisoformat(value)
    if at.utcoffset() is None:raise ValueError('History timestamps must be aware')
    return at

def snapshot_size(value,max_snapshot_bytes):
    if type(max_snapshot_bytes) is not int or not 1<=max_snapshot_bytes<=MAX_EXPANDED_BYTES:
        raise ValueError('Invalid history snapshot capacity')
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    if len(raw.encode())>max_snapshot_bytes:raise ValueError('Local history snapshot exceeds capacity')
    return raw

def history_snapshot(state,*,max_snapshot_bytes=MAX_LOCAL_SNAPSHOT_BYTES,include_records=True):
    value={'format':'local-history-state-v1','owner_id':state.owner_id,'core':state_snapshot(state,state.owner_id,include_records=include_records),
           'history':deepcopy(state.history),'history_bytes':state.history_bytes,'history_removed':state.history_removed,
           'drafts':deepcopy(state.drafts),'draft_proofs':deepcopy(state.draft_proofs)}
    if state.owner_features_used or state.catalog_aliases:
        # Older runtimes must reject rather than silently discard durable
        # vocabulary or attempt an unknown removal action during rollback.
        value['format']='local-history-state-v2'
        extra={'owner':state.owner_id,'scope':state.workbook_id,'aliases':deepcopy(state.catalog_aliases)}
        extra['seal']=digest(extra);value['owner_features_v1']=extra
    if state.calendar_drafts:
        format_name='calendar-history-v2' if any('all_day' in d for d in state.calendar_drafts.values()) else 'calendar-history-v1'
        value['calendar_v1']={'format':format_name,'drafts':deepcopy(state.calendar_drafts),'proofs':deepcopy(state.calendar_proofs)}
    if state.calendar_batches:
        value['calendar_batches_v1']={'format':'calendar-batches-v1','batches':deepcopy(state.calendar_batches),'proofs':deepcopy(state.calendar_batch_proofs)}
    if state.calendar_connections is not None:value['calendar_connections_v1']=deepcopy(state.calendar_connections)
    if state.activity_labels:
        # A separate optional key: older runtimes ignore it, so a rollback
        # forgets these conveniences instead of rejecting the owner state.
        from .activity_labels import snapshot as activity_snapshot
        value['activity_labels_v1']=activity_snapshot(state)
    if state.raw_history_enabled and state.raw_history_migrated:
        value['history']=[]
        value['raw_history']={'enabled':True,'rolling_summary':deepcopy(state.rolling_summary)}
    return json.loads(snapshot_size(value,max_snapshot_bytes))

def restore_history(state,value,owner,*,expected_scope=None,max_snapshot_bytes=MAX_LOCAL_SNAPSHOT_BYTES):
    # Validate off to the side: a rejected snapshot cannot partially replace a
    # live worker's validated values, receipts or model reservation records.
    if value.get('format') not in {'local-history-state-v1','local-history-state-v2'} or value.get('owner_id')!=owner:raise ValueError('History snapshot owner/version mismatch')
    extension=value.get('owner_features_v1')
    if (value['format']=='local-history-state-v2')!=(extension is not None):raise ValueError('Owner feature state/version mismatch')
    snapshot_size(value,max_snapshot_bytes)
    fresh=HistoryState(history_bytes=value['history_bytes']);fresh.owner_id=owner
    raw=value.get('raw_history')
    if raw is not None:
        if set(raw)!={'enabled','rolling_summary'} or raw['enabled'] is not True:raise ValueError('Invalid raw history envelope')
        if state.raw_journal is None:raise ValueError('Recoverable raw journal required to restore full history')
        fresh.raw_history_enabled=True;fresh.raw_history_migrated=True;fresh.raw_journal=state.raw_journal;fresh.rolling_summary=deepcopy(raw['rolling_summary'])
        if fresh.rolling_summary is not None and not isinstance(fresh.rolling_summary,dict):raise ValueError('Invalid derived summary cache')
        # Invalidate only the old derived cache; originals remain in the
        # journal and the next request reads them before summarizing if needed.
        if fresh.rolling_summary and fresh.rolling_summary.get('format')!='raw-summary-provenance-v2':fresh.rolling_summary=None
    restore_state(fresh,value['core'],owner)
    if extension is not None:
        from .catalog_aliases import validate_records
        scope=expected_scope if expected_scope is not None else fresh.workbook_id
        if (not isinstance(extension,dict) or set(extension)!={'owner','scope','aliases','seal'}
            or extension['owner']!=owner or extension['scope']!=scope
            or extension['seal']!=digest({k:v for k,v in extension.items() if k!='seal'})):
            raise ValueError('Invalid owner feature scope/proof')
        validate_records(extension['aliases'])
        fresh.catalog_aliases=deepcopy(extension['aliases']);fresh.owner_features_used=True
    rows=fresh.raw_journal.load(state.clock()) if raw else value['history']
    if not isinstance(rows,list) or not raw and len(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode())>fresh.history_bytes:raise ValueError('Invalid persisted history bound')
    if type(value['history_removed']) is not int or value['history_removed']<0:raise ValueError('Invalid removal counter')
    for row in rows:
        if row.get('role') not in {'user','assistant','tool'}:raise ValueError('Invalid history role')
        aware(row['at'])
        if row['role']=='user':
            from .history_context import source_ref
            if row.get('source_ref')!=source_ref(row['event']) or not isinstance(row.get('text'),str):raise ValueError('Invalid persisted source identity')
    drafts,proofs=value['drafts'],value['draft_proofs']
    if not isinstance(drafts,dict) or not isinstance(proofs,dict) or set(drafts)!=set(proofs):raise ValueError('History provenance record mismatch')
    for ref,d in drafts.items():
        p=proofs[ref]
        if (d.get('draft_ref')!=ref or d.get('owner_id')!=owner or p.get('owner_id')!=owner
            or type(d.get('version')) is not int or d['version']<1 or p.get('version')!=d['version']
            or p.get('fingerprint')!=draft_fingerprint(d)):raise ValueError('History draft integrity/version mismatch')
        for field in ['created_at','updated_at','expires_at']:aware(d[field])
        if not isinstance(d.get('entries'),list):raise ValueError('Invalid persisted draft entries')
        for entry in d['entries']:
            from .previous_prices import validate as validate_previous_price
            validate_previous_price(entry)
            required={'entry_ref','item','amount','date','date_parts','note','date_basis','date_issue','kind','mapping','source_refs'}
            if not isinstance(entry,dict) or not required<=set(entry):raise ValueError('Missing persisted draft entry fields')
            if (not isinstance(entry['entry_ref'],str) or not entry['entry_ref']
                or any(entry[k] is not None and not isinstance(entry[k],str) for k in ['item','amount','date','date_issue'])
                or not isinstance(entry['note'],str) or not isinstance(entry['date_parts'],dict) or not isinstance(entry['date_basis'],dict)
                or entry['kind']!='expense' or not isinstance(entry['source_refs'],list) or any(not isinstance(r,str) for r in entry['source_refs'])):
                raise ValueError('Invalid persisted partial draft values')
            facts=entry.get('fact_sources',{})
            if entry['date_basis'].get('temporal'):
                from .temporal import validate_proof
                proof=entry['date_basis']['temporal'];validate_proof(proof)
                if proof['owner']!=owner or proof['values']['date']!=entry['date'] or proof['source_ref'] not in facts.get('date',[]):
                    raise ValueError('Invalid persisted temporal expense date')
            names=entry.get('name_candidates',[])
            if (not isinstance(names,list) or len(names)>1000 or any(not isinstance(n,str) or not 1<=len(n)<=200 for n in names)
                or len(names)!=len(set(names))):raise ValueError('Invalid persisted catalog name candidates')
            if (not isinstance(facts,dict) or set(facts)-{'item','amount','date','note'}
                or any(not isinstance(refs,list) or any(not isinstance(ref,str) or ref not in entry['source_refs'] for ref in refs) for refs in facts.values())):
                raise ValueError('Invalid persisted field provenance')
            occurrence=entry.get('amount_occurrence')
            if occurrence:
                required={'source_ref','span','ordinal','amount','at','occurrence_ref'}
                if (set(occurrence)!=required or occurrence['source_ref'] not in entry['source_refs'] or occurrence['amount']!=entry['amount']
                    or type(occurrence['ordinal']) is not int or not 0<=occurrence['ordinal']<5
                    or not isinstance(occurrence['span'],list) or len(occurrence['span'])!=2
                    or any(type(v) is not int for v in occurrence['span']) or not 0<=occurrence['span'][0]<occurrence['span'][1]<=2000
                    or occurrence['occurrence_ref']!='occ_'+digest({k:v for k,v in occurrence.items() if k!='occurrence_ref'})[:20]):
                    raise ValueError('Invalid persisted amount occurrence')
                aware(occurrence['at'])
        occurrences=[e['amount_occurrence']['occurrence_ref'] for e in d['entries'] if e.get('amount_occurrence')]
        if len(occurrences)!=len(set(occurrences)):raise ValueError('Reused persisted amount occurrence')
        if d['review']:
            aware(d['review']['at'])
            payload={'entries':[{k:e[k] for k in ['item','amount','date','note','kind']} for e in d['entries']]}
            from .expense_evidence import catalog_mappings
            mappings=catalog_mappings(d['entries'])
            if mappings:payload['catalog']=mappings
            if d['review']['payload_hash']!=digest(payload) or d['review']['catalog_hash']!=d['catalog_hash']:raise ValueError('Invalid persisted confirmation binding')
    if 'calendar_v1' in value:
        from .calendar_workflow import restore_calendar
        restore_calendar(fresh,value['calendar_v1'])
    if 'calendar_batches_v1' in value:
        from .calendar_batches import restore_batches
        restore_batches(fresh,value['calendar_batches_v1'])
    if 'calendar_connections_v1' in value:
        from .calendar_connections import validate as validate_connections
        validate_connections(value['calendar_connections_v1'],owner)
        fresh.calendar_connections=deepcopy(value['calendar_connections_v1'])
    if 'activity_labels_v1' in value:
        from .activity_labels import validate as validate_labels
        validate_labels(value['activity_labels_v1'],owner)
        fresh.activity_labels=deepcopy(value['activity_labels_v1']['labels'])
    fresh.history=deepcopy(rows);fresh.history_removed=value['history_removed'];fresh.drafts=deepcopy(drafts);fresh.draft_proofs=deepcopy(proofs)
    for k,v in vars(fresh).items():
        if k not in {'lock','raw_journal','clock'}:setattr(state,k,v)

class LocalHistoryState(HistoryState):
    """Serialize local workers with flock; short SQLite transactions and CAS.

    The OS lock spans the owner handler including mock/model I/O. This is not a
    time-limited distributed lease. Checkpoints outside a session are rejected.
    """
    def __init__(self,path:Path,owner_id:str,*,history_bytes=MAX_HISTORY_BYTES,phase_hook=None,clock=None):
        super().__init__(history_bytes=history_bytes)
        if not owner_id:raise ValueError('Explicit local owner required')
        self.owner_id=owner_id;self.store=SQLiteStore(path);self.lock_path=self.store.path.with_suffix('.history.lock')
        self.revision=0;self._active=False;self.phase_hook=phase_hook or (lambda phase,revision:None)
        from .raw_history import SQLiteJournal
        self.raw_journal=SQLiteJournal(path,owner_id)
        if clock is not None:self.clock=clock
        with self.session():pass

    @contextmanager
    def session(self):
        with self.lock:
            if self._active:raise RuntimeError('Nested local history session')
            fd=os.open(self.lock_path,os.O_CREAT|os.O_RDWR,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX)
                with self.store.transaction() as c:record=self.store.read(c,'history_state')
                if record:
                    if type(record.get('revision')) is not int or record['revision']<1:raise ValueError('Invalid local storage revision')
                    restore_history(self,record['snapshot'],self.owner_id);self.revision=record['revision']
                self._active=True
                try:yield
                finally:
                    try:self.checkpoint()
                    finally:self._active=False
            finally:
                fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

    def checkpoint(self):
        if not self._active:raise RuntimeError('Checkpoint requires active local owner session')
        value=history_snapshot(self)
        with self.store.transaction() as c:
            old=self.store.read(c,'history_state');actual=old['revision'] if old else 0
            if actual!=self.revision:raise RuntimeError('Stale local checkpoint revision')
            self.store.write(c,'history_state',{'revision':actual+1,'snapshot':value})
            self.phase_hook('state_staged',actual+1)
        self.revision+=1;self.phase_hook('state_committed',self.revision)

    def before_effect(self):self.checkpoint()

class LocalHistorySheets(ConversationSheets):
    """Independent atomic fake rows+receipts. Real Sheets lacks this contract."""
    def __init__(self,layout,rows,path:Path,owner_id:str,*,reconciliation_available=True,phase_hook=None):
        super().__init__(layout,rows)
        self.store=SQLiteStore(path);self.owner_id=owner_id
        self.reconciliation_available=reconciliation_available;self.phase_hook=phase_hook or (lambda phase,key:None)
        self._call('summary',date.min,date.max)

    def _call(self,name,*args):
        layout={'columns':dict(self.layout.columns),'formula_columns':list(self.layout.formula_columns),'date_mode':self.layout.date_mode}
        with self.store.transaction() as c:
            value=self.store.read(c,'history_sheets')
            if value:
                if value['owner_id']!=self.owner_id or value['layout']!=layout:raise ValueError('Local provider owner/layout mismatch')
                self.rows=[tuple(r) for r in value['rows']];self.entries=[Transaction.model_validate(e) for e in value['entries']]
                self.cell_values=value['cell_values'];self.write_log=value['write_log'];self.atomic_batches=value['atomic_batches']
                self.effects.receipts={k:(v[0],tuple(v[1])) for k,v in value['receipts'].items()}
                self.enrollments.receipts={k:(v[0],tuple(v[1])) for k,v in value['enrollments'].items()}
            result=getattr(super(),name)(*args)
            self.store.write(c,'history_sheets',{'owner_id':self.owner_id,'layout':layout,'rows':self.rows,
                'entries':[e.model_dump(mode='json') for e in self.entries],'cell_values':self.cell_values,'write_log':self.write_log,
                'atomic_batches':self.atomic_batches,'receipts':self.effects.receipts,'enrollments':self.enrollments.receipts})
            if name in {'add','enroll_and_add'}:self.phase_hook('provider_staged',args[-1])
        if name in {'add','enroll_and_add'}:self.phase_hook('provider_committed',args[-1])
        return result

    def add(self,entries,key):return self._call('add',entries,key)
    def enroll_and_add(self,payload,key):return self._call('enroll_and_add',payload,key)
    def summary(self,start,end):return self._call('summary',start,end)
    def reconcile(self,key,payload):return self._call('reconcile',key,payload) if self.reconciliation_available else Reconciliation('unknown')
    def reconcile_enrollment(self,key,payload):return self._call('reconcile_enrollment',key,payload) if self.reconciliation_available else Reconciliation('unknown')

def local_history_assistant(directory:Path,*,port,owner_id='mock-owner',clock=None,enabled=False,reconciliation_available=True,phase_hook=None,state_phase_hook=None):
    if enabled is not True:raise ValueError('Local persistence prototype defaults off')
    from .history_first import HistoryFirstAssistant
    from .config import Settings
    from .adapters import SheetLayout
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    state=LocalHistoryState(directory/'history.sqlite',owner_id,phase_hook=state_phase_hook,clock=clock)
    sheets=LocalHistorySheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),
        [('ค่ากระดาษ','Work/Office',''),('กาแฟ','Food',''),('รถเมล์','Transportation','')],directory/'fake-sheets.sqlite',owner_id,
        reconciliation_available=reconciliation_available,phase_hook=phase_hook)
    service=HistoryFirstAssistant(Settings(owner_id=owner_id),sheets,port=port,state=state,clock=clock,history_first_enabled=True)
    service.phase_hook=phase_hook or (lambda phase,key:None)
    return service
