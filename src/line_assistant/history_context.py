"""Local-only rolling history; does not change production state or persistence."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib,json
from .state import MemoryState
from .language import BANGKOK
from .model_api import MODEL_ID,MAX_BODY_BYTES,MAX_OUTPUT_TOKENS,MAX_INPUT_TOKENS_BOUND

WINDOW=timedelta(hours=24)
MAX_HISTORY_BYTES=131072
MAX_DRAFTS=8

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def source_ref(event):return 'msg_'+hashlib.sha256(event.encode()).hexdigest()[:20]
def draft_fingerprint(d):
    # Confirmation/status are transient; validated values and their provenance
    # are a separate server-owned record, never supplied by a model tool.
    return digest({k:d[k] for k in ['draft_ref','owner_id','version','entries','created_at','updated_at','expires_at','catalog_hash']})

class HistoryState(MemoryState):
    """Separate mock state. No 6/10-turn cap and no midnight reset."""
    def __init__(self,*,history_bytes=MAX_HISTORY_BYTES):
        super().__init__()
        if type(history_bytes) is not int or not 1024<=history_bytes<=MAX_HISTORY_BYTES:raise ValueError('Bounded local history required')
        self.history=[];self.history_bytes=history_bytes;self.history_removed=0
        self.drafts={};self.draft_proofs={};self.owner_id=None
        self.calendar_drafts={};self.calendar_proofs={}
        self.calendar_batches={};self.calendar_batch_proofs={}
        self.calendar_connections=None;self.calendar_connection_probe=None
        self.catalog_aliases={}
        self.owner_features_used=False
        self.activity_labels={}
        self.raw_history_enabled=False;self.raw_history_migrated=False;self.raw_journal=None;self.rolling_summary=None
        self.raw_tool_definitions=[]
        self.clock=lambda:datetime.now(timezone.utc)

    def enable_raw_history(self,journal=None):
        from .raw_history import MemoryJournal
        if self.raw_history_enabled and self.raw_history_migrated and (journal is None or journal is self.raw_journal):return
        if journal is not None:self.raw_journal=journal
        if self.raw_journal is None:self.raw_journal=MemoryJournal()
        self.raw_history_enabled=True
        # Migrate retained original owner rows. Assistant delivery is verified
        # separately before journal publication; tool records are not speakers.
        for i,row in enumerate(self.history):
            row.setdefault('sequence',i)
            if row['role']=='user':self.raw_journal.put(row)
        if not self.history:self.raw_history_migrated=True

    def mark_history_delivered(self,event,result,*,at=None):
        if not self.raw_history_enabled:return
        from .raw_history import message_hash,rendered_message
        existing=next((r for r in self.history if r['event']==event and r['role']=='assistant'),None)
        if existing and existing.get('delivery_verified'):return
        receipt=self.receipts.get(event)
        if not receipt or receipt.result!=result:return
        stamp=(at or (datetime.fromisoformat(existing['at']) if existing else self.clock())).isoformat()
        row={'role':'assistant','event':event,'at':stamp,'text':result.message,
             'sequence':existing.get('sequence',len(self.history)) if existing else len(self.history),
             'delivery_verified':True,'content_hash':message_hash(rendered_message(result))}
        self.raw_journal.put(row)
        self.history=[r for r in self.history if not(r['event']==event and r['role']=='assistant')]+[row]

    def expire(self,now):
        # A partially completed Calendar set owns its successful child proofs
        # until the remaining uncertain effects are resolved, even past TTL.
        protected_keys={item['key'] for a in self.actions.values() if a.metadata.get('calendar_batch') and a.status!='completed' for item in a.payload['members']}
        protected_actions={k:v for k,v in self.actions.items() if k in protected_keys}
        super().expire(now)
        self.actions.update(protected_actions)
        cutoff=now-WINDOW
        retained=[r for r in self.history if datetime.fromisoformat(r['at'])>cutoff]
        self.history_removed+=len(self.history)-len(retained);self.history=retained
        self.drafts={k:v for k,v in self.drafts.items() if datetime.fromisoformat(v['expires_at'])>now}
        self.draft_proofs={k:v for k,v in self.draft_proofs.items() if k in self.drafts}
        self.calendar_batches={k:b for k,b in self.calendar_batches.items() if datetime.fromisoformat(b['expires_at'])>now or b['status']=='committing'}
        self.calendar_batch_proofs={k:v for k,v in self.calendar_batch_proofs.items() if k in self.calendar_batches}
        protected={r for b in self.calendar_batches.values() for r in b['members']}
        self.calendar_drafts={k:v for k,v in self.calendar_drafts.items() if datetime.fromisoformat(v['expires_at'])>now or v['status']=='committing' or k in protected}
        self.calendar_proofs={k:v for k,v in self.calendar_proofs.items() if k in self.calendar_drafts}
        if self.rolling_summary and datetime.fromisoformat(self.rolling_summary['expires_at'])<=now:self.rolling_summary=None

    def append(self,role,event,at,*,text=None,tool=None,result=None):
        if any(r['event']==event and r['role']==role and r.get('tool')==tool and (role!='tool' or r.get('result')==result) for r in self.history):return
        row={'role':role,'event':event,'at':at.isoformat()}
        if self.raw_history_enabled and role=='user':
            from .raw_history import RAW_SOURCE
            original=RAW_SOURCE.get()
            if original and original[0]==event and isinstance(original[1],str):text=original[1]
        if text is not None:row['text']=text
        if role=='user':row['source_ref']=source_ref(event)
        if tool is not None:row.update(tool=tool,result=deepcopy(result))
        if self.raw_history_enabled:
            row['sequence']=max((r.get('sequence',0) for r in self.history),default=-1)+1
            if role=='user':self.raw_journal.put(row)
        self.history.append(row)
        if self.raw_history_enabled:return
        while len(json.dumps(self.history,ensure_ascii=False,separators=(',',':')).encode())>self.history_bytes:
            oldest=self.history[0]['event']
            removed=[r for r in self.history if r['event']==oldest]
            self.history=[r for r in self.history if r['event']!=oldest]
            self.history_removed+=len(removed)

def draft_index(state):
    result=[]
    for k,d in state.drafts.items():
        p=state.draft_proofs.get(k)
        if not p or d.get('owner_id')!=state.owner_id or p.get('owner_id')!=state.owner_id or p.get('version')!=d.get('version'):continue
        try:verified=p['fingerprint']==draft_fingerprint(d)
        except (KeyError,TypeError):verified=False
        if verified and d['status']!='recorded':result.append({'draft_ref':k,'version':d['version'],'status':d['status'],
                'items':[(e['item'] or '')[:32] for e in d['entries']],'entry_count':len(d['entries'])})
    return result

def build_request(state,text,at,event,definitions,instructions,previous=None,*,max_body_bytes=MAX_BODY_BYTES,semantic=False):
    if not 3000<=max_body_bytes<=MAX_BODY_BYTES:raise ValueError('Existing request cap cannot be raised')
    # The current message is separate and never removed. History remains one
    # chronological sequence of messages and server-produced tool results.
    history=[{k:deepcopy(v) for k,v in r.items() if k not in {'event'}} for r in state.history if r['event']!=event]
    removed=0
    while True:
        context={'bangkok_datetime':at.astimezone(BANGKOK).isoformat(),'message':text,
                 'current_source_ref':source_ref(event),'history_window_hours':24,
                 'history':history,'available_drafts':draft_index(state),
                 'context_removed':{'request_records':removed,'storage_or_expiry_records':state.history_removed}}
        if semantic:
            for view in context['available_drafts']:
                view['entries']=[{k:e[k] for k in ['entry_ref','item','amount','date']} for e in state.drafts[view['draft_ref']]['entries']]
            pending=[e for d in state.drafts.values() if d['status']!='recorded' for e in d['entries']]
            recent=[r for r in state.history if r['role']=='user' and r['event']!=event and any(e['item'] and e['item'] in r['text'] for e in pending)]
            if recent:context['recent_owner_facts']=[{k:r[k] for k in ['source_ref','text','at']} for r in recent[-2:]]
        request={'model':MODEL_ID,'instructions':instructions,'input':[{'role':'user','content':json.dumps(context,ensure_ascii=False,separators=(',',':'))}],
                 'tools':deepcopy(definitions),'tool_choice':'auto','parallel_tool_calls':False,
                 'reasoning':{'effort':'none'},'max_output_tokens':MAX_OUTPUT_TOKENS,'store':False,'service_tier':'default'}
        if previous:
            call,result=previous
            request['input']+=[deepcopy(call),{'type':'function_call_output','call_id':call['call_id'],'output':json.dumps(result,ensure_ascii=False,separators=(',',':'))}]
        size=len(json.dumps(request,ensure_ascii=False,separators=(',',':')).encode())
        # UTF-8 byte count + overhead is a deliberately conservative token
        # upper bound; exact model-token counts require later live validation.
        if size<=max_body_bytes and size+256<=MAX_INPUT_TOKENS_BOUND:
            visible={r['source_ref'] for r in history if r['role']=='user'}|{source_ref(event)}
            if semantic:visible.update(r['source_ref'] for r in context.get('recent_owner_facts',[]))
            return request,visible,{'utf8_bytes':size,'token_upper_bound':size+256,'removed_records':removed}
        if not history:raise ValueError('protected_context_too_large')
        # Remove an old whole turn, keeping its tool outputs with the message.
        stamp=history[0]['at'];before=len(history);history=[r for r in history if r['at']!=stamp]
        removed+=before-len(history)
