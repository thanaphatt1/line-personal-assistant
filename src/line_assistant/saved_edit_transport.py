"""Allow only owned saved-record reads and one sealed, confirmed mutation."""
from urllib.parse import quote
from .history_context import digest
from .expense_edits import edit_body,expense_after
from .calendar_reschedule import reschedule_body,rescheduled_times
from .transport import LiveDisabled

def owned(http):
    c,s=http.config,http.state
    return bool(c.history_enabled and c.history_state_enabled and s and s.holder and
        s.owner_id==c.owner_id and s.production_ready and s.workbook_id==c.storage_scope_id)

def actions(http):
    c=http.config
    for key,a in http.state.actions.items():
        m,p=a.metadata,a.payload;domain=p.get('domain')
        target=c.storage_scope_id if domain=='expense' else c.calendar_id
        if (domain not in {'expense','calendar'} or not key.startswith(c.owner_id+':savededit:')
            or a.kind!=('delete_saved_expense' if p.get('operation')=='delete' else 'edit_saved_expense' if domain=='expense' else 'reschedule_saved_event')
            or not m.get('saved_record_edit') or m.get('owner')!=c.owner_id or m.get('workbook')!=c.storage_scope_id
            or m.get('target')!=target or m.get('payload_hash')!=digest(p)):continue
        yield key,a

def calendar_read(http,path):
    c,s=http.config,http.state
    if not c.calendar_enabled or not owned(http):return False
    ids=set()
    p=s.pending
    if p and p.kind.startswith('saved_record_'):
        v=p.payload
        if (v.get('owner')==c.owner_id and v.get('workbook')==c.storage_scope_id and v.get('target')==c.calendar_id
            and v.get('domain')=='calendar' and v.get('seal')==digest({k:x for k,x in v.items() if k!='seal'})):
            ids.update(r['record']['id'] for r in v.get('candidates',[]))
    for _,a in actions(http):
        if a.payload['domain']=='calendar':ids.add(a.payload['id'])
    # Existing confirmed creation receipts may supply scope=last.
    from .calendar_transport import completed_creation_id
    for key,a in s.actions.items():
        identifier=completed_creation_id(key,a,c.owner_id,c.calendar_id)
        if identifier is not None:ids.add(identifier)
    base='/calendar/v3/calendars/'+quote(c.calendar_id,safe='')+'/events/'
    return any(path==base+quote(i,safe='') for i in ids)

def mutation(http,service,method,path,body,params,headers=None):
    if not owned(http):return False
    c=http.config
    for key,a in actions(http):
        p,m=a.payload,a.metadata;f=m.get('request_fence')
        if a.status!='executing' or not isinstance(f,dict) or f.get('body_hash')!=digest(body):continue
        proof=f.get('proof',{})
        if proof.get('version')!=p['before']['version'] or proof.get('id')!=p['id']:continue
        domain=p['domain']
        if domain=='expense':
            if not (c.saved_expense_enabled and c.saved_expense_concurrency=='reviewed_single_writer'):continue
            if (service!='sheets' or method!='POST' or path!='/v4/spreadsheets/'+c.storage_scope_id+':batchUpdate'
                or params is not None or headers):continue
            if p.get('operation')=='delete':
                if not c.saved_expense_delete_enabled or p['after'] is not None or p['changes']!={'operation':'delete'}:continue
            elif p['after']!=expense_after(p['before'],p['changes']):continue
            if body!=edit_body(c.sheet_target(),p,proof.get('row')):continue
        else:
            if not c.calendar_enabled or not c.saved_calendar_enabled:continue
            if (service!='calendar' or method!='PATCH' or path!='/calendar/v3/calendars/'+quote(c.calendar_id,safe='')+'/events/'+quote(p['id'],safe='')
                or params!={'sendUpdates':'none'} or headers!={'If-Match':p['before']['etag']} or proof.get('etag')!=p['before']['etag']):continue
            if p['after']!=rescheduled_times(p['before'],p['changes']) or body!=reschedule_body(p,key):continue
        attempted=getattr(http,'saved_edit_attempted_keys',set())
        if key in attempted:raise LiveDisabled('Saved-record mutation already attempted; reconcile only')
        attempted.add(key);http.saved_edit_attempted_keys=attempted;return True
    return False
