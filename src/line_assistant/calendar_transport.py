"""Exact Calendar resource/method/payload gates for the existing fenced runtime."""
from datetime import datetime,timedelta
from urllib.parse import quote
import hashlib,re
from .calendar_provider import creation_body
from .calendar_event_types import parse_payload
from .history_context import digest
from .transport import LiveDisabled

def authorize(http,method,path,body,params):
    c=http.config;s=http.state
    if (not getattr(c,'calendar_enabled',False) or not c.history_enabled or not s or not s.holder
        or s.owner_id!=c.owner_id or not s.production_ready or s.workbook_id!=c.storage_scope_id):raise LiveDisabled('Calendar requires explicit configuration and owner lease')
    base='/calendar/v3/calendars/'+quote(c.calendar_id,safe='')+'/events'
    if method=='GET' and path==base and body is None and isinstance(params,dict):
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
