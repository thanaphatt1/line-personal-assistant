"""Exact Calendar resource/method/payload gates for the existing fenced runtime."""
from datetime import datetime,timedelta
from urllib.parse import quote
import hashlib,re
from .calendar_provider import creation_body,CALENDAR_LIST_PATH,CALENDAR_LIST_PARAMS
from .calendar_event_types import parse_payload
from .history_context import digest
from .transport import LiveDisabled

def authorize(http,method,path,body,params):
    c=http.config;s=http.state
    if (not getattr(c,'calendar_enabled',False) or not c.history_enabled or not s or not s.holder
        or s.owner_id!=c.owner_id or not s.production_ready or s.workbook_id!=c.storage_scope_id):raise LiveDisabled('Calendar requires explicit configuration and owner lease')
    base='/calendar/v3/calendars/'+quote(c.calendar_id,safe='')+'/events'
    from .calendar_connections import probe_allowed,connected_ids
    if probe_allowed(http,method,path,body,params):return
    all_reads=getattr(c,'calendar_read_all_enabled',False)
    if all_reads and method=='GET' and path==CALENDAR_LIST_PATH and body is None and isinstance(params,dict):
        if (set(params) not in [set(CALENDAR_LIST_PARAMS),set(CALENDAR_LIST_PARAMS)|{'pageToken'}]
            or any(type(params.get(k)) is not type(v) or params[k]!=v for k,v in CALENDAR_LIST_PARAMS.items())
            or 'pageToken' in params and (not isinstance(params['pageToken'],str) or not 1<=len(params['pageToken'])<=1024
                or params['pageToken']!=getattr(http,'calendar_next_page',None)
                or getattr(http,'calendar_discovery_holder',None)!=s.holder)):
            raise LiveDisabled('Invalid Calendar discovery query')
        return
    read_paths={base}
    read_paths.update('/calendar/v3/calendars/'+quote(identifier,safe='')+'/events' for identifier in getattr(c,'calendar_read_ids',()))
    if getattr(c,'calendar_connections_enabled',False):
        read_paths.update('/calendar/v3/calendars/'+quote(identifier,safe='')+'/events' for identifier in connected_ids(s,c.owner_id,c.calendar_id))
    if all_reads and getattr(http,'calendar_read_holder',None)==s.holder:
        read_paths.update('/calendar/v3/calendars/'+quote(identifier,safe='')+'/events' for identifier in http.calendar_read_ids)
    if method=='GET' and path in read_paths and body is None and isinstance(params,dict):
        required={'timeMin','timeMax','singleEvents','orderBy','timeZone','maxResults'}
        if set(params) not in [required,required|{'pageToken'}]:raise LiveDisabled('Unbounded Calendar conflict query')
        try:a=datetime.fromisoformat(params['timeMin']);b=datetime.fromisoformat(params['timeMax'])
        except (ValueError,TypeError):raise LiveDisabled('Invalid Calendar time window') from None
        if (a.utcoffset() is None or b.utcoffset() is None or not timedelta(0)<b-a<=timedelta(days=7)
            or params['singleEvents']!='true' or params['orderBy']!='startTime' or params['timeZone']!='Asia/Bangkok' or type(params['maxResults']) is not int or params['maxResults']!=20
            or 'pageToken' in params and (not isinstance(params['pageToken'],str) or not 1<=len(params['pageToken'])<=1024)):
            raise LiveDisabled('Invalid Calendar query bounds')
        return
    for key,action in s.actions.items():
        m=action.metadata
        if (not key.startswith(c.owner_id+':calendar:') or action.kind!='create_event' or not m.get('calendar_mvp') or m.get('calendar_id')!=c.calendar_id
            or action.status not in {'executing','uncertain'} or digest([c.owner_id,c.calendar_id,action.payload])!=m.get('calendar_payload_hash')):continue
        event_id=hashlib.sha256(key.encode()).hexdigest()
        if method=='GET' and path==base+'/'+event_id and body is None and params is None:return
        if method=='POST' and path==base and params is None and action.status=='executing':
            if body!=creation_body(parse_payload(action.payload),key,c.calendar_id):raise LiveDisabled('Calendar body differs from confirmed action')
            if key in http.calendar_attempted_keys:raise LiveDisabled('Calendar creation already attempted; reconcile only')
            http.calendar_attempted_keys.add(key);return
    raise LiveDisabled('Calendar route/action is outside the confirmed creation')
