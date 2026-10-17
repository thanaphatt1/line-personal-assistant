"""Creation-only Calendar port with fixed resource and positive reconciliation.

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
    def __init__(self,client,calendar_id,*,writes_enabled=False,effect_guard=None):
        if client.service!='calendar' or not isinstance(calendar_id,str) or not 1<=len(calendar_id)<=512 or calendar_id=='primary':raise ValueError('Explicit server Calendar ID required')
        self.client=client;self.calendar_id=calendar_id;self.writes_enabled=writes_enabled
        self.base='/calendar/v3/calendars/'+quote(calendar_id,safe='')+'/events'
        self.effect_guard=effect_guard or (lambda:None)
        self.mocked=client.mocked
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

    def saved_events(self,start,end):
        """List saved appointments, including transparent/all-day occurrences.

        Uses precisely the already permitted seven-day conflict GET surface.
        Failure to finish pagination is not an empty calendar.
        """
        if start.utcoffset() is None or end.utcoffset() is None or not timedelta(0)<end-start<=timedelta(days=7):
            raise NotReady('Calendar query requires an offset and at most seven days')
        result={};page=None;seen=set()
        for _ in range(3):
            params={'timeMin':start.isoformat(),'timeMax':end.isoformat(),'singleEvents':'true','orderBy':'startTime','timeZone':'Asia/Bangkok','maxResults':20}
            if page:params['pageToken']=page
            value=self.client.request('GET',self.base,params=params)
            items=value.get('items',[] if value.get('kind')=='calendar#events' else None)
            if not isinstance(items,list) or len(items)>20:raise NotReady('Calendar page malformed')
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
                    if identifier in result and result[identifier]!=row:raise NotReady('Calendar pages changed during read')
                    result[identifier]=row
            page=value.get('nextPageToken')
            if not page:return sorted(result.values(),key=lambda e:(e['start'],e['title']))
            if not isinstance(page,str) or len(page)>1024 or page in seen:break
            seen.add(page)
        raise NotReady('Calendar saved-event result is incomplete')
