"""Small owner state plus versioned, owner-fenced durable evidence records."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
import re
from .firestore_state import FirestoreState
from .history_context import HistoryState
from .history_persistence import history_snapshot, restore_history
from .persistence import state_snapshot, restore_state
from .snapshot_codec import MAX_EXPANDED_BYTES

from .transport import LiveDisabled

class ProductionHistoryState(FirestoreState, HistoryState):
    def __init__(self,client,*,clock=None):
        from .pilot_http import PilotPort
        if (type(client) is not PilotPort or client.service!='firestore' or not client.http.config.history_state_enabled
                or client.http.config.mode!='production' or not client.http.config.launch_authorized):
            raise LiveDisabled('Complete history state opt-in required')
        c=client.http.config
        super().__init__(client,c.owner_id,project_id=c.project_id,collection_id='assistant_pilot_states',clock=clock,
                         max_snapshot_bytes=800000 if c.state_codec_enabled else 200000,compact_snapshots=True)
        # Hosted history always enables compact_snapshots above. The legacy
        # state-codec flag selects the encoded bound (200k/800k), not whether
        # history can use compression. Validate expanded JSON against the
        # codec's expanded bound; _fields still bounds the complete wire image.
        self.history_snapshot_bytes=MAX_EXPANDED_BYTES
        self.history_workspaces={}
        from .raw_history import FirestoreJournal
        self.raw_journal=FirestoreJournal(client,self.database,self.owner_id,c.storage_scope_id)
        from .state_records import StateRecords
        self.record_store=StateRecords(self)
        self.record_phase_hook=lambda phase,cursor:None
    @property
    def approved_workbooks(self):
        return self.client.http.config.approved_workbooks
    def _parts(self):
        v=history_snapshot(self,max_snapshot_bytes=self.history_snapshot_bytes,include_records=self.record_store.mode!='ready');v.pop('core');return v
    def _snapshot_state(self):
        if self.record_store.mode=='copy':
            return {'version':2,'owner_id':self.owner_id,'state_records_v1':deepcopy(self.record_store.marker)}
        if self.record_store.mode=='ready':return self._record_snapshot()
        return self._legacy_snapshot()
    def _legacy_snapshot(self):
        return {**state_snapshot(self,self.owner_id),'history24':{'active_workbook':self.workbook_id,
                'current':self._parts(),'workspaces':deepcopy(self.history_workspaces)}}
    def _record_snapshot(self):
        marker=deepcopy(self.record_store.marker)
        marker.setdefault('cleanup',{})
        months=list(self.model_budgets)
        if marker.get('budget_month'):months.append(marker['budget_month'])
        marker['budget_month']=max(months,default=None)
        pointers=marker.setdefault('last_action_keys',{})
        if not self.last:pointers.pop(self.workbook_id,None)
        elif self.last.expires_at>self.clock():
            matches=[]
            for key,a in self.actions.items():
                if a.status!='completed' or not a.result:continue
                data=a.result.data
                refs=data.get('refs') or ([data.get('event_id') or data.get('id')] if data.get('event_id') or data.get('id') else [])
                if tuple(refs)==tuple(self.last.refs):matches.append(key)
            if matches:pointers[self.workbook_id]=matches[-1]
        # Preserve inactive workspace references on the one-time conversion.
        for book,part in self.workspaces.items():
            last=part.get('last')
            if not last:continue
            for key,a in part.get('actions',{}).items():
                data=(a.get('result') or {}).get('data',{})
                refs=data.get('refs') or ([data.get('event_id') or data.get('id')] if data.get('event_id') or data.get('id') else [])
                if a['status']=='completed' and tuple(refs)==tuple(last['refs']):pointers[book]=key
        return {**state_snapshot(self,self.owner_id,include_records=False),'version':2,
                'state_records_v1':marker,'history24':{'active_workbook':self.workbook_id,
                'current':self._parts(),'workspaces':deepcopy(self.history_workspaces)}}
    def _restore_state(self,value):
        from .state_records import FORMAT
        store=self.record_store;store.reset();store.mode=None;store.marker=None
        marker=value.get('state_records_v1')
        if marker is None:
            return self._restore_legacy_state(value)
        if (value.get('version')!=2 or value.get('owner_id')!=self.owner_id or not isinstance(marker,dict)
            or marker.get('format')!=FORMAT or marker.get('phase') not in {'copy','ready'}):
            raise ValueError('Invalid external evidence envelope')
        if marker.get('budget_month') is not None and (not isinstance(marker['budget_month'],str) or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])',marker['budget_month'])):
            raise ValueError('Invalid evidence budget clock')
        store.marker=deepcopy(marker);store.mode=marker['phase'];store.reading=True
        try:
            if store.mode=='copy':
                from .state_record_migration import source_snapshot
                if (set(value)!={'version','owner_id','state_records_v1'}
                    or set(marker)!={'format','phase','source','cursor','count','budget_month'}
                    or type(marker['cursor']) is not int or type(marker['count']) is not int
                    or not 0<=marker['cursor']<=marker['count'] or not isinstance(marker['source'],str)
                    or not re.fullmatch(r'[a-f0-9]{64}',marker['source'])):raise ValueError('Invalid migration cursor')
                original,_=source_snapshot(store,marker)
                self._restore_legacy_state(original)
            else:
                if (set(marker)!={'format','phase','budget_month','last_action_keys','cleanup'}
                    or not isinstance(marker['last_action_keys'],dict)
                    or not isinstance(marker['cleanup'],dict) or set(marker['cleanup'])-{'next_at','events','actions','models'}
                    or set(marker['last_action_keys'])-self.approved_workbooks
                    or any(not isinstance(k,str) or not 1<=len(k)<=512 for k in marker['last_action_keys'].values())):
                    raise ValueError('Invalid evidence metadata')
                cleanup=marker['cleanup']
                if cleanup.get('next_at'):
                    if not isinstance(cleanup['next_at'],str) or datetime.fromisoformat(cleanup['next_at']).utcoffset() is None:
                        raise ValueError('Invalid cleanup clock')
                for group in ('events','actions','models'):
                    cursor=cleanup.get(group)
                    if cursor is None:continue
                    if (not isinstance(cursor,list) or len(cursor)!=2 or set(cursor[0])!={'timestampValue'}
                        or set(cursor[1])!={'referenceValue'} or not isinstance(cursor[1]['referenceValue'],str)
                        or not re.fullmatch(re.escape(store.parent(group)+'/records/')+r'[a-f0-9]{64}',cursor[1]['referenceValue'])
                        or datetime.fromisoformat(cursor[0]['timestampValue'].replace('Z','+00:00')).utcoffset() is None):
                        raise ValueError('Invalid cleanup cursor')
                core=deepcopy(value);core.pop('state_records_v1');core['version']=1
                if any(core.get(k) for k in ('claims','receipts','actions','replies','model_runs','model_budgets','event_workbooks')):
                    raise ValueError('External evidence cannot also be inline')
                if any(part.get(k) for part in core.get('workspaces',{}).values() for k in ('claims','receipts','actions','replies')):
                    raise ValueError('Inactive external evidence cannot also be inline')
                store.validation_actions(core)
                self._restore_legacy_state(core)
                store.hydrate()
        finally:store.reading=False
    def _restore_legacy_state(self,value):
        core=deepcopy(value);extra=core.pop('history24',None)
        if core.get('workbook_id') is not None and core['workbook_id'] not in self.approved_workbooks:
            raise ValueError('Persisted storage scope is not explicitly retained by configuration')
        def journal_for(book):
            from .raw_history import FirestoreJournal
            book=book if book in self.approved_workbooks else self.client.http.config.storage_scope_id
            journal=FirestoreJournal(self.client,self.database,self.owner_id,book)
            journal.auth_secret=self.raw_journal.auth_secret
            return journal
        self.raw_journal=journal_for(core.get('workbook_id'))
        if extra is None:
            fresh=HistoryState();fresh.owner_id=self.owner_id;restore_state(fresh,core,self.owner_id)
            for k,v in vars(fresh).items():
                if k not in {'lock','raw_journal','clock'}:setattr(self,k,v)
            self.history_workspaces={};return
        if (set(extra)!={'active_workbook','current','workspaces'} or extra['active_workbook']!=core.get('workbook_id')
                or not isinstance(extra['workspaces'],dict) or set(extra['workspaces'])-self.approved_workbooks):
            raise ValueError('Invalid history workspace binding')
        for book,parts in extra['workspaces'].items():
            workspace_core={**core,**core.get('workspaces',{}).get(book,{}),'workbook_id':book}
            fresh=HistoryState();fresh.raw_journal=journal_for(book);fresh.clock=self.clock;restore_history(fresh,{**parts,'core':workspace_core},self.owner_id,expected_scope=book,max_snapshot_bytes=self.history_snapshot_bytes)
        restore_history(self,{**extra['current'],'core':core},self.owner_id,max_snapshot_bytes=self.history_snapshot_bytes)
        self.history_workspaces=deepcopy(extra['workspaces'])
    @contextmanager
    def session(self):
        with super().session():
            store=self.record_store
            if (store.mode=='copy' or self.client.http.config.state_records_enabled and store.mode!='ready') and self.workbook_id in self.approved_workbooks:
                from .state_record_migration import migrate
                migrate(store)
            try:yield
            finally:
                if store.mode=='ready':
                    from .state_record_cleanup import prune
                    prune(store)
    def _commit(self,previous,fields):
        if self.holder and self.record_store.mode:
            return self.record_store.commit(previous,fields)
        return super()._commit(previous,fields)
    def expire(self,now):
        super().expire(now)
        if self.record_store.mode=='ready':self.record_store.install()
    def model_record_count(self):
        return self.record_store.count_models() if self.record_store.mode=='ready' else len(self.model_runs)
    def select_workbook(self,workbook_id,*,legacy_workbook):
        if workbook_id not in self.approved_workbooks:raise LiveDisabled('Known workbook required')
        previous=self.workbook_id or legacy_workbook;parts=None
        if previous!=workbook_id:
            self.history_workspaces[previous]=self._parts();parts=self.history_workspaces.pop(workbook_id,None)
        if self.record_store.mode=='ready':
            from .persistence import workspace_snapshot,restore_workspace
            from .state import MemoryState
            if previous!=workbook_id:
                self.workspaces[previous]=workspace_snapshot(self,include_records=False)
                restore_workspace(self,self.workspaces.pop(workbook_id,None) or workspace_snapshot(MemoryState()))
            self.workbook_id=workbook_id
        else:super().select_workbook(workbook_id,legacy_workbook=legacy_workbook)
        if previous!=workbook_id:
            from .raw_history import FirestoreJournal
            journal=FirestoreJournal(self.client,self.database,self.owner_id,workbook_id)
            journal.auth_secret=self.raw_journal.auth_secret;self.raw_journal=journal
            if parts:
                core=state_snapshot(self,self.owner_id,include_records=self.record_store.mode!='ready')
                if self.record_store.mode=='ready':
                    core['history24']={'current':parts}
                    self.record_store.validation_actions(core)
                    core.pop('history24')
                restore_history(self,{**parts,'core':core},self.owner_id,max_snapshot_bytes=self.history_snapshot_bytes)
            else:
                self.history=[];self.drafts={};self.draft_proofs={};self.history_removed=0
                self.calendar_drafts={};self.calendar_proofs={}
                self.calendar_batches={};self.calendar_batch_proofs={}
                self.calendar_connections=None;self.calendar_connection_probe=None
                self.catalog_aliases={};self.owner_features_used=False;self.activity_labels={}
            if self.record_store.mode=='ready':self.record_store.hydrate()

def production_binding(state,sheets,port):
    from .pilot_http import PilotPort
    from .sheets_api import SheetsApi
    from .firestore_expenses import FirestoreExpenseStore
    if type(sheets) is FirestoreExpenseStore:
        return (type(state) is ProductionHistoryState and type(sheets.client) is PilotPort and type(port) is PilotPort
            and port.service=='model' and sheets.client.http is state.client.http is port.http
            and sheets.state is state and sheets.target==port.http.config.firestore_expense_target()
            and getattr(port.http,'expense_store',None) is sheets and sheets.effect_guard==state.before_effect)
    return (type(state) is ProductionHistoryState and type(sheets) is SheetsApi
            and type(sheets.client) is PilotPort and type(port) is PilotPort and port.service=='model'
            and sheets.client.http is state.client.http is port.http
            and sheets.target==port.http.config.sheet_target() and sheets.effect_guard==state.before_effect)
