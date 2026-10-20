"""Owner-requested LINE onboarding for persistent read-only calendar sources."""
from copy import deepcopy
from datetime import datetime,time,timedelta
import re
from urllib.parse import urlparse,parse_qs,quote

from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from .calendar_provider import additional_calendar_ids,MAX_READ_CALENDARS
from .history_context import digest
from .language import BANGKOK
from .model_api import object_schema
from .state import Result
from .transport import ApiError,NotReady,OutcomeUnknown

ROLE={'reader','writer','owner','writerWithoutPrivateAccess'}
TTL=timedelta(hours=24)

class Connect(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    operation: Literal['start','add','cancel']
    calendar_id: str|None=Field(max_length=512)
    source_refs: list[str]=Field(min_length=1,max_length=4)

def definitions():
    return [{'type':'function','name':'connect_calendar','strict':True,
        'description':'Connect a shared calendar for appointment READS. For an owner request to add/connect another calendar, use start with calendar_id=null. The server supplies the bot sharing email and asks for the ID. Only after that delivered prompt, use add with the literal ID or ID decoded from the owner calendar.google.com embed link in the CURRENT message. Never use an event-creation tool to connect a calendar. A bare ID without pending onboarding must start onboarding first. Cancel only when the owner asks to cancel this setup.',
        'parameters':object_schema({'operation':{'type':'string','enum':['start','add','cancel']},
            'calendar_id':{'type':['string','null']},'source_refs':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':4}})}]

def seal(value):
    value['proof']=digest({k:v for k,v in value.items() if k!='proof'})

def validate(value,owner):
    if value is None:return
    required={'format','owner_id','calendar_id','calendars','pending','proof'}
    if (not isinstance(value,dict) or set(value)!=required or value['format']!='calendar-connections-v1'
        or value['owner_id']!=owner or not isinstance(value['calendar_id'],str) or not 1<=len(value['calendar_id'])<=512
        or value['proof']!=digest({k:v for k,v in value.items() if k!='proof'}) or not isinstance(value['calendars'],list)):
        raise ValueError('Invalid Calendar connection binding')
    additional_calendar_ids([row.get('id') if isinstance(row,dict) else None for row in value['calendars']],value['calendar_id'])
    def aware(text):
        at=datetime.fromisoformat(text)
        if at.utcoffset() is None:raise ValueError('Calendar connection timestamp requires offset')
        return at
    for row in value['calendars']:
        if (set(row)!={'id','connected_at','source_ref','access_role'} or row['access_role'] not in ROLE
            or not isinstance(row['source_ref'],str) or not re.fullmatch(r'msg_[a-f0-9]{20}',row['source_ref'])):
            raise ValueError('Invalid Calendar connection record')
        aware(row['connected_at'])
    pending=value['pending']
    if pending is not None:
        if (not isinstance(pending,dict) or set(pending)!={'ref','event','created_at','expires_at'}
            or not isinstance(pending['event'],str) or not pending['event']
            or pending['ref']!='cc_'+digest([owner,pending['event']])[:20]
            or aware(pending['expires_at'])-aware(pending['created_at'])!=TTL):
            raise ValueError('Invalid Calendar connection onboarding')

def connected_ids(state,owner,calendar_id):
    value=getattr(state,'calendar_connections',None)
    validate(value,owner)
    return tuple(row['id'] for row in value['calendars']) if value and value['calendar_id']==calendar_id else ()

def context(host):
    if not host.calendar_connections_enabled:return {}
    value=host.state.calendar_connections
    active=value if value and value['owner_id']==host.settings.owner_id and value['calendar_id']==host.calendar_flow.calendar_id else None
    return {'calendar_connection':{'awaiting_calendar_id':bool(active and active['pending']
        and datetime.fromisoformat(active['pending']['expires_at'])>datetime.fromisoformat(host._current['at'])),
        'connected_count':len(active['calendars']) if active else 0}}

def probe_params(at):
    start=datetime.combine(at.astimezone(BANGKOK).date(),time.min,BANGKOK)
    return {'timeMin':start.isoformat(),'timeMax':(start+timedelta(days=1)).isoformat(),
        'singleEvents':'true','orderBy':'startTime','timeZone':'Asia/Bangkok','maxResults':1,'fields':'kind,accessRole'}

def probe_allowed(http,method,path,body,params):
    probe=getattr(http.state,'calendar_connection_probe',None)
    return bool(getattr(http.config,'calendar_connections_enabled',False) and probe and method=='GET' and body is None
        and probe['owner_id']==http.config.owner_id and probe['calendar_id']!=http.config.calendar_id
        and path=='/calendar/v3/calendars/'+quote(probe['calendar_id'],safe='')+'/events' and params==probe['params'])

def literal_id(identifier,text):
    if identifier in re.findall(r'[A-Za-z0-9_.+#-]+@[A-Za-z0-9.-]+',text):return True
    for url in re.findall(r'https://calendar\.google\.com/[^\s<>]+',text):
        parsed=urlparse(url.rstrip(')'))
        if parsed.hostname=='calendar.google.com' and parsed.path=='/calendar/embed' and parse_qs(parsed.query).get('src')==[identifier]:return True
    return False

def claims_connected(text):
    return bool(re.search(r'(?:เชื่อม(?:ต่อ)?|เพิ่ม).*?ปฏิทิน.*?(?:แล้ว|สำเร็จ|เรียบร้อย)|\bcalendar\s+(?:(?:has\s+been|is|was)\s+)?(?:now\s+)?(?:connected|added)\b|\b(?:successfully|I\s+have|I\x27ve)\s+(?:connected|added)\b.*?\bcalendar\b',text,re.I))

def execute(host,args):
    output={'calendar_connection':True,'domain':'calendar','calendar_write_authorized':False,'financial_write_authorized':False}
    def failed(code):return {**output,'ok':False,'status':'connection_error','error_code':code}
    if not host.calendar_connections_enabled:return failed('disabled')
    try:
        parsed=Connect.model_validate(args)
        sources=host._owner_sources(parsed.source_refs)
        if host._current['source_ref'] not in parsed.source_refs:raise ValueError()
        validate(host.state.calendar_connections,host.settings.owner_id)
    except (ValueError,TypeError,KeyError):return failed('invalid_arguments')
    now=datetime.fromisoformat(host._current['at'])
    value=deepcopy(host.state.calendar_connections)
    if value is None:
        value={'format':'calendar-connections-v1','owner_id':host.settings.owner_id,'calendar_id':host.calendar_flow.calendar_id,
            'calendars':[],'pending':None}
    if value['calendar_id']!=host.calendar_flow.calendar_id:return failed('target_changed')
    if parsed.operation=='cancel':
        if parsed.calendar_id is not None:return failed('invalid_arguments')
        value['pending']=None;seal(value);host.state.calendar_connections=value
        return {**output,'ok':True,'status':'connection_cancelled'}
    if parsed.operation=='start':
        if parsed.calendar_id is not None:return failed('invalid_arguments')
        pending={'ref':'cc_'+digest([host.settings.owner_id,host._active_event_id])[:20],'event':host._active_event_id,
            'created_at':now.isoformat(),'expires_at':(now+TTL).isoformat()}
        value['pending']=pending;seal(value);host.state.calendar_connections=value
        return {**output,'ok':True,'status':'awaiting_calendar_id','onboarding_ref':pending['ref'],'sharing_email':host.calendar_sharing_email}
    try:
        if parsed.calendar_id!=host.calendar_flow.calendar_id:additional_calendar_ids([parsed.calendar_id],host.calendar_flow.calendar_id)
        if not any(row['source_ref']==host._current['source_ref'] and literal_id(parsed.calendar_id,row['text']) for row in sources):raise ValueError()
    except (ValueError,TypeError):return failed('invalid_calendar_id')
    pending=value['pending']
    if not pending or datetime.fromisoformat(pending['expires_at'])<=now:return failed('start_required')
    receipt=host.state.receipts.get(pending['event'])
    prompt=receipt.result.data.get('tool_result',{}) if receipt else {}
    if prompt.get('onboarding_ref')!=pending['ref']:return failed('start_required')
    if getattr(host,'production',False) and not any(row['role']=='assistant' and row['event']==pending['event'] and row.get('delivery_verified') for row in host.state.history):return failed('prompt_not_delivered')
    identifiers=set(host.calendar_flow.adapter.calendar_read_ids)|{host.calendar_flow.calendar_id}|{row['id'] for row in value['calendars']}
    if parsed.calendar_id in identifiers:
        value['pending']=None;seal(value);host.state.calendar_connections=value
        return {**output,'ok':True,'status':'already_connected','calendar_id':parsed.calendar_id}
    if len(identifiers)>=MAX_READ_CALENDARS:return failed('capacity')
    params=probe_params(now)
    host.state.calendar_connection_probe={'owner_id':host.settings.owner_id,'calendar_id':parsed.calendar_id,'params':params}
    try:role=host.calendar_flow.adapter.verify_read_access(parsed.calendar_id,params)
    except ApiError as error:return failed('access_required' if error.status in {401,403,404,410} else 'provider_failure')
    except (NotReady,OutcomeUnknown):return failed('verification_incomplete')
    except Exception:return failed('provider_failure')
    finally:host.state.calendar_connection_probe=None
    value['calendars'].append({'id':parsed.calendar_id,'connected_at':now.isoformat(),'source_ref':host._current['source_ref'],'access_role':role})
    value['pending']=None;seal(value);host.state.calendar_connections=value
    return {**output,'ok':True,'status':'calendar_connected','calendar_id':parsed.calendar_id}

ERRORS={'disabled':'ยังไม่ได้เปิดการเชื่อมปฏิทินผ่าน LINE ครับ',
    'invalid_arguments':'ยังเชื่อมปฏิทินไม่ได้ครับ กรุณาเริ่มเชื่อมปฏิทินใหม่',
    'invalid_calendar_id':'กรุณาส่ง Calendar ID ที่ถูกต้อง หรือส่งลิงก์ฝังปฏิทินจาก Google Calendar ครับ',
    'start_required':'กรุณาบอกว่า “เพิ่มปฏิทินใหม่” เพื่อเริ่มเชื่อมปฏิทินก่อนครับ',
    'prompt_not_delivered':'ยังยืนยันไม่ได้ว่าคำแนะนำการแชร์ส่งถึงคุณแล้ว กรุณาเริ่มเชื่อมปฏิทินใหม่ครับ',
    'access_required':'บอตยังอ่านปฏิทินนี้ไม่ได้ครับ กรุณาแชร์ให้บัญชีบอตด้วยสิทธิ์ “ดูรายละเอียดกิจกรรมทั้งหมด” แล้วส่ง Calendar ID อีกครั้ง',
    'provider_failure':'ตอนนี้ตรวจสอบปฏิทินไม่สำเร็จครับ ยังไม่ได้เพิ่มปฏิทิน กรุณาส่ง Calendar ID อีกครั้งภายหลัง',
    'verification_incomplete':'ยังตรวจสอบสิทธิ์อ่านปฏิทินไม่ผ่านครับ ยังไม่ได้เพิ่มปฏิทิน',
    'capacity':'เชื่อมปฏิทินครบจำนวนที่รองรับแล้วครับ ยังไม่ได้เพิ่มปฏิทินนี้',
    'target_changed':'การตั้งค่าปฏิทินหลักเปลี่ยนครับ ยังไม่ได้เพิ่มปฏิทิน'}

def result(output):
    status=output['status']
    if not output['ok']:message=ERRORS[output['error_code']]
    elif status=='awaiting_calendar_id':message='แชร์ปฏิทินให้ '+output['sharing_email']+' ด้วยสิทธิ์ “ดูรายละเอียดกิจกรรมทั้งหมด” แล้วส่ง Calendar ID หรือส่งลิงก์ฝังปฏิทินมาที่นี่ครับ'
    elif status=='connection_cancelled':message='ยกเลิกการเชื่อมปฏิทินครั้งนี้แล้วครับ'
    elif status=='already_connected':message='ปฏิทิน '+output['calendar_id']+' อยู่ในรายการที่อ่านอยู่แล้วครับ'
    else:message='เชื่อมปฏิทิน '+output['calendar_id']+' สำหรับอ่านนัดเรียบร้อยแล้วครับ ต่อไปเวลาถามนัดจะรวมปฏิทินนี้ด้วย'
    return Result(status,message,{'domain':'calendar','calendar_write_authorized':False,'financial_write_authorized':False,'tool_result':deepcopy(output)})
