"""Owner-grounded Calendar drafts and domain-bound server confirmation.

Model tools prepare/read/cancel drafts only. Provider creation goes through the
existing durable action/lease protocol after a delivered unchanged review.
"""
from copy import deepcopy
from datetime import date,datetime,timedelta
import hmac,re,secrets
from pydantic import BaseModel,ConfigDict,Field
from .calendar_dates import event_date,event_time,duration,bangkok_zone
from .contracts import CreateEvent
from .calendar_event_types import AllDayEvent,ALL_DAY,interval,parse_payload,date_label,range_requested,clarification_fields,timing_question
from .tool_errors import PlanningError,recovery
from .history_context import digest,WINDOW
from .history_dates import MONTH
from .language import BANGKOK,intent_guard,DIGITS
from .model_api import object_schema
from .state import Result,LastAction

FIELDS=('title','date_text','time_text','end_date_text','end_time_text','duration_text')
NAMES={'title':'ชื่อเรื่อง','date':'วันนัด','time':'เวลาเริ่ม','end':'เวลาสิ้นสุดหรือระยะเวลา (นาที)','time_or_all_day':'ทั้งวันหรือเวลาเริ่ม',
       'calendar_date_unclear':'วันนัดที่ชัดเจน','calendar_date_invalid':'วันที่มีอยู่จริง','calendar_weekday_conflict':'วันในสัปดาห์กับวันที่ให้ตรงกัน',
       'calendar_time_unclear':'เวลาเริ่มหรือสิ้นสุดที่ชัดเจน','calendar_time_invalid':'เวลาที่ถูกต้อง',
       'calendar_duration_unclear':'ระยะเวลาเป็นนาทีหรือชั่วโมง','calendar_end_conflict':'เวลาสิ้นสุดและระยะเวลาให้ตรงกัน',
       'calendar_end_order':'วันและเวลาสิ้นสุดที่อยู่หลังเวลาเริ่ม','calendar_access_unavailable':'การเชื่อมต่อปฏิทิน'}
NAMES.update(calendar_event_type_conflict='เลือกนัดทั้งวันหรือนัดระบุเวลาอย่างใดอย่างหนึ่ง',
    calendar_all_day_range_unsupported='เลือกวันเดียวต่อหนึ่งนัดทั้งวัน หรือแยกช่วงเป็นแต่ละนัด')

class Arguments(BaseModel):model_config=ConfigDict(extra='forbid',strict=True)
class TemporalBinding(Arguments):
    field: str
    temporal_ref: str
class Edit(Arguments):
    draft_ref:str|None=None
    draft_version:int|None=Field(default=None,ge=1)
    title:str|None=Field(default=None,min_length=1,max_length=200)
    date_text:str|None=Field(default=None,min_length=1,max_length=100)
    time_text:str|None=Field(default=None,min_length=1,max_length=100)
    end_date_text:str|None=Field(default=None,min_length=1,max_length=100)
    end_time_text:str|None=Field(default=None,min_length=1,max_length=100)
    duration_text:str|None=Field(default=None,min_length=1,max_length=100)
    all_day:bool|None=None
    source_refs:list[str]=Field(min_length=1,max_length=16)
    temporal_refs:list[TemporalBinding]=Field(default_factory=list,max_length=5)
class Control(Arguments):
    draft_ref:str
    draft_version:int=Field(ge=1)
    operation:str
class Read(Arguments):draft_ref:str|None=None

def fingerprint(d):return digest(d)
def unsupported_features(text):
    return [key for pattern,key in [
        (r'ทุก(?:วัน|สัปดาห์|เดือน|จันทร์|อังคาร|พุธ|พฤหัส|ศุกร์|เสาร์|อาทิตย์)|\b(?:weekly|daily|recurr|every\s+(?:day|week|month))','recurrence'),
        (r'ส่งคำเชิญ|เชิญ|ผู้เข้าร่วม|\b(?:invite|attendee)','attendees'),
        (r'แจ้งเตือน|เตือน(?:ก่อน|ล่วงหน้า|ด้วย|\s*\d)','reminders')] if re.search(pattern,text,re.I)]
def payload(d):
    if d.get('all_day'):return AllDayEvent(title=d['facts']['title'],start=d['start'],end=d['end']).model_dump(mode='json')
    return CreateEvent(title=d['facts']['title'],start=datetime.fromisoformat(d['start']),end=datetime.fromisoformat(d['end']),reminder_minutes=None).model_dump(mode='json')
def payload_hash(d):return digest([d['owner_id'],d['calendar_id'],payload(d)])

def outside_title(text,title):
    return text.replace(title,' '*len(title)) if title else text

def literal_fact(value,text,field):
    """Certify the original owner span; accept spacing/digit script, not new facts."""
    if field=='title':return value if value in text else None
    compact=''.join(value.translate(DIGITS).split())
    if not compact:return None
    labeled_duration=field=='duration_text' and bool(re.match(r'^(?:ใช้เวลา|กินเวลา|(?:กำหนด|มี)?ระยะเวลา(?:ไว้)?|เวลา|นาน)',compact))
    pieces=[f'[{c}{chr(ord("๐")+int(c))}]' if c.isascii() and c.isdigit() else re.escape(c) for c in compact]
    for match in re.finditer(r'\s*'.join(pieces),text):
        original=match[0];before=text[:match.start()];after=text[match.end():]
        if re.search(r'(?<=\d)\s+(?=\d)',original):continue
        if before and before[-1].isdigit() and not labeled_duration and (compact[0].isdigit() or field in {'duration_text','time_text','end_time_text'}):continue
        if after and after[0].isdigit() and (compact[-1].isdigit() or field in {'duration_text','time_text','end_time_text'}):continue
        if field in {'date_text','end_date_text'} and re.match(r'\s*(?:'+MONTH+r'|เดือน|ปี|พ\.?ศ\.?|ค\.?ศ\.?|\d{4}(?!\d))',after,re.I):continue
        if field in {'time_text','end_time_text'} and re.match(r'\s*(?:am|pm|โมง|ครึ่ง|เช้า|เย็น|ทุ่ม|นาฬิกา|[:.]\d|น\.?\s*ครึ่ง)',after,re.I):continue
        if field=='duration_text' and (not labeled_duration and re.search(r'(?:ยี่|สิบ|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า)$',before) or after.startswith('ครึ่ง')):continue
        return original
    if field in {'time_text','end_time_text'}:
        # Thai น. is a clock-unit suffix, not an AM/PM or zone assertion.
        # A model may repeat the range's trailing unit on its first clock;
        # certify and retain the actual original clock span instead.
        clock=re.fullmatch(r'([0-9๐-๙]{1,2}[:.][0-9๐-๙]{2})\s*น\.?',value.strip())
        if clock:return literal_fact(clock[1],text,field)
    return None

def literal_guard_text(text,title):
    """Quoted literal names are data; a quoted whole instruction is not consent."""
    spans=[m.span() for m in re.finditer(re.escape(title),text)] if title else []
    clean=list(text)
    for quote in re.finditer(r'["“]([^"“”]+)["”]',text):
        contained=any(a<=quote.start() and quote.end()<=b for a,b in spans)
        named=quote[1]==title and re.search(r'เพิ่มนัด|สร้างนัด|นัด|จอง',text[:quote.start()])
        if contained or named:clean[quote.start():quote.end()]=' '*(quote.end()-quote.start())
    return ''.join(clean)

def calculate(d):
    facts=d['facts'];missing=[];issues=[];start=None;end=None
    if not facts['title']:missing.append('title')
    def at(field):return datetime.fromisoformat(d['anchors'][field]['at'])
    def parse(field,fn,missing_name=None):
        if not facts[field]:
            if missing_name:missing.append(missing_name)
            return None
        if d['anchors'][field].get('temporal'):
            from .temporal import validate_proof,projection
            proof=d['anchors'][field]['temporal'];validate_proof(proof)
            if proof['owner']!=d['owner_id'] or proof['values'].get(projection(field))!=facts[field]:raise PlanningError('calendar_temporal_proof_invalid')
        try:return fn(facts[field])
        except ValueError as error:issues.append(str(error));return None
    day=parse('date_text',lambda text:event_date(text,at('date_text')),'date')
    if d.get('all_day'):
        if d.get('all_day_range_source'):issues.append('calendar_all_day_range_unsupported')
        if d.get('all_day_clock_source'):issues.append('calendar_event_type_conflict')
        if any(facts[k] for k in ['time_text','end_time_text','duration_text']):issues.append('calendar_event_type_conflict')
        end_day=parse('end_date_text',lambda text:event_date(text,at('end_date_text')))
        if end_day and day and end_day!=day:issues.append('calendar_all_day_range_unsupported')
        try:end=day+timedelta(days=1) if day else None
        except OverflowError:issues.append('calendar_date_invalid')
        return day.isoformat() if day else None,end.isoformat() if end else None,missing,issues
    start_time=parse('time_text',lambda text:event_time(text,allow_bare=d['anchors']['time_text'].get('bare',False)),'time')
    minutes=parse('duration_text',lambda text:duration(text,allow_bare=d['anchors']['duration_text'].get('bare',False)))
    end_time=parse('end_time_text',event_time)
    end_day=parse('end_date_text',lambda text:event_date(text,at('end_date_text')))
    if not facts['duration_text'] and not facts['end_time_text']:missing.append('end')
    if facts['end_date_text'] and not facts['end_time_text']:issues.append('calendar_end_order')
    if day and start_time:
        start=datetime.combine(day,start_time,BANGKOK)
        if minutes:end=start+timedelta(minutes=minutes)
        if end_time:
            explicit=datetime.combine(end_day or day,end_time,BANGKOK)
            if end and end!=explicit:issues.append('calendar_end_conflict')
            end=explicit
        if end and (end<=start or end-start>timedelta(days=7)):issues.append('calendar_end_order')
        for field,actual in [('time_text',start),('end_time_text',end)]:
            proof=d['anchors'].get(field,{}).get('temporal')
            if proof and proof['values'].get('datetime') and actual and actual!=datetime.fromisoformat(proof['values']['datetime']):
                issues.append('calendar_temporal_date_pair_required')
    return start.isoformat() if start else None,end.isoformat() if end else None,list(dict.fromkeys(missing)),list(dict.fromkeys(issues))

def restore_calendar(state,value):
    if set(value)!={'format','drafts','proofs'} or value['format'] not in {'calendar-history-v1','calendar-history-v2'}:raise PlanningError('Invalid Calendar history envelope')
    drafts=value['drafts'];proofs=value['proofs']
    if not isinstance(drafts,dict) or not isinstance(proofs,dict) or set(drafts)!=set(proofs) or len(drafts)>64:raise PlanningError('Invalid Calendar history records')
    if value['format']=='calendar-history-v1' and any('all_day' in d for d in drafts.values()):raise PlanningError('Date-mode state requires Calendar history v2')
    for ref,d in drafts.items():
        if (not isinstance(d,dict) or not re.fullmatch(r'cal_[a-f0-9]{20}',ref) or d.get('draft_ref')!=ref or d.get('owner_id')!=state.owner_id
            or type(d.get('version')) is not int or d['version']<1 or d.get('status') not in {'needs_information','review','committing','recorded','cancelled'}
            or not isinstance(d.get('calendar_id'),str) or not 1<=len(d['calendar_id'])<=512 or d['calendar_id']=='primary'
            or not isinstance(d.get('calendar_label'),str) or not 1<=len(d['calendar_label'])<=100
            or proofs[ref]!={'owner_id':state.owner_id,'version':d['version'],'fingerprint':fingerprint(d)}):raise PlanningError('Invalid Calendar draft proof/binding')
        for field in ['created_at','updated_at','expires_at']:
            if datetime.fromisoformat(d[field]).utcoffset() is None:raise PlanningError('Calendar clock must be aware')
        if type(d.get('all_day',False)) is not bool:raise PlanningError('Invalid Calendar date mode')
        if 'all_day' in d:
            anchor=d.get('all_day_anchor',{})
            if (anchor.get('value') is not d['all_day'] or anchor.get('source_ref') not in d['source_refs']
                or not isinstance(anchor.get('literal'),str) or not 1<=len(anchor['literal'])<=100
                or datetime.fromisoformat(anchor['at']).utcoffset() is None):raise PlanningError('Invalid Calendar date-mode proof')
        for key in ['all_day_range_source','all_day_clock_source']:
            if key in d:
                anchor=d[key]
                if not d.get('all_day') or anchor['source_ref'] not in d['source_refs'] or datetime.fromisoformat(anchor['at']).utcoffset() is None:raise PlanningError('Invalid all-day temporal source')
        if (set(d['facts'])!=set(FIELDS) or not isinstance(d['source_refs'],list) or len(d['source_refs'])>32
            or any(not isinstance(v,str) for v in d['source_refs'])):raise PlanningError('Invalid Calendar facts')
        for field,text in d['facts'].items():
            if text is None:continue
            if not isinstance(text,str) or not 1<=len(text)<=200:raise PlanningError('Invalid Calendar literal fact')
            a=d['anchors'].get(field,{})
            if (a.get('source_ref') not in d['source_refs'] or d['fact_sources'].get(field)!=[a['source_ref']]
                or datetime.fromisoformat(a['at']).utcoffset() is None or type(a.get('bare')) is not bool):raise PlanningError('Invalid Calendar fact provenance')
        computed=calculate(d)
        if (d['start'],d['end'],d['missing_fields'])!=computed[:3] or any(v not in d['issues'] for v in computed[3]):raise PlanningError('Invalid Calendar normalized values')
        if d['review']:
            r=d['review']
            if (d['status'] not in {'review','committing'} or d['missing_fields'] or d['issues'] or r['payload_hash']!=payload_hash(d)
                or not re.fullmatch(r'[a-f0-9]{16}',r['nonce']) or datetime.fromisoformat(r['at']).utcoffset() is None):raise PlanningError('Invalid Calendar review')
    state.calendar_drafts=deepcopy(drafts);state.calendar_proofs=deepcopy(proofs)

class CalendarWorkflow:
    tool_names={'prepare_calendar_event','read_calendar_drafts','control_calendar_draft'}
    def __init__(self,host,*,enabled=False,adapter=None,calendar_id=None,label='LINE Assistant'):
        self.h=host;self.enabled=enabled;self.adapter=adapter;self.calendar_id=calendar_id;self.label=label;self.request_views={}
        from .calendar_batches import CalendarBatches
        self.batches=CalendarBatches(self);self.tool_names=self.tool_names|self.batches.names
        if enabled:
            if not host.raw_history_enabled or not host.semantic_enabled:raise PlanningError('Calendar requires explicit raw history/semantic mode')
            if not adapter or not calendar_id or calendar_id=='primary' or getattr(adapter,'calendar_id',None)!=calendar_id:raise PlanningError('Explicit bound Calendar adapter required')
            if not getattr(host,'production',False) and not getattr(adapter,'mocked',False):
                # Production factory supplies the existing fenced production binding.
                from .production_history_state import ProductionHistoryState
                if type(host.state) is not ProductionHistoryState:raise PlanningError('Local Calendar requires a fake provider')
            if not isinstance(label,str) or not 1<=len(label)<=100:raise PlanningError('Bounded Calendar label required')
    def seal(self,d):self.h.state.calendar_proofs[d['draft_ref']]={'owner_id':self.h.settings.owner_id,'version':d['version'],'fingerprint':fingerprint(d)}
    def draft(self,ref,*,active=True):
        d=self.h.state.calendar_drafts.get(ref);proof=self.h.state.calendar_proofs.get(ref)
        if (not d or d['owner_id']!=self.h.settings.owner_id or d['calendar_id']!=self.calendar_id or getattr(self.adapter,'calendar_id',None)!=self.calendar_id or proof!={'owner_id':d['owner_id'],'version':d['version'],'fingerprint':fingerprint(d)}
            or datetime.fromisoformat(d['expires_at'])<=self.h.clock() or active and d['status'] in {'recorded','cancelled'}):raise PlanningError('calendar_draft_stale_or_unavailable')
        return d
    def definitions(self):return self.single_definitions()+self.batches.definitions()
    def views(self):
        result=[];self.request_views={};focused=self.batches.focused_edit_ref()
        for ref,d in self.h.state.calendar_drafts.items():
            if focused and d.get('batch_ref')!=focused:continue
            if d['status'] in {'recorded','cancelled'}:continue
            try:self.draft(ref)
            except ValueError:continue
            self.request_views[ref]=fingerprint(d)
            result.append({**{k:deepcopy(d[k]) for k in ['draft_ref','version','status','facts','start','end','missing_fields','issues','fact_sources']},'all_day':d.get('all_day',False)})
        return result
    def single_definitions(self):
        nullable={'type':['string','null']}
        fields={name:{**nullable,'description':'Literal full owner event name, excluding leading request verbs unless explicitly quoted/named as title' if name=='title' else 'Literal owner date/time/duration wording; null retains the certified value'} for name in FIELDS}
        fields['date_text']['description']='Copy only the literal date span, excluding clock/duration/all-day mode words even when adjacent. วันที่20ตุลาทั้งวัน -> วันที่20ตุลา; all_day=true separately.'
        temporal_refs={'type':'array','maxItems':5,'items':object_schema({'field':{'type':'string','enum':[f for f in FIELDS if f!='title']},'temporal_ref':{'type':'string'}})}
        return [{'type':'function','name':'prepare_calendar_event','strict':True,
            'description':'Prepare one appointment, never writes. all_day=true only for explicit owner all-day wording; requires a date, never times. Timed appointments need start and end/duration. Null retains existing mode/facts. One all-day date per event; clarify or split multi-day ranges. Use owned source refs/current version. No guests, recurrence or reminders.',
            'parameters':object_schema({'draft_ref':nullable,'draft_version':{'type':['integer','null']},**fields,'all_day':{'type':['boolean','null']},'temporal_refs':temporal_refs,'source_refs':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':16}})},
            {'type':'function','name':'read_calendar_drafts','strict':True,'description':'Read owned pending Calendar facts without creating a review or consent.','parameters':object_schema({'draft_ref':nullable})},
            {'type':'function','name':'control_calendar_draft','strict':True,'description':'Only explicitly requested review or pending-draft cancellation. Never confirms a write or deletes an event.','parameters':object_schema({'draft_ref':{'type':'string'},'draft_version':{'type':'integer'},'operation':{'type':'string','enum':['review','cancel']}})}]
    def bound(self,p,*,allow_batch=False):
        d=self.draft(p.draft_ref)
        if d.get('batch_ref') and not allow_batch:raise PlanningError('calendar_batch_required')
        if p.draft_version!=d['version'] or self.request_views.get(p.draft_ref)!=fingerprint(d):raise PlanningError('calendar_stale_draft_version')
        if datetime.fromisoformat(self.h._current['at'])<=datetime.fromisoformat(d['updated_at']):raise PlanningError('calendar_older_update')
        return d
    def _answer(self,d,field,value):
        _,shown=self.h._presented_result();view=shown.data.get('calendar_view',{}) if shown else {}
        return bool(view.get('draft_ref')==d['draft_ref'] and view.get('version')==d['version'] and field in view.get('missing_fields',[]) and self.h._current['text'].strip()==value.strip())
    def prepare(self,args,*,new_draft=None,commit=True,scope=None,allow_batch=False,guard_titles=(),field_evidence=None):
        p=Edit.model_validate(args)
        if p.temporal_refs:
            from .temporal import calendar_evidence
            if len({v.field for v in p.temporal_refs})!=len(p.temporal_refs):raise PlanningError('temporal_duplicate_field')
            resolved=calendar_evidence(self.h,{v.field:v.temporal_ref for v in p.temporal_refs},p.source_refs)
            for field,row in resolved.items():
                if getattr(p,field) not in {None,row['value'],row['text']}:raise PlanningError('temporal_value_mismatch',fields=[field])
            p=p.model_copy(update={k:v['value'] for k,v in resolved.items()})
            field_evidence={**(field_evidence or {}),**resolved}
        # Models occasionally put an explicit end clock or clock range in
        # the duration slot. Retain the literal end-role prefix so ordinary
        # owner-span checks cannot accept a model-invented "ถึง" role.
        if p.time_text is None and p.end_time_text is None and p.duration_text:
            from .calendar_sources import CLOCK
            clocks=list(CLOCK.finditer(p.duration_text))
            if (len(clocks)==1 and re.fullmatch(r'\s*(?:จนถึง|ถึง)\s*',p.duration_text[:clocks[0].start()])
                and not p.duration_text[clocks[0].end():].strip()):
                p=p.model_copy(update={'end_time_text':p.duration_text,'duration_text':None})
            elif (len(clocks)==2 and not p.duration_text[:clocks[0].start()].strip()
                and not p.duration_text[clocks[-1].end():].strip()
                and re.fullmatch(r'\s*(?:[-–—]|ถึง|จนถึง|to|until)\s*',p.duration_text[clocks[0].end():clocks[1].start()],re.I)):
                p=p.model_copy(update={'time_text':p.duration_text,'duration_text':None})
        # A literal complete range is two owner-supplied clocks, never an
        # inferred duration. Preserve both exact source spans for validation.
        if p.time_text and p.end_time_text is None and p.duration_text is None:
            from .calendar_sources import CLOCK
            clocks=list(CLOCK.finditer(p.time_text))
            if len(clocks)==2 and re.fullmatch(r'\s*(?:เวลา|ช่วง|ตั้งแต่|เริ่ม(?:เวลา|ตอน)?)?\s*',p.time_text[:clocks[0].start()]) and re.fullmatch(r'\s*',p.time_text[clocks[-1].end():]) and re.fullmatch(r'\s*(?:[-–—]|ถึง|จนถึง|to|until)\s*',p.time_text[clocks[0].end():clocks[1].start()],re.I):
                p=p.model_copy(update={'time_text':clocks[0][0],'end_time_text':clocks[1][0]})
        title=p.title or (new_draft or self.h.state.calendar_drafts.get(p.draft_ref,{}) or {}).get('facts',{}).get('title')
        guard=literal_guard_text(self.h._current['text'],title)
        for other in guard_titles:guard=literal_guard_text(guard,other)
        if not self.h.raw_history_enabled and (intent_guard(guard) or re.search(r'(?:อย่า|ไม่ต้อง|ยังไม่(?:ต้อง)?|ห้าม|ไม่ให้)(?:\s|เพิ่ง)*(?:สร้าง|นัด|จอง|เพิ่ม)',guard)):raise PlanningError('calendar_owner_no_write')
        old=deepcopy(new_draft) if new_draft is not None else deepcopy(self.bound(p,allow_batch=allow_batch)) if p.draft_ref else None
        if not p.draft_ref and p.draft_version is not None:raise PlanningError('calendar_unbound_version')
        if old and old['status']=='committing':raise PlanningError('calendar_action_pending')
        now=self.h._current['at']
        if old and old['review'] and commit:
            revoked=deepcopy(old);revoked.update(version=old['version']+1,updated_at=now,status='needs_information',review=None)
            self.h.state.calendar_drafts[old['draft_ref']]=revoked;self.seal(revoked);self.h.state.checkpoint()
        rows=self.h._owner_sources(p.source_refs)
        if field_evidence is not None:
            # A shared patch already checked each original owner span. Keep
            # its own timestamp and field binding through domain validation.
            original_scope=scope
            scope=lambda row,field,value,title:field_evidence[field] if field in field_evidence else original_scope(row,field,value,title) if original_scope else row
        if not old:
            if sum(d['status'] not in {'recorded','cancelled'} for d in self.h.state.calendar_drafts.values())>=8:raise PlanningError('calendar_too_many_drafts')
            ref='cal_'+digest([self.h.settings.owner_id,self.calendar_id,self.h._active_event_id])[:20]
            d={'draft_ref':ref,'owner_id':self.h.settings.owner_id,'calendar_id':self.calendar_id,'calendar_label':self.label,'version':1,
               'status':'needs_information','facts':dict.fromkeys(FIELDS),'fact_sources':{},'anchors':{},'source_refs':[],
               'created_at':now,'updated_at':now,'expires_at':(self.h.clock()+timedelta(seconds=self.h.settings.pending_ttl_seconds)).isoformat(),
               'start':None,'end':None,'missing_fields':[],'issues':[],'review':None}
        else:d=deepcopy(old);d.update(version=old['version']+(0 if new_draft is not None and p.draft_ref not in self.h.state.calendar_drafts else 1),updated_at=now,review=None)
        # Explicit literal all-day wording is authoritative even if the model
        # omits the optional mode field. Scope it to this source unit first.
        explicit_day=False;explicit_clock_source=None
        for row in rows:
            if field_evidence is not None and 'all_day' not in field_evidence:continue
            if field_evidence is None and old and old['facts']['title'] and row['source_ref']!=self.h._current['source_ref']:continue
            clean=outside_title(row['text'],p.title or d['facts']['title'])
            for match in ALL_DAY.finditer(clean):
                if re.search(r'(?:ไม่|อย่า|ยกเลิก).{0,12}$',clean[:match.start()]):continue
                try:scoped=scope(row,'all_day',match[0],p.title or d['facts']['title']) if scope else row
                except ValueError:continue
                if match[0] in outside_title(scoped['text'],p.title or d['facts']['title']):
                    explicit_day=True
                    from .calendar_sources import CLOCK
                    if CLOCK.search(outside_title(scoped['text'],p.title or d['facts']['title'])):explicit_clock_source={'source_ref':row['source_ref'],'at':row['at']}
        if explicit_day:
            if p.all_day is False:raise PlanningError('calendar_event_type_conflict')
            p=p.model_copy(update={'all_day':True})
        changed=False
        if p.all_day is not None and (p.all_day!=d.get('all_day',False) or p.all_day and explicit_day and (d.get('all_day_clock_source') or any(d['facts'][k] for k in ['time_text','end_time_text','duration_text']))):
            mode_sources=[]
            for row in rows:
                if field_evidence is None and old and old['facts']['title'] and row['source_ref']!=self.h._current['source_ref']:continue
                clean=outside_title(row['text'],p.title or d['facts']['title'])
                if p.all_day:
                    matches=[m[0] for m in ALL_DAY.finditer(clean) if not re.search(r'(?:ไม่|อย่า|ยกเลิก).{0,12}$',clean[:m.start()])]
                else:
                    matches=[v for v in [p.time_text,p.end_time_text,p.duration_text] if v and literal_fact(v,clean,'time_text') is not None]
                    matches+=[m[0] for m in re.finditer(r'ระบุเวลา|เป็นเวลา|มีเวลา',clean)]
                for literal in matches:
                    try:scoped=scope(row,'all_day',literal,p.title or d['facts']['title']) if scope else row
                    except ValueError:continue
                    if literal in outside_title(scoped['text'],p.title or d['facts']['title']):mode_sources.append((row,literal))
            if not mode_sources:raise PlanningError('calendar_all_day_source')
            row,literal=mode_sources[-1];d['all_day']=p.all_day
            d['all_day_anchor']={'value':p.all_day,'source_ref':row['source_ref'],'at':row['at'],'literal':literal}
            # A new event type cannot inherit incompatible old clock/range
            # facts. Supplied new facts are certified normally below.
            for field in ['time_text','end_time_text','duration_text','end_date_text']:
                d['facts'][field]=None;d['fact_sources'].pop(field,None);d['anchors'].pop(field,None)
            changed=True
        if not d.get('all_day'):
            d.pop('all_day_range_source',None);d.pop('all_day_clock_source',None)
        elif explicit_day:
            if explicit_clock_source:
                if d.get('all_day_clock_source')!=explicit_clock_source:changed=True
                d['all_day_clock_source']=explicit_clock_source
            elif d.pop('all_day_clock_source',None) is not None:changed=True
        if d.get('all_day'):
            range_source=None
            for row in rows:
                try:scoped=scope(row,'date_text',p.date_text or d['facts']['date_text'] or '',p.title or d['facts']['title']) if scope else row
                except ValueError:continue
                if range_requested(outside_title(scoped['text'],p.title or d['facts']['title'])):range_source={'source_ref':row['source_ref'],'at':row['at']}
            if range_source:d['all_day_range_source']=range_source;changed=True
            elif p.date_text is not None and self.h._current['source_ref'] in p.source_refs:
                if d.pop('all_day_range_source',None) is not None:changed=True
        if d.get('all_day') and p.end_date_text is None and re.search(r'วันเดียว|เฉพาะวัน',self.h._current['text']) and (d['facts']['end_date_text'] or d.get('all_day_range_source')):
            d['facts']['end_date_text']=None;d['fact_sources'].pop('end_date_text',None);d['anchors'].pop('end_date_text',None);d.pop('all_day_range_source',None);changed=True
        for field in FIELDS:
            value=getattr(p,field)
            if value is None:continue
            if old and value==d['facts'][field]:
                reanchor=False
                anchor=field_evidence.get(field) if field_evidence is not None else self.h._current
                if field in {'date_text','end_date_text'} and anchor and literal_fact(value,outside_title(anchor['text'],p.title or d['facts']['title']),field):
                    try:reanchor=event_date(value,datetime.fromisoformat(d['anchors'][field]['at']))!=event_date(value,datetime.fromisoformat(anchor['at']))
                    except ValueError:pass
                if not reanchor:continue
            eligible=[field_evidence[field]] if field_evidence is not None and field in field_evidence else [r for r in rows if not old or r['source_ref']==self.h._current['source_ref'] or allow_batch and old['facts'][field] is None]
            scoped=[scope(r,field,value,p.title or d['facts']['title']) if scope else r for r in eligible]
            matches=[(r,r['value'] if r.get('temporal') and r['value']==value else literal_fact(value,r['text'] if field=='title' else outside_title(r['text'],p.title or d['facts']['title']),field)) for r in scoped]
            matches=[(r,literal) for r,literal in matches if literal is not None]
            if not matches:raise PlanningError('calendar_'+field+'_source')
            row,literal=matches[-1];bare=bool(old and self._answer(old,'time' if field=='time_text' else 'end',literal))
            d['facts'][field]=literal;d['fact_sources'][field]=[row['source_ref']]
            d['anchors'][field]={'source_ref':row['source_ref'],'at':row['at'],'bare':bare,
                **({'temporal':deepcopy(row['temporal'])} if row.get('temporal') else {})};changed=True
        if old and not changed:
            if not commit and allow_batch:return deepcopy(old)
            raise PlanningError('calendar_no_fact_change')
        if p.duration_text is not None and p.end_time_text is None:
            for f in ['end_date_text','end_time_text']:d['facts'][f]=None;d['fact_sources'].pop(f,None);d['anchors'].pop(f,None)
        if p.end_time_text is not None and p.duration_text is None:
            d['facts']['duration_text']=None;d['fact_sources'].pop('duration_text',None);d['anchors'].pop('duration_text',None)
        relevant={ref for refs in d['fact_sources'].values() for ref in refs}
        guard_rows=[r for r in rows if r['source_ref'] in relevant and r['source_ref']!=self.h._current['source_ref']]+[self.h._current]
        for row in guard_rows:
            clean=literal_guard_text(row['text'],d['facts']['title'])
            for other in guard_titles:clean=literal_guard_text(clean,other)
            bangkok_zone(clean)
            if re.search(r'หรือ|ประมาณ|ราว\s*ๆ?|คร่าว\s*ๆ|ยังไม่แน่ใจ|อาจจะ',clean):raise PlanningError('calendar_ambiguous_source')
            if re.search(r'ทุก(?:วัน|สัปดาห์|เดือน|จันทร์|อังคาร|พุธ|พฤหัส|ศุกร์|เสาร์|อาทิตย์)|\b(?:weekly|daily|recurr|invite|attendee|every\s+(?:day|week|month))|ส่งคำเชิญ|เชิญ|ผู้เข้าร่วม|แจ้งเตือน|เตือน(?:ก่อน|ล่วงหน้า|ด้วย|\s*\d)',clean,re.I):
                error=PlanningError('calendar_timed_single_creation_only',kind='unsupported');error.unsupported_features=unsupported_features(clean);raise error
        d['source_refs']=list(dict.fromkeys([*(old['source_refs'] if old else []),*p.source_refs,*(r for refs in d['fact_sources'].values() for r in refs),*([d['all_day_anchor']['source_ref']] if 'all_day_anchor' in d else [])]))
        if len(d['source_refs'])>32:raise PlanningError('calendar_source_limit')
        d['start'],d['end'],d['missing_fields'],d['issues']=calculate(d)
        if 'calendar_temporal_date_pair_required' in d['issues']:
            raise PlanningError('calendar_temporal_date_pair_required',fields=['date_text','end_date_text'],required=['date projection paired with the offset time'])
        return self.save_review(d) if commit else d
    def review_draft(self,d):
        d['review']=None;d['status']='needs_information'
        if not d['missing_fields'] and not d['issues']:
            try:conflicts=self.adapter.conflicts(*interval(d))
            except Exception:d['issues']=['calendar_access_unavailable']
            else:
                d['status']='review';d['review']={'nonce':secrets.token_hex(8),'payload_hash':payload_hash(d),'at':self.h._current['at'],
                    'event':self.h._active_event_id,'conflicts':deepcopy(conflicts),'conflicts_hash':digest(conflicts)}
        return d
    def save_review(self,d):
        self.review_draft(d)
        self.h.state.calendar_drafts[d['draft_ref']]=d;self.seal(d);self.h.state.checkpoint()
        return self.output(d)
    def output(self,d):
        return {'domain':'calendar','ok':True,'status':'review' if d['status']=='review' else 'needs_information','draft_ref':d['draft_ref'],
            'draft_version':d['version'],'missing_fields':deepcopy(d['missing_fields']),'issues':deepcopy(d['issues']),
            'calendar_view':{**{k:deepcopy(d[k]) for k in ['draft_ref','version','facts','start','end','missing_fields','issues']},'all_day':d.get('all_day',False)}}
    def tool(self,name,args,*,interpreted_patch=False,field_evidence=None):
        try:
            if not self.enabled:raise PlanningError('calendar_disabled')
            if name in self.batches.names:return self.batches.tool(name,args,interpreted_patch=interpreted_patch,field_evidence=field_evidence)
            if name=='prepare_calendar_event':
                from .calendar_sources import units
                p=Edit.model_validate(args);d=self.h.state.calendar_drafts.get(p.draft_ref,{})
                focused=self.batches.focused_edit_ref()
                if focused and d.get('batch_ref')!=focused:
                    return self.batches.prepare({'batch_ref':None,'batch_version':None,'events':[args]})
                if d.get('batch_ref'):
                    b=self.batches.get(d['batch_ref'])
                    return self.batches.prepare({'batch_ref':b['batch_ref'],'batch_version':b['version'],'events':[args]},interpreted_patch=interpreted_patch,field_evidence=field_evidence)
                if not p.draft_ref and len(units(self.h._current,[p.title] if p.title else []))>1:
                    return self.batches.prepare({'batch_ref':None,'batch_version':None,'events':[args]})
            if name=='prepare_calendar_event':return self.prepare(args,field_evidence=field_evidence.get(args['draft_ref']) if field_evidence is not None else None)
            if name=='read_calendar_drafts':
                p=Read.model_validate(args);views=self.views()
                if p.draft_ref:views=[v for v in views if v['draft_ref']==p.draft_ref]
                return {'domain':'calendar','ok':True,'status':'draft_read','drafts':views}
            p=Control.model_validate(args);d=deepcopy(self.bound(p))
            if p.operation=='cancel':
                if not self.cancel_intent(self.h._current['text']):raise PlanningError('calendar_cancel_not_requested')
                if (self.h.state.pending or any(v['status']!='recorded' for v in self.h.state.drafts.values())) and not re.search(r'นัด|ปฏิทิน|'+re.escape(d['draft_ref']),self.h._current['text']):raise PlanningError('calendar_ambiguous_cancel_target')
                blocked=self.h._blocked_action()
                if blocked:raise PlanningError('calendar_action_pending')
                d.update(status='cancelled',review=None,version=d['version']+1,updated_at=self.h._current['at'])
                self.h.state.calendar_drafts[d['draft_ref']]=d;self.seal(d);self.h.state.checkpoint()
                return {'domain':'calendar','ok':True,'status':'cancelled','draft_ref':d['draft_ref']}
            if p.operation!='review' or not re.search(r'ตรวจ|ทบทวน|ดูร่าง|บันทึก|สร้าง|review|save',self.h._current['text'],re.I):raise PlanningError('calendar_review_not_requested')
            d.update(version=d['version']+1,updated_at=self.h._current['at']);d['start'],d['end'],d['missing_fields'],d['issues']=calculate(d)
            return self.save_review(d)
        except (ValueError,KeyError,TypeError) as error:
            code=str(error) if isinstance(error,PlanningError) else str(error) if type(error) is ValueError and re.fullmatch(r'calendar_[a-z_]+',str(error)) else 'calendar_invalid_arguments'
            metadata=recovery(error,kind=None if isinstance(error,PlanningError) else 'assistant_arguments',tools=[name,'patch_pending_records','resolve_temporal','read_calendar_drafts'])
            if code in {'calendar_batch_stale','calendar_draft_stale_or_unavailable','calendar_stale_draft_version'} and self.batches.refresh_requested(self.h._current['text']):
                metadata['handled_by']='calendar_review_refresh'
            return {'domain':'calendar','ok':False,'status':'invalid_request','error':code,
                'recovery':metadata,
                **({'unsupported_features':error.unsupported_features} if hasattr(error,'unsupported_features') else {})}
    def render(self,output):
        if output.get('ok') and 'calendar_batch_view' in output:return self.batches.render(output)
        data={'domain':'calendar','tool_result':output,'calendar_reason':'นัดต้องมีชื่อ วัน เวลาเริ่ม และเวลาสิ้นสุดหรือระยะเวลาจากเจ้าของ แล้วจึงตรวจร่างและยืนยันก่อนสร้าง'}
        if not output.get('ok'):
            messages={'calendar_ambiguous_source':'มีตัวเลือกมากกว่าหนึ่งแบบ กรุณาระบุรายละเอียดนัดที่ต้องการใช้',
                'calendar_timezone_unsupported':'รอบนี้ใช้เวลาไทย (Asia/Bangkok) กรุณาระบุวันและเวลาไทยที่ต้องการก่อนตรวจร่าง',
                'calendar_timed_single_creation_only':'คำขอนี้มีรูปแบบนัดที่ยังเพิ่มให้ไม่ได้ จึงยังไม่ได้สร้างนัดครับ',
                'calendar_event_type_conflict':'ต้องการนัดทั้งวันหรือนัดระบุเวลาครับ? ข้อมูลสองแบบนี้ยังขัดกัน จึงยังไม่ได้สร้างนัด',
                'calendar_all_day_source':'ยังยืนยันไม่ได้ว่าต้องการเปลี่ยนเป็นนัดทั้งวันหรือระบุเวลา จึงยังไม่ได้สร้างนัดครับ',
                'calendar_action_pending':'ยังต้องตรวจสอบผลรายการเดิมก่อนแก้หรือสร้างนัดเพิ่ม',
                'calendar_no_fact_change':'รายละเอียดร่างยังไม่เปลี่ยน กรุณาระบุข้อมูลที่ต้องการแก้ หรือขอตรวจร่าง',
                'calendar_owner_no_write':'ยังไม่สร้างนัดตามที่คุณระบุ',
                'calendar_ambiguous_cancel_target':'มีทั้งรายจ่ายและนัดที่รอยืนยัน ต้องการยกเลิกรายการไหนครับ?'}
            messages.update(calendar_event_capacity='รองรับสูงสุด 8 นัดต่อชุด ยังไม่ยืนยันหรือสร้างบางส่วน',
                calendar_event_fact_pairing='วันหรือเวลายังผูกกับนัดที่ระบุไม่ได้ กรุณาระบุชื่อนัดหรือหมายเลขที่ต้องการแก้',
                calendar_ambiguous_event_target='มีหลายร่างนัด กรุณาระบุชื่อนัด หมายเลข หรือระบุว่าทั้งชุด',
                calendar_shared_target_count='จำนวนวัน/นัดที่ระบุร่วมกันไม่ตรงกับชุด กรุณาระบุรายการที่ต้องการใช้เวลาเดียวกัน',
                calendar_batch_required='นัดนี้อยู่ในชุด กรุณาตรวจหรือแก้ชุดนัดล่าสุด')
            message=messages.get(output.get('error'),'ขอรายละเอียดนัดที่ชัดเจนจากข้อความของคุณ เพื่อจัดเตรียมร่างให้ตรวจสอบ')
            if output.get('error') in {'calendar_event_type_conflict','calendar_all_day_source'}:
                return Result('clarification',message,data)
            if output.get('error')=='calendar_timed_single_creation_only':
                selected=output.get('unsupported_features',[])
                labels={'recurrence':'นัดซ้ำ','attendees':'คำเชิญผู้เข้าร่วม','reminders':'การตั้งเตือน'}
                phrases={'recurrence':'สร้างนัดซ้ำ','attendees':'ส่งคำเชิญให้ผู้เข้าร่วม','reminders':'ตั้งเตือน'}
                if len(selected)==1 and selected[0] in phrases:message='ตอนนี้ยัง'+phrases[selected[0]]+'ให้ไม่ได้ จึงยังไม่ได้สร้างนัดนี้ครับ'
                elif selected and all(k in labels for k in selected):message='คำขอมี'+ ' / '.join(labels[k] for k in selected)+' ซึ่งยังรองรับไม่ได้ จึงยังไม่ได้สร้างนัดนี้ครับ'
                return Result('clarification',message,data)
            return Result('clarification','ยังไม่ได้สร้างนัด\n'+message,data)
        if output['status']=='cancelled':return Result('cancelled','ยกเลิกร่างนัดแล้ว ไม่มีการลบนัดที่สร้างไว้',data)
        if output['status']=='draft_read':
            values=output['drafts'];return Result('answer','ร่างนัดที่ยังไม่บันทึก:\n'+'\n'.join((v['facts']['title'] or 'ยังไม่มีชื่อ')+' '+(v['start'] or 'ยังไม่มีเวลา') for v in values) if values else 'ไม่มีร่างนัดที่ค้างอยู่',data)
        d=self.draft(output['draft_ref']);data['calendar_view']=deepcopy(output['calendar_view'])
        if output['status']!='review':
            fields=clarification_fields(d)
            needed=list(dict.fromkeys([NAMES.get(k,k) for k in [*fields,*d['issues']] if k!='time_or_all_day']))
            message='ยังไม่ได้สร้างนัด '+(d['facts']['title'] or '')
            if 'time_or_all_day' in fields and d['facts']['date_text']:message+='\nวันนัด: '+d['facts']['date_text']
            if needed:message+='\nขอเฉพาะข้อมูลที่ยังขาด/ไม่ชัดเจน: '+', '.join(needed)
            if 'time_or_all_day' in fields:message+='\n'+timing_question()
            data['calendar_reason']=message;return Result('clarification',message,data)
        r=d['review'];a,b=interval(d)
        message=f"ตรวจสอบนัด: {d['facts']['title']}\nปฏิทิน: {d['calendar_label']}\n"
        message+=('ทั้งวัน '+date_label(d['start'])+'\n' if d.get('all_day') else f'เริ่ม {a:%Y-%m-%d %H:%M}\nสิ้นสุด {b:%Y-%m-%d %H:%M} (Asia/Bangkok)\n')
        message+=f"มีนัดทับในปฏิทินนี้ {len(r['conflicts'])} รายการ\n" if r['conflicts'] else 'ตรวจเฉพาะปฏิทินนี้ ไม่ได้ตรวจปฏิทินหลัก\n'
        if a<self.h.clock():message+='เวลาเริ่มนี้อยู่ในอดีต กรุณาตรวจสอบก่อนยืนยัน\n'
        message+='ยืนยันสร้างนัดนี้ไหมครับ? ตอบ ยืนยัน หรือใช้ปุ่ม'
        data['confirm_command']='ยืนยันนัด '+d['draft_ref']+' '+r['nonce']
        from .conversation_ui import render_actions
        data['line_message']=render_actions(message,[{'type':'postback','label':label,
            'data':f"c1|{operation}|{d['draft_ref']}|{d['version']}|{r['nonce']}",'displayText':label} for operation,label in [('confirm','ยืนยันนัด'),('cancel','ยกเลิกร่างนัด')]])
        return Result('preview',message,data)
    @staticmethod
    def cancel_intent(text):
        return not re.search(r'ไม่ต้องยกเลิก|อย่ายกเลิก|ห้ามยกเลิก',text) and bool(re.search(r'ยกเลิก|ไม่เอา|ทิ้งร่าง|ลบร่าง',text))
    def delivered(self,d,*,latest=False):
        r=d['review'];receipt=self.h.state.receipts.get(r['event']) if r else None
        if not receipt or receipt.result.status!='preview' or receipt.result.data.get('domain')!='calendar' or receipt.result.data.get('confirm_command')!='ยืนยันนัด '+d['draft_ref']+' '+r['nonce']:return False
        from .line_api import message_hash,rendered_message
        if getattr(self.h,'production',False):
            reply=self.h.state.replies.get(r['event'])
            if not reply or reply.status!='sent' or reply.content_hash!=message_hash(rendered_message(receipt.result)):return False
        if latest:
            row,shown=self.h._presented_result()
            if not row:return False
            if row['event']!=r['event'] or shown!=receipt.result:
                # A rejected old nonce for this same draft changes no facts.
                # Skip only that exact server-validated stale confirmation;
                # any edit, read, other domain or missing delivery still blocks.
                after=[x for x in self.h.state.history if x['role']=='assistant' and x['event']!=self.h._active_event_id
                    and datetime.fromisoformat(r['at'])<datetime.fromisoformat(x['at'])<datetime.fromisoformat(self.h._current['at'])]
                if not after:return False
                for x in after:
                    old=self.h.state.receipts.get(x['event']);reply=self.h.state.replies.get(x['event'])
                    owner=next((v.get('text','') for v in self.h.state.history if v['role']=='user' and v['event']==x['event']),'')
                    if (not old or old.expires_at<=self.h.clock() or old.result.status!='stale' or old.result.data.get('tool_result')
                        or not re.fullmatch(r'ยืนยันนัด '+re.escape(d['draft_ref'])+r' [a-f0-9]{16}',owner)):
                        return False
                    if getattr(self.h,'production',False) and (not reply or reply.status!='sent' or reply.content_hash!=message_hash(rendered_message(old.result))):return False
        return True
    def confirm(self,ref,nonce,*,latest=False):
        try:d=self.draft(ref)
        except ValueError:return Result('stale','ร่างนัดเปลี่ยนหรือหมดอายุแล้ว')
        r=d['review']
        if (not self.enabled or d['status']!='review' or not r or not hmac.compare_digest(r['nonce'],nonce)
            or r['payload_hash']!=payload_hash(d) or datetime.fromisoformat(self.h._current['at'])<=datetime.fromisoformat(r['at']) or not self.delivered(d,latest=latest)):
            return Result('stale','กรุณาตรวจสอบร่างนัดล่าสุดก่อนยืนยัน')
        blocked=self.h._blocked_action()
        if blocked:return self.h._uncertain(blocked)
        try:conflicts=self.adapter.conflicts(*interval(d))
        except Exception:return Result('setup_required','ยังตรวจสอบปฏิทินไม่ได้ จึงยังไม่สร้างนัด')
        if digest(conflicts)!=r['conflicts_hash']:
            d=deepcopy(d);d.update(version=d['version']+1,updated_at=self.h._current['at']);return self.render(self.save_review(d))
        key=self.h.settings.owner_id+':calendar:'+digest([d['calendar_id'],ref,d['version'],r['payload_hash']])[:32]
        d['status']='committing';self.seal(d)
        result=self.h._execute(key,'create_event',payload(d),ref,{'calendar_mvp':True,'calendar_id':d['calendar_id'],'calendar_label':d['calendar_label'],
            'calendar_draft_ref':ref,'calendar_draft_version':d['version'],'calendar_payload_hash':r['payload_hash'],'draft_expires_at':d['expires_at']})
        if result.status in {'setup_required','expired','conflict','not_found'}:
            d['status']='needs_information';d['review']=None;self.seal(d);self.h.state.checkpoint()
        return result
    def control(self,text):
        batch=self.batches.control(text)
        if batch is not None:return batch
        active=[d for d in self.h.state.calendar_drafts.values() if d['status'] not in {'recorded','cancelled'}]
        expenses=bool(self.h.state.pending) or any(d['status']!='recorded' for d in self.h.state.drafts.values())
        if re.fullmatch(r'ยกเลิก(?:\s*(?:ครับ|ค่ะ|คะ))?',text) and active and expenses:
            return Result('clarification','มีทั้งรายจ่ายและนัดที่รอยืนยัน ต้องการยกเลิกรายการไหนครับ?')
        if re.fullmatch(r'ตรวจ(?:สอบ)?ผลนัด(?:ล่าสุด)?',text):
            keys=[k for k,a in self.h.state.actions.items() if a.metadata.get('calendar_mvp') and a.status in {'executing','uncertain'}]
            if len(keys)!=1:return Result('clarification','ไม่พบผลการสร้างนัดที่ไม่แน่นอนเพียงรายการเดียวให้ตรวจสอบ',{'domain':'calendar'})
            self.h.state.claims[self.h._active_event_id].action_key=keys[0];self.h.state.checkpoint()
            return self.h._resume(keys[0])
        plain=bool(re.fullmatch(r'ยืนยัน(?:\s*(?:ครับ|ค่ะ|คะ))?',text))
        explicit=re.fullmatch(r'ยืนยันนัด (cal_[a-f0-9]{20}) ([a-f0-9]{16})',text)
        postback=re.fullmatch(r'c1\|(confirm|cancel)\|(cal_[a-f0-9]{20})\|([1-9]\d*)\|([a-f0-9]{16})',text)
        if explicit:return self.confirm(*explicit.groups())
        if postback:
            operation,ref,version,nonce=postback.groups()
            if self.h._active_event_kind!='postback':return Result('stale','ปุ่มนัดไม่ถูกต้อง')
            try:d=self.draft(ref)
            except ValueError:return Result('stale','ร่างนัดเปลี่ยนหรือหมดอายุแล้ว')
            if d['version']!=int(version) or not d['review'] or not hmac.compare_digest(d['review']['nonce'],nonce) or not self.delivered(d) or datetime.fromisoformat(self.h._current['at'])<=datetime.fromisoformat(d['review']['at']):return Result('stale','ปุ่มนัดเปลี่ยนหรือหมดอายุแล้ว')
            if operation=='confirm':return self.confirm(ref,nonce)
            blocked=self.h._blocked_action()
            if blocked:return self.h._uncertain(blocked)
            d.update(status='cancelled',review=None,version=d['version']+1,updated_at=self.h._current['at']);self.seal(d);self.h.state.checkpoint()
            return Result('cancelled','ยกเลิกร่างนัดแล้ว ไม่มีการลบนัดที่สร้างไว้',{'domain':'calendar'})
        if plain and active:
            if (len(active)==1 and self.h.historical_categories.has_pending()
                and not any(d['status']!='recorded' for d in self.h.state.drafts.values())
                and self.delivered(active[0],latest=True)):
                # A separately waiting historical mapping review is not an
                # expense draft. The latest delivered Calendar review owns
                # this confirmation; all Calendar guards still run below.
                expenses=False
            if len(active)!=1 or expenses:return Result('clarification','มีหลายร่างหรือมีทั้งรายจ่ายและนัด ต้องการยืนยันร่างใดครับ? ระบุชื่อหรือวันของร่างที่ต้องการตรวจได้')
            d=active[0];return self.confirm(d['draft_ref'],d['review']['nonce'] if d['review'] else '',latest=True)
        if re.fullmatch(r'ยกเลิก(?:ร่าง)?นัด(?:ล่าสุด|นี้)?',text) and not active:
            return Result('clarification','นัดที่สร้างแล้วไม่ถูกลบด้วยการยกเลิกร่าง รอบนี้รองรับการสร้างและยกเลิกร่างนัดเท่านั้น',{'domain':'calendar'})
        from .validation_explanations import asks_why
        _,shown=self.h._presented_result()
        if asks_why(text) and shown and shown.data.get('domain')=='calendar':
            return Result('answer',shown.data.get('calendar_reason','ผลล่าสุดเป็นสถานะนัดที่ระบบตรวจสอบแล้ว ไม่ได้ใช้กฎวันที่ของรายจ่าย'),{'domain':'calendar'})
        return None
    def can_resume(self,action):
        m=action.metadata
        return bool(self.enabled and action.kind=='create_event' and m.get('calendar_id')==self.calendar_id and getattr(self.adapter,'calendar_id',None)==self.calendar_id and self.adapter is self.h.calendar
            and digest([self.h.settings.owner_id,self.calendar_id,action.payload])==m.get('calendar_payload_hash'))
    def failed(self,action,result):
        if result.status not in {'setup_required','expired','conflict','not_found'}:return
        d=self.h.state.calendar_drafts.get(action.metadata.get('calendar_draft_ref'))
        if d and d['status']=='committing':
            d.update(status='needs_information',review=None);self.seal(d);self.h.state.checkpoint()
    def complete(self,key,receipt):
        action=self.h.state.actions[key];args=parse_payload(action.payload)
        expected=__import__('hashlib').sha256(key.encode()).hexdigest()
        if (getattr(receipt,'id',None)!=expected or receipt.title!=args.title or receipt.start!=args.start or receipt.end!=args.end
            or receipt.native_reminders!={'useDefault':False,'overrides':[]}):
            action.status='uncertain';self.h.state.checkpoint();return self.h._uncertain(key)
        m=action.metadata;ref=m['calendar_draft_ref'];d=self.h.state.calendar_drafts.get(ref)
        if d:d.update(status='recorded',review=None);self.seal(d)
        when=('ทั้งวัน '+date_label(args.start.isoformat()) if getattr(args,'all_day',False) else f'{args.start.astimezone(BANGKOK):%Y-%m-%d %H:%M} ถึง {args.end.astimezone(BANGKOK):%Y-%m-%d %H:%M} (Asia/Bangkok)')
        result=Result('created',f"เพิ่มนัด {args.title} เรียบร้อยแล้วครับ\n{when}",
            {'domain':'calendar','event_id':receipt.id,'draft_ref':ref,'start':args.start.isoformat(),'end':args.end.isoformat(),'calendar_label':m['calendar_label'],'all_day':getattr(args,'all_day',False)})
        self.h.state.last=LastAction('event',(receipt.id,),datetime.fromisoformat(m['last_expires_at']))
        action.status='completed';action.result=deepcopy(result);self.h.state.checkpoint();self.h.phase_hook('action_completed',key)
        return result
