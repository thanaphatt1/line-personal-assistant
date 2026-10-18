"""History inside the existing v1 core; bounded lossless codec and rollback."""
from copy import deepcopy
from .firestore_state import FirestoreState
from .history_context import HistoryState
from .history_persistence import history_snapshot, restore_history
from .persistence import state_snapshot, restore_state

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
        self.history_workspaces={}
        from .raw_history import FirestoreJournal
        self.raw_journal=FirestoreJournal(client,self.database,self.owner_id,c.storage_scope_id)
    @property
    def approved_workbooks(self):
        return self.client.http.config.approved_workbooks
    def _parts(self):
        v=history_snapshot(self);v.pop('core');return v
    def _snapshot_state(self):
        return {**state_snapshot(self,self.owner_id),'history24':{'active_workbook':self.workbook_id,
                'current':self._parts(),'workspaces':deepcopy(self.history_workspaces)}}
    def _restore_state(self,value):
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
            fresh=HistoryState();fresh.raw_journal=journal_for(book);fresh.clock=self.clock;restore_history(fresh,{**parts,'core':core},self.owner_id)
        restore_history(self,{**extra['current'],'core':core},self.owner_id)
        self.history_workspaces=deepcopy(extra['workspaces'])
    def select_workbook(self,workbook_id,*,legacy_workbook):
        if workbook_id not in self.approved_workbooks:raise LiveDisabled('Known workbook required')
        previous=self.workbook_id or legacy_workbook;parts=None
        if previous!=workbook_id:
            self.history_workspaces[previous]=self._parts();parts=self.history_workspaces.pop(workbook_id,None)
        super().select_workbook(workbook_id,legacy_workbook=legacy_workbook)
        if previous!=workbook_id:
            from .raw_history import FirestoreJournal
            journal=FirestoreJournal(self.client,self.database,self.owner_id,workbook_id)
            journal.auth_secret=self.raw_journal.auth_secret;self.raw_journal=journal
            if parts:restore_history(self,{**parts,'core':state_snapshot(self,self.owner_id)},self.owner_id)
            else:
                self.history=[];self.drafts={};self.draft_proofs={};self.history_removed=0
                self.calendar_drafts={};self.calendar_proofs={}
                self.calendar_batches={};self.calendar_batch_proofs={}

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
