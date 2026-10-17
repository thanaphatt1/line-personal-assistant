"""Conditional rescheduling on one fixed Calendar; no credential discovery.

Uses existing calendar.events scope and resource ETags/If-Match. No deletion,
guests, recurrence, reminders, title changes or generic header passthrough.
"""
from copy import deepcopy
from datetime import date,datetime,time,timedelta
from urllib.parse import quote
import hashlib,json,re
from .adapters import Conflict,NotFound,Reconciliation
from .google_requests import target_path,timed
from .history_context import digest
from .language import BANGKOK
from .transport import ApiError,NotReady,OutcomeUnknown,WriteBlocked


def rescheduled_times(before,changes):
    if not isinstance(changes,dict) or not changes or set(changes)-{'date','time','end_date','end_time'}:raise ValueError('saved_calendar_patch_fields')
    if before['all_day']:
        if any(k in changes for k in ['time','end_time']):raise ValueError('saved_calendar_mode_change_unsupported')
        old_a=date.fromisoformat(before['start']);old_b=date.fromisoformat(before['end'])
        a=date.fromisoformat(changes['date']) if changes.get('date') else old_a
        b=date.fromisoformat(changes['end_date'])+timedelta(days=1) if changes.get('end_date') else a+(old_b-old_a)
        if not timedelta(0)<b-a<=timedelta(days=7):raise ValueError('saved_calendar_interval')
    else:
        old_a=datetime.fromisoformat(before['start']).astimezone(BANGKOK);old_b=datetime.fromisoformat(before['end']).astimezone(BANGKOK)
        day=date.fromisoformat(changes['date']) if changes.get('date') else old_a.date()
        clock=time.fromisoformat(changes['time']) if changes.get('time') else old_a.timetz().replace(tzinfo=None)
        if clock.tzinfo is not None or clock.microsecond:raise ValueError('saved_calendar_local_clock')
        a=datetime.combine(day,clock,BANGKOK)
        if any(k in changes for k in ['end_date','end_time']):
            last=date.fromisoformat(changes['end_date']) if changes.get('end_date') else a.date()
            clock=time.fromisoformat(changes['end_time']) if changes.get('end_time') else old_b.timetz().replace(tzinfo=None)
            if clock.tzinfo is not None or clock.microsecond:raise ValueError('saved_calendar_local_clock')
            b=datetime.combine(last,clock,BANGKOK)
        else:b=a+(old_b-old_a)
        if not timedelta(0)<b-a<=timedelta(days=7):raise ValueError('saved_calendar_interval')
    if not 1900<=a.year<=b.year<=2100:raise ValueError('saved_calendar_date_bound')
    after={'start':a.isoformat(),'end':b.isoformat(),'all_day':before['all_day'],'title':before['title']}
    if all(after[k]==before[k] for k in ['start','end']):raise ValueError('saved_calendar_no_change')
    return after


def reschedule_body(payload,key):
    before,after=payload['before'],payload['after']
    private=deepcopy(before['private']);private['line_edit_action']=hashlib.sha256(key.encode()).hexdigest()
    private['line_edit_payload']=digest([before['calendar'],payload['id'],after])
    return {'start':{'date':after['start']} if after['all_day'] else timed(datetime.fromisoformat(after['start'])),
        'end':{'date':after['end']} if after['all_day'] else timed(datetime.fromisoformat(after['end'])),
        'extendedProperties':{'private':private}}


class SavedCalendarEditor:
    def __init__(self,client,calendar_id,*,writes_enabled=False,effect_guard=None):
        if client.service!='calendar' or not isinstance(calendar_id,str) or not 1<=len(calendar_id)<=512 or calendar_id=='primary':raise ValueError('Bound separate Calendar required')
        if type(writes_enabled) is not bool:raise ValueError('Explicit Calendar edit flag required')
        self.client=client;self.calendar_id=calendar_id;self.base='/calendar/v3/calendars/'+quote(calendar_id,safe='')+'/events'
        self.writes_enabled=writes_enabled;self.effect_guard=effect_guard or (lambda:None);self.mocked=client.mocked

    def record(self,value):
        try:
            identifier=value['id'];etag=value['etag'];title=value.get('summary') or '(ไม่มีชื่อ)'
            if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,1024}',identifier) or not isinstance(title,str) or not 1<=len(title)<=500:raise ValueError()
            target_path(identifier,etag)
            if (value.get('status')=='cancelled' or value.get('eventType','default')!='default' or value.get('attendees')
                or value.get('recurrence') or value.get('recurringEventId') or value.get('conferenceData') or value.get('organizer',{}).get('self') is False):raise ValueError()
            all_day='date' in value['start']
            if all_day!=('date' in value['end']):raise ValueError()
            def boundary(field):
                point=value[field]
                if all_day:
                    if 'dateTime' in point:raise ValueError()
                    return date.fromisoformat(point['date'])
                result=datetime.fromisoformat(point['dateTime'].replace('Z','+00:00'))
                if result.utcoffset() is None:raise ValueError()
                return result.astimezone(BANGKOK)
            a,b=boundary('start'),boundary('end')
            if not timedelta(0)<b-a<=timedelta(days=7) or not 1900<=a.year<=b.year<=2100:raise ValueError()
            private=value.get('extendedProperties',{}).get('private',{})
            if not isinstance(private,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in private.items()) or len(json.dumps(private).encode())>4096:raise ValueError()
        except (KeyError,ValueError,TypeError,OverflowError,AttributeError):raise NotReady('Event version/type is not eligible for bounded rescheduling') from None
        result={'id':identifier,'title':title,'start':a.isoformat(),'end':b.isoformat(),'all_day':all_day,
            'etag':etag,'private':deepcopy(private),'calendar':self.calendar_id}
        result['version']=digest(result);return result

    def get(self,identifier):
        if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,1024}',identifier):raise ValueError('saved_calendar_id_bound')
        try:value=self.client.request('GET',self.base+'/'+quote(identifier,safe=''))
        except ApiError as error:
            if error.status in {404,410}:raise NotFound('Saved event is missing') from None
            raise
        result=self.record(value)
        if result['id']!=identifier:raise NotReady('Calendar event identity changed')
        return result

    def records(self,start,end):
        if start.utcoffset() is None or end.utcoffset() is None or not timedelta(0)<end-start<=timedelta(days=7):raise ValueError('saved_calendar_read_window')
        rows={};page=None;seen=set();unsupported=0
        for _ in range(3):
            params={'timeMin':start.isoformat(),'timeMax':end.isoformat(),'singleEvents':'true','orderBy':'startTime','timeZone':'Asia/Bangkok','maxResults':20}
            if page:params['pageToken']=page
            value=self.client.request('GET',self.base,params=params);items=value.get('items',[] if value.get('kind')=='calendar#events' else None)
            if not isinstance(items,list) or len(items)>20:raise NotReady('Incomplete saved Calendar page')
            for event in items:
                if not isinstance(event,dict):raise NotReady('Invalid saved Calendar event')
                if event.get('status')=='cancelled':continue
                try:record=self.record(event)
                except NotReady:unsupported+=1;continue
                a=datetime.combine(date.fromisoformat(record['start']),time.min,BANGKOK) if record['all_day'] else datetime.fromisoformat(record['start'])
                b=datetime.combine(date.fromisoformat(record['end']),time.min,BANGKOK) if record['all_day'] else datetime.fromisoformat(record['end'])
                if not a<end or not start<b:continue
                key=record['id']
                if key in rows and rows[key]!=record:raise NotReady('Calendar changed across pages')
                rows[key]=record
            page=value.get('nextPageToken')
            if not page:return {'records':sorted(rows.values(),key=lambda r:(r['start'],r['id'])),'unsupported_count':unsupported,'complete':True}
            if not isinstance(page,str) or not 1<=len(page)<=1024 or page in seen:break
            seen.add(page)
        raise NotReady('Calendar edit selection is incomplete')

    def matches_after(self,value,payload,key):
        try:row=self.record(value)
        except NotReady:return False
        after=payload['after'];private=reschedule_body(payload,key)['extendedProperties']['private']
        return (row['id']==payload['id'] and all(row[k]==after[k] for k in ['title','start','end','all_day'])
            and row['private'].get('line_edit_action')==private['line_edit_action']
            and row['private'].get('line_edit_payload')==private['line_edit_payload'])

    def apply(self,payload,key,*,bind_request):
        if not self.writes_enabled:raise WriteBlocked('Calendar rescheduling is disabled')
        if payload.get('domain')!='calendar' or payload['before']['calendar']!=self.calendar_id:raise WriteBlocked('Bound saved Calendar action required')
        current=self.get(payload['id'])
        if current['version']!=payload['before']['version']:raise Conflict('Saved event changed after review')
        body=reschedule_body(payload,key);headers={'If-Match':current['etag']};params={'sendUpdates':'none'}
        bind_request(body,{'id':current['id'],'etag':current['etag'],'version':current['version']})
        self.effect_guard()
        try:value=self.client.request('PATCH',self.base+'/'+quote(payload['id'],safe=''),body=body,params=params,headers=headers)
        except ApiError as error:
            if error.status==412:raise Conflict('Calendar ETag precondition failed; fresh review required') from None
            if error.status in {404,410}:raise NotFound('Saved event is missing') from None
            if error.status in {401,403}:raise WriteBlocked('Existing Calendar edit permission is unavailable') from None
            raise
        if not self.matches_after(value,payload,key):raise OutcomeUnknown('Calendar update acknowledgement does not prove the confirmed change')
        return {'id':payload['id'],'before':{k:payload['before'][k] for k in ['title','start','end','all_day']},'after':deepcopy(payload['after']),'proof':'conditional_ack'}

    def reconcile(self,key,payload):
        try:value=self.client.request('GET',self.base+'/'+quote(payload['id'],safe=''))
        except Exception:return Reconciliation('unknown')
        if self.matches_after(value,payload,key):return Reconciliation('committed',{'id':payload['id'],
            'before':{k:payload['before'][k] for k in ['title','start','end','all_day']},'after':deepcopy(payload['after']),'proof':'positive_marker_and_state'})
        return Reconciliation('unknown')
