"""Fixed-target Calendar writes and optional discovered-calendar saved reads.

No credential discovery or direct network client. Production must explicitly
bind a guarded PilotPort; tests inject a MockTransport-backed RestClient.
"""
from datetime import datetime,date,time,timedelta
from copy import deepcopy
from urllib.parse import quote
import hashlib,json,re
from .adapters import CalendarEvent,Conflict,Reconciliation
from .contracts import CreateEvent
from .calendar_event_types import parse_payload
from .google_requests import timed
from .language import BANGKOK
from .transport import ApiError,OutcomeUnknown,WriteBlocked,NotReady

CALENDAR_LIST_PATH='/calendar/v3/users/me/calendarList'
CALENDAR_LIST_PARAMS={'maxResults':20,'minAccessRole':'reader','showHidden':'true','showDeleted':'false'}
MAX_READ_CALENDARS=16
MAX_QUERY_DAYS=1096
MAX_EVENT_PAGES=20
READ_PAGE_SIZE=50

def additional_calendar_ids(identifiers,calendar_id):
    if (not isinstance(identifiers,(list,tuple)) or len(identifiers)>=MAX_READ_CALENDARS
        or any(not isinstance(identifier,str) or not 1<=len(identifier)<=512
            or not re.fullmatch(r'[A-Za-z0-9_.+#-]+@[A-Za-z0-9.-]+',identifier) for identifier in identifiers)
        or len(set(identifiers))!=len(identifiers) or calendar_id in identifiers):
        raise ValueError('Explicit additional Calendar IDs required')
    return tuple(identifiers)

def calendar_list_page(value):
    """Validate provider discovery before admitting any additional read target."""
    if not isinstance(value,dict):raise NotReady('Calendar list malformed')
    items=value.get('items',[] if value.get('kind')=='calendar#calendarList' else None)
    if not isinstance(items,list) or len(items)>20:raise NotReady('Calendar list malformed')
    identifiers=[]
    for item in items:
        if not isinstance(item,dict):raise NotReady('Calendar list entry malformed')
        if item.get('deleted') is True or item.get('accessRole') in {'none','freeBusyReader'}:continue
        identifier=item.get('id')
        if (item.get('accessRole') not in {'reader','writer','owner','writerWithoutPrivateAccess'}
            or not isinstance(identifier,str) or not re.fullmatch(r'[\x21-\x7e]{1,512}',identifier) or identifier=='primary'):
            raise NotReady('Calendar list entry unavailable')
        identifiers.append(identifier)
    page=value.get('nextPageToken')
    if page is not None and (not isinstance(page,str) or not 1<=len(page)<=1024):raise NotReady('Calendar list token malformed')
    return identifiers,page

def creation_body(args,key,calendar_id):
    args=parse_payload(args.model_dump())
    if args.reminder_minutes is not None:raise ValueError('calendar_reminders_not_supported')
    if not key:raise ValueError('calendar_action_key_required')
    payload=args.model_dump(mode='json')
    proof=hashlib.sha256(json.dumps([calendar_id,payload],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'id':hashlib.sha256(key.encode()).hexdigest(),'summary':args.title,
            'start':{'date':args.start.isoformat()} if getattr(args,'all_day',False) else timed(args.start),
            'end':{'date':args.end.isoformat()} if getattr(args,'all_day',False) else timed(args.end),
            'reminders':{'useDefault':False,'overrides':[]},
            'extendedProperties':{'private':{'line_action':hashlib.sha256(key.encode()).hexdigest(),'line_payload':proof}}}

def matches(value,args,key,calendar_id):
    wanted=creation_body(args,key,calendar_id)
    try:
        if getattr(args,'all_day',False):
            boundaries=(set(value['start'])=={'date'} and set(value['end'])=={'date'}
                and value['start']['date']==args.start.isoformat() and value['end']['date']==args.end.isoformat())
        else:
            boundaries=('date' not in value['start'] and 'date' not in value['end']
                and datetime.fromisoformat(value['start']['dateTime'].replace('Z','+00:00'))==args.start
                and datetime.fromisoformat(value['end']['dateTime'].replace('Z','+00:00'))==args.end)
        return (value['id']==wanted['id'] and value.get('summary')==args.title and value.get('etag')
            and boundaries
            and value.get('extendedProperties',{}).get('private',{}).get('line_action')==wanted['extendedProperties']['private']['line_action']
            and value.get('extendedProperties',{}).get('private',{}).get('line_payload')==wanted['extendedProperties']['private']['line_payload']
            and value.get('reminders',{}).get('useDefault') is False and not value.get('reminders',{}).get('overrides',[])
            and not value.get('attendees') and not value.get('recurrence') and value.get('status')!='cancelled')
    except (KeyError,TypeError,ValueError):return False

def receipt(value):
    def boundary(field):
        return date.fromisoformat(value[field]['date']) if 'date' in value[field] else datetime.fromisoformat(value[field]['dateTime'].replace('Z','+00:00'))
    return CalendarEvent(value['id'],value['summary'],boundary('start'),boundary('end'),None,value['etag'],{'useDefault':False,'overrides':[]})

class CalendarCreationApi:
    def __init__(self,client,calendar_id,*,writes_enabled=False,effect_guard=None,read_all_enabled=False,calendar_read_ids=(),connected_ids=None):
        if client.service!='calendar' or not isinstance(calendar_id,str) or not 1<=len(calendar_id)<=512 or calendar_id=='primary':raise ValueError('Explicit server Calendar ID required')
        self.client=client;self.calendar_id=calendar_id;self.writes_enabled=writes_enabled
        self.base='/calendar/v3/calendars/'+quote(calendar_id,safe='')+'/events'
        self.effect_guard=effect_guard or (lambda:None)
        self.mocked=client.mocked
        self.read_all_enabled=read_all_enabled
        self.calendar_read_ids=additional_calendar_ids(calendar_read_ids,calendar_id)
        self.connected_ids=connected_ids or (lambda:())
    def create(self,args,key):
        if not self.writes_enabled:raise WriteBlocked('Calendar creation is disabled')
        body=creation_body(args,key,self.calendar_id);self.effect_guard()
        try:value=self.client.request('POST',self.base,body=body)
        except ApiError as error:
            if error.status in {401,403}:raise WriteBlocked('Calendar access/setup required') from None
            if error.status!=409:raise
            proof=self.reconcile(key,args.model_dump(mode='json'))
            if proof.outcome!='committed':raise Conflict('Calendar ID exists with another payload') from None
            return proof.receipt
        if not matches(value,args,key,self.calendar_id):raise OutcomeUnknown('Calendar acknowledgement does not prove the frozen creation')
        return receipt(value)
    def reconcile(self,key,payload):
        args=parse_payload(payload);event_id=hashlib.sha256(key.encode()).hexdigest()
        try:value=self.client.request('GET',self.base+'/'+event_id)
        except ApiError as error:
            if error.status in {404,410}:return Reconciliation('unknown')
            raise
        return Reconciliation('committed',receipt(value)) if matches(value,args,key,self.calendar_id) else Reconciliation('unknown')
    def conflicts(self,start,end):
        result=[];page=None;seen=set()
        for _ in range(3):
            params={'timeMin':start.isoformat(),'timeMax':end.isoformat(),'singleEvents':'true','orderBy':'startTime','timeZone':'Asia/Bangkok','maxResults':20}
            if page:params['pageToken']=page
            value=self.client.request('GET',self.base,params=params)
            for event in value.get('items',[]):
                if event.get('status')=='cancelled' or event.get('transparency')=='transparent':continue
                def boundary(field):
                    point=event[field]
                    if 'dateTime' in point:
                        value=datetime.fromisoformat(point['dateTime'].replace('Z','+00:00'))
                        if value.utcoffset() is None:raise NotReady('Calendar conflict offset missing')
                        return value
                    return datetime.combine(date.fromisoformat(point['date']),time.min,BANGKOK)
                a,b=boundary('start'),boundary('end')
                if a<end and start<b:result.append({'id':event['id'],'start':a.isoformat(),'end':b.isoformat()})
            page=value.get('nextPageToken')
            if not page:return sorted(result,key=lambda x:(x['start'],x['id']))
            if page in seen:break
            seen.add(page)
        raise NotReady('Calendar conflict result is incomplete')

    def readable_calendars(self):
        identifiers=dict.fromkeys([self.calendar_id,*self.calendar_read_ids,*self.connected_ids()]);page=None;seen=set()
        if len(identifiers)>MAX_READ_CALENDARS:raise NotReady('Calendar list exceeds read capacity')
        if not self.read_all_enabled:return tuple(identifiers)
        for _ in range(3):
            params={**CALENDAR_LIST_PARAMS,**({'pageToken':page} if page else {})}
            found,page=calendar_list_page(self.client.request('GET',CALENDAR_LIST_PATH,params=params))
            identifiers.update(dict.fromkeys(found))
            if len(identifiers)>MAX_READ_CALENDARS:raise NotReady('Calendar list exceeds read capacity')
            if not page:return tuple(identifiers)
            if page in seen:break
            seen.add(page)
        raise NotReady('Calendar list is incomplete')

    def verify_read_access(self,calendar_id,params):
        additional_calendar_ids([calendar_id],self.calendar_id)
        value=self.client.request('GET','/calendar/v3/calendars/'+quote(calendar_id,safe='')+'/events',params=params)
        if (not isinstance(value,dict) or value.get('kind')!='calendar#events'
            or value.get('accessRole') not in {'reader','writer','owner','writerWithoutPrivateAccess'}):
            raise NotReady('Calendar event-reading role unavailable')
        return value['accessRole']

    def saved_events(self,start,end,*,include_details=False,include_refs=False):
        """Read every selected calendar completely before returning an answer."""
        if start.utcoffset() is None or end.utcoffset() is None or not timedelta(0)<end-start<=timedelta(days=MAX_QUERY_DAYS):
            raise NotReady('Calendar query requires an offset and a bounded window')
        result={}
        for calendar_id in self.readable_calendars():
            for identity,row in self._saved_events(calendar_id,start,end,include_details=include_details):
                if include_refs:row={**row,'read_ref':hashlib.sha256(json.dumps(identity,separators=(',',':')).encode()).hexdigest()}
                # Copies of an invitation share iCalUID. Preserve separate
                # recurring occurrences and unrelated events with equal titles.
                result.setdefault(identity,row)
        return sorted(result.values(),key=lambda e:(e['start'],e['title']))

    def _saved_events(self,calendar_id,start,end,*,include_details=False):
        base='/calendar/v3/calendars/'+quote(calendar_id,safe='')+'/events'
        result={};page=None;seen=set()
        for _ in range(MAX_EVENT_PAGES):
            params={'timeMin':start.isoformat(),'timeMax':end.isoformat(),'singleEvents':'true','orderBy':'startTime','timeZone':'Asia/Bangkok','maxResults':READ_PAGE_SIZE}
            if page:params['pageToken']=page
            value=self.client.request('GET',base,params=params)
            if not isinstance(value,dict):raise NotReady('Calendar page malformed')
            items=value.get('items',[] if value.get('kind')=='calendar#events' else None)
            if not isinstance(items,list) or len(items)>READ_PAGE_SIZE:raise NotReady('Calendar page malformed')
            for event in items:
                if not isinstance(event,dict):raise NotReady('Calendar event malformed')
                if event.get('status')=='cancelled':continue
                try:
                    identifier=event['id'];title=event.get('summary') or '(ไม่มีชื่อ)'
                    if not isinstance(identifier,str) or not 1<=len(identifier)<=1024 or not isinstance(title,str) or len(title)>500:raise ValueError()
                    all_day='date' in event['start']
                    if all_day != ('date' in event['end']):raise ValueError()
                    def boundary(field):
                        point=event[field]
                        if all_day:
                            if 'dateTime' in point:raise ValueError()
                            return datetime.combine(date.fromisoformat(point['date']),time.min,BANGKOK)
                        instant=datetime.fromisoformat(point['dateTime'].replace('Z','+00:00'))
                        if instant.utcoffset() is None:raise ValueError()
                        return instant.astimezone(BANGKOK)
                    a,b=boundary('start'),boundary('end')
                    if b<=a:raise ValueError()
                except (ValueError,TypeError,KeyError,OverflowError):raise NotReady('Calendar event boundaries unavailable') from None
                if a<end and start<b:
                    row={'title':title,'start':a.date().isoformat() if all_day else a.isoformat(),
                         'end':b.date().isoformat() if all_day else b.isoformat(),'all_day':all_day}
                    if include_details:
                        row['details_truncated']=False
                        for field,limit in [('description',1000),('location',500)]:
                            text=event.get(field,'')
                            if not isinstance(text,str):raise NotReady('Calendar event details malformed')
                            row[field]=text[:limit]
                            row['details_truncated']|=len(text)>limit
                    uid=event.get('iCalUID')
                    if uid is not None and (not isinstance(uid,str) or not 1<=len(uid)<=1024):raise NotReady('Calendar event identity malformed')
                    identity=('ical',uid,row['start'],row['end'],all_day) if uid else ('event',calendar_id,identifier)
                    record=(identity,row)
                    if identifier in result and result[identifier]!=record:raise NotReady('Calendar pages changed during read')
                    result[identifier]=record
            page=value.get('nextPageToken')
            if not page:return list(result.values())
            if not isinstance(page,str) or len(page)>1024 or page in seen:break
            seen.add(page)
        raise NotReady('Calendar saved-event result is incomplete')
