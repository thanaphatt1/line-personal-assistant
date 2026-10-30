"""Bounded Calendar sets with persistent slots and independently durable effects."""
from .expense_storage import storage_scope
from copy import deepcopy
from datetime import datetime,timedelta
import hmac,re,secrets
from pydantic import Field
from .calendar_workflow import Arguments,Edit,FIELDS,calculate,payload,payload_hash,fingerprint,literal_fact,literal_guard_text,NAMES,approximate_notices
from .reply_display import appointment_text
from .calendar_dates import bangkok_zone,event_date
from .calendar_event_types import interval,date_label,clarification_fields,timing_question
from . import calendar_sources as sources
from .tool_errors import PlanningError,recovery
from .history_context import digest
from .model_api import object_schema
from .state import Action,Result
from .language import BANGKOK,intent_guard

ORDINAL=re.compile(r'(?:นัด|รายการ|อัน|คอร์ส)(?:ที่)?\s*(แรก|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|[1-8๑-๘])')

class BatchEdit(Arguments):
    batch_ref:str|None=None
    batch_version:int|None=Field(default=None,ge=1)
    events:list[Edit]=Field(min_length=1,max_length=8)
class BatchControl(Arguments):
    batch_ref:str
    batch_version:int=Field(ge=1)
    operation:str
    event_refs:list[str]=Field(max_length=8)

def batch_fingerprint(batch):return digest(batch)
def members_hash(host,batch):
    return digest([host.settings.owner_id,host.calendar_flow.calendar_id,
        [[ref,host.state.calendar_drafts[ref]['version'],payload_hash(host.state.calendar_drafts[ref])]
            for ref in batch['members'] if host.state.calendar_drafts[ref]['status'] not in {'cancelled','recorded'}]])
def restore_batches(state,value):
    if not isinstance(value,dict) or set(value)!={'format','batches','proofs'} or value['format']!='calendar-batches-v1':raise PlanningError('Invalid Calendar batches envelope')
    batches=value['batches'];proofs=value['proofs']
    if not isinstance(batches,dict) or not isinstance(proofs,dict) or set(batches)!=set(proofs) or len(batches)>32:raise PlanningError('Invalid Calendar batch records')
    claimed=set()
    for ref,b in batches.items():
        if (not re.fullmatch(r'calset_[a-f0-9]{20}',ref) or b.get('batch_ref')!=ref or b.get('owner_id')!=state.owner_id
            or type(b.get('version')) is not int or b['version']<1 or b.get('status') not in {'needs_information','review','committing','partial','recorded','cancelled'}
            or proofs[ref]!=batch_fingerprint(b) or not isinstance(b.get('members'),list) or not 1<=len(b['members'])<=8 or len(set(b['members']))!=len(b['members'])):raise PlanningError('Invalid Calendar batch binding')
        for field in ['created_at','updated_at','expires_at']:
            if datetime.fromisoformat(b[field]).utcoffset() is None:raise PlanningError('Invalid Calendar batch clock')
        for index,child in enumerate(b['members']):
            d=state.calendar_drafts.get(child)
            if (child in claimed or not d or d.get('batch_ref')!=ref or d.get('ordinal')!=index+1 or d['calendar_id']!=b['calendar_id']):raise PlanningError('Invalid Calendar batch membership')
            unit=d.get('source_unit')
            if (not isinstance(unit,dict) or set(unit)!={'source_ref','start','end','text','at','slot_key','occurrence'}
                or type(unit['start']) is not int or type(unit['end']) is not int or not 0<=unit['start']<unit['end']<=2000
                or type(unit['occurrence']) is not int or not 0<=unit['occurrence']<8
                or len(unit['text'])!=unit['end']-unit['start'] or unit['slot_key']!=digest([unit['source_ref'],unit['start'],unit['end'],unit['occurrence']])[:20]):raise PlanningError('Invalid Calendar source slot')
            claimed.add(child)
        if b['review']:
            r=b['review']
            if b['status'] not in {'review','committing'} or not re.fullmatch(r'[a-f0-9]{16}',r['nonce']) or r['version']!=b['version']:raise PlanningError('Invalid Calendar batch review')
        key=b.get('action_key')
        if key:
            action=state.actions.get(key)
            if (not action or not action.metadata.get('calendar_batch') or action.metadata.get('batch_ref')!=ref or action.metadata.get('calendar_id')!=b['calendar_id']
                or action.metadata.get('payload_hash')!=digest([state.owner_id,b['calendar_id'],action.payload])):raise PlanningError('Invalid Calendar batch action')
    state.calendar_batches=deepcopy(batches);state.calendar_batch_proofs=deepcopy(proofs)

class CalendarBatches:
    names={'prepare_calendar_events','control_calendar_events'}
    def __init__(self,flow):self.f=flow;self.h=flow.h;self.request_views={}
    def seal(self,b):self.h.state.calendar_batch_proofs[b['batch_ref']]=batch_fingerprint(b)
    def get(self,ref,*,active=True):
        b=self.h.state.calendar_batches.get(ref)
        if (not b or b['owner_id']!=self.h.settings.owner_id or b['calendar_id']!=self.f.calendar_id
            or self.h.state.calendar_batch_proofs.get(ref)!=batch_fingerprint(b)
            or active and b['status'] in {'recorded','cancelled'}
            or datetime.fromisoformat(b['expires_at'])<=self.h.clock() and b['status']!='committing'):raise PlanningError('calendar_batch_stale')
        return b
    def child(self,ref):
        d=self.h.state.calendar_drafts[ref]
        if self.h.state.calendar_proofs.get(ref)!={'owner_id':d['owner_id'],'version':d['version'],'fingerprint':fingerprint(d)}:raise PlanningError('calendar_draft_stale_or_unavailable')
        return d
    def view(self,b,children=None):
        child=self.child if children is None else children.__getitem__
        return {'batch_ref':b['batch_ref'],'version':b['version'],'status':b['status'],'events':[
            {**{k:deepcopy(child(ref)[k]) for k in ['draft_ref','version','ordinal','status','facts','start','end','missing_fields','issues','source_unit']},'all_day':child(ref).get('all_day',False)} for ref in b['members']]}
    def views(self):
        values=[];self.request_views={};focused=self.focused_edit_ref()
        for ref in self.h.state.calendar_batches:
            if focused and ref!=focused:continue
            try:b=self.get(ref)
            except ValueError:continue
            # Show the model the same proven reconciliation that prepare will
            # perform. This is a read-only projection; consent stays absent and
            # binding still checks the original persisted version/fingerprint.
            projected=deepcopy(b);children={r:deepcopy(self.child(r)) for r in b['members']}
            if b['status']=='needs_information' and not b.get('action_key'):
                self.remove_empty_shadows(projected,children,self.h._current)
            values.append(self.view(projected,children));self.request_views[ref]=batch_fingerprint(b)
        return values
    def focused_edit_ref(self):
        """Immediate ordinal edits follow the proven displayed set, not model choice."""
        text=(self.h._current or {}).get('text','')
        if (not re.search(r'แก้|เลื่อน|เปลี่ยน|ขยับ',text) or not ORDINAL.search(text)
            or re.search(r'calset_[a-f0-9]{20}|ชุด(?:เก่า|ก่อนหน้า|แรก)',text)):return None
        _,shown=self.h._presented_result();view=shown.data.get('calendar_batch_view',{}) if shown else {}
        try:b=self.get(view.get('batch_ref'))
        except (ValueError,KeyError,TypeError):return None
        return b['batch_ref'] if view.get('version')==b['version'] and b['status'] in {'review','needs_information'} else None
    def definitions(self):
        item=deepcopy(self.f.single_definitions()[0]['parameters'])
        return [{'type':'function','name':'prepare_calendar_events','strict':True,
            'description':'Prepare ALL requested appointments in one set, including incomplete ones. Each entry is one distinct event. New draft_ref/version null. Updates echo batch and per-event refs/versions, send only changed fields; omitted events are retained. Original source_refs required. Shared times/dates must be applied to every intended event. Never confirm or write.',
            'parameters':object_schema({'batch_ref':{'type':['string','null']},'batch_version':{'type':['integer','null']},'events':{'type':'array','minItems':1,'maxItems':8,'items':item}})},
            {'type':'function','name':'control_calendar_events','strict':True,
                'description':'Explicit review, removal of selected pending appointments, or cancellation of a pending set. Never removes saved events or confirms writes. event_refs selects members; empty means whole set only for review/cancel.',
                'parameters':object_schema({'batch_ref':{'type':'string'},'batch_version':{'type':'integer'},'operation':{'type':'string','enum':['review','remove','cancel']},'event_refs':{'type':'array','items':{'type':'string'},'maxItems':8}})}]
    def bound(self,ref,version):
        b=self.get(ref)
        if b['version']!=version or self.request_views.get(ref)!=batch_fingerprint(b):raise PlanningError('calendar_batch_stale')
        if datetime.fromisoformat(self.h._current['at'])<=datetime.fromisoformat(b['updated_at']):raise PlanningError('calendar_older_update')
        if b['status']=='committing' or self.h._blocked_action():raise PlanningError('calendar_action_pending')
        return b
    def selected(self,b,text):
        selected=set();words={'แรก':1,'หนึ่ง':1,'สอง':2,'สาม':3,'สี่':4,'ห้า':5,'หก':6,'เจ็ด':7,'แปด':8}
        for match in ORDINAL.finditer(text):
            n=words.get(match[1]) or int(match[1].translate(__import__('line_assistant.language',fromlist=['DIGITS']).DIGITS))
            if n<=len(b['members']):selected.add(b['members'][n-1])
        for ref in b['members']:
            d=self.child(ref)
            if ref in text or d['facts']['title'] and d['facts']['title'] in text:selected.add(ref)
        return selected
    def placeholder(self,b,unit):
        ref='cal_'+digest([b['batch_ref'],unit['slot_key']])[:20];at=self.h._current['at']
        d={'draft_ref':ref,'owner_id':self.h.settings.owner_id,'calendar_id':self.f.calendar_id,'calendar_label':self.f.label,
            'version':1,'status':'needs_information','facts':dict.fromkeys(FIELDS),'fact_sources':{},'anchors':{},'source_refs':[unit['source_ref']],
            'created_at':at,'updated_at':at,'expires_at':b['expires_at'],'start':None,'end':None,'missing_fields':['title','date','time','end'],'issues':[],
            'review':None,'batch_ref':b['batch_ref'],'ordinal':len(b['members'])+1,'source_unit':deepcopy(unit)}
        b['members'].append(ref);return d
    def prepare(self,args,*,interpreted_patch=False,field_evidence=None):
        p=BatchEdit.model_validate(args);current=self.h._current
        focused=self.focused_edit_ref()
        if focused and p.batch_ref!=focused:
            latest=deepcopy(self.get(focused))
            if latest['review']:
                latest.update(version=latest['version']+1,updated_at=current['at'],status='needs_information',review=None)
                self.h.state.calendar_batches[focused]=latest;self.seal(latest);self.h.state.checkpoint()
            raise PlanningError('calendar_ambiguous_event_target')
        old=deepcopy(self.bound(p.batch_ref,p.batch_version)) if p.batch_ref else None
        if old and old['review']:
            # Revoke delivered consent before validating the proposed changes.
            # A failed/ambiguous edit must not leave an old save button live.
            revoked=deepcopy(old);revoked.update(version=old['version']+1,updated_at=current['at'],status='needs_information',review=None)
            self.h.state.calendar_batches[old['batch_ref']]=revoked;self.seal(revoked);self.h.state.checkpoint()
        guard=current['text']
        for title in [e.title for e in p.events if e.title]:guard=literal_guard_text(guard,title)
        if not self.h.raw_history_enabled and (intent_guard(guard) or re.search(r'(?:อย่า|ไม่ต้อง|ยังไม่ต้อง|ห้าม)(?:\s|เพิ่ง)*(?:สร้าง|นัด|จอง|เพิ่ม)',guard)):raise PlanningError('calendar_owner_no_write')
        bangkok_zone(current['text'])
        if not old and p.batch_version is not None:raise PlanningError('calendar_unbound_version')
        if not old and len([b for b in self.h.state.calendar_batches.values() if b['status'] not in {'recorded','cancelled'}])>=8:raise PlanningError('calendar_too_many_drafts')
        at=current['at'];b=deepcopy(old) if old else {'batch_ref':'calset_'+digest([self.h.settings.owner_id,self.f.calendar_id,self.h._active_event_id])[:20],
            'owner_id':self.h.settings.owner_id,'calendar_id':self.f.calendar_id,'version':0,'members':[],'created_at':at,
            'updated_at':at,'expires_at':(self.h.clock()+timedelta(seconds=self.h.settings.pending_ttl_seconds)).isoformat(),'status':'needs_information','review':None,'action_key':None}
        b.update(version=b['version']+1,updated_at=at,review=None,status='needs_information',action_key=None)
        children={ref:deepcopy(self.child(ref)) for ref in b['members']}
        if not old:
            legacy=[e for e in p.events if e.draft_ref]
            for e in legacy:
                d=deepcopy(self.f.bound(e))
                origin=self.h._owner_sources(d['source_refs']);title=d['facts']['title']
                candidates=[u for row in origin for u in sources.units(row,[title] if title else []) if not title or title in u['text']]
                if len(candidates)!=1:raise PlanningError('calendar_event_selection')
                d.update(batch_ref=b['batch_ref'],ordinal=len(b['members'])+1,source_unit=candidates[0]);b['members'].append(d['draft_ref']);children[d['draft_ref']]=d
        refs=list(dict.fromkeys(r for e in p.events for r in e.source_refs));rows=self.h._owner_sources(refs)
        p=p.model_copy(update={'events':sources.repeated_names(p.events,rows)})
        titles=list(dict.fromkeys([*(e.title for e in p.events if e.title),*(d['facts']['title'] for d in children.values() if d['facts']['title'])]))
        # Referencing an earlier turn supplies evidence, not more occurrences.
        # A complete restatement owns slot discovery; genuinely split requests
        # still discover their missing titles in the earlier messages.
        restated=bool(titles and all(title in current['text'] for title in titles))
        if restated:
            p=p.model_copy(update={'events':[e.model_copy(update={'source_refs':list(dict.fromkeys([*e.source_refs,current['source_ref']]))}) for e in p.events]})
            rows=self.h._owner_sources(list(dict.fromkeys([*refs,current['source_ref']])))
        unit_map={}
        discovery=[current] if restated else rows
        for row in discovery:
            # Only actual new appointment content is discovered. A fact-only
            # followup updates existing slots and cannot fabricate a new event.
            for unit in sources.units(row,titles):unit_map[unit['slot_key']]=unit
        if not old and not children:unit_map=sources.temporal_occurrences(self.h,unit_map,p.events)
        if not old:
            if restated:
                # Explicitly adopted v12 drafts retain their IDs. Rebind only
                # an unambiguous literal source unit in the fresh restatement.
                for ref,d in children.items():
                    edit=next(e for e in p.events if e.draft_ref==ref)
                    candidates=[u for u in unit_map.values() if d['facts']['title'] and d['facts']['title'] in u['text']
                        and all(literal_fact(getattr(e,f) or d['facts'][f],u['text'],f) is not None for e in p.events if e.draft_ref==ref for f in ['date_text'] if getattr(e,f) or d['facts'][f])]
                    if len(candidates)!=1:raise PlanningError('calendar_event_selection')
                    d['source_unit']=deepcopy(candidates[0])
                    value=d['facts']['date_text'];anchor=d['anchors'].get('date_text')
                    if value and anchor and (edit.date_text is None or edit.date_text==value):
                        if event_date(value,datetime.fromisoformat(anchor['at']))!=event_date(value,datetime.fromisoformat(current['at'])):
                            if edit.date_text is None:raise PlanningError('calendar_ambiguous_source')
                            # "Tomorrow" can have the same spelling but a new
                            # date after midnight. Certify the fresh literal.
                            d['facts']['date_text']=None;d['fact_sources'].pop('date_text');d['anchors'].pop('date_text')
            existing_keys={d['source_unit']['slot_key'] for d in children.values()}
            consumed_keys={d['source_unit']['slot_key'] for d in self.h.state.calendar_drafts.values() if d.get('source_unit') and d['status'] in {'cancelled','recorded'}}
            for unit in unit_map.values():
                if unit['slot_key'] in existing_keys or unit['slot_key'] in consumed_keys:continue
                d=self.placeholder(b,unit);children[d['draft_ref']]=d
        else:
            existing_keys={d['source_unit']['slot_key'] for d in children.values()}
            for unit in unit_map.values():
                if unit['source_ref']!=current['source_ref'] or unit['slot_key'] in existing_keys:continue
                if any(e.draft_ref is None and e.title and e.title in unit['text'] for e in p.events):
                    d=self.placeholder(b,unit);children[d['draft_ref']]=d
        if not b['members'] or len(b['members'])>8:raise PlanningError('calendar_event_capacity')
        if old and restated and old['status']=='needs_information' and not old.get('action_key'):
            # Reconcile proven empty duplicates before interpreting "both";
            # the old malformed member count must not block its own repair.
            self.remove_empty_shadows(b,children,current)
        selected=self.selected(old,current['text']) if old else set()
        live=[ref for ref in b['members'] if children[ref]['status'] not in {'recorded','cancelled'}]
        shared=sources.shared_changes(current,len(live),p.events,titles) if old else {}
        edits=list(p.events)
        if shared:
            by_ref={e.draft_ref:e for e in edits if e.draft_ref}
            for ref in live:
                if ref in by_ref:edits[edits.index(by_ref[ref])]=by_ref[ref].model_copy(update={**shared,'source_refs':[current['source_ref']]})
                else:edits.append(Edit(draft_ref=ref,draft_version=children[ref]['version'],source_refs=[current['source_ref']],**shared))
        used=set()
        if not old and self.refresh_requested(current['text']) and len(live)>1:
            # A refresh may cite an original owner row containing one exact
            # shared clock pair while the proposal omits every clock. Reuse
            # only that explicit shared pair for its same-source slots. Every
            # value still goes through the ordinary literal certifier below.
            for row in rows:
                if not all(children[ref]['source_unit']['source_ref']==row['source_ref'] for ref in live):continue
                shared_clocks=sources.shared_clock(row,len(live))
                if not shared_clocks:continue
                edits=[e.model_copy(update={**shared_clocks,'source_refs':list(dict.fromkeys([*e.source_refs,row['source_ref']]))})
                    if e.all_day is not True and not any([e.time_text,e.end_time_text,e.duration_text]) else e for e in edits]
        for e in edits:
            if e.draft_ref:
                if e.draft_ref not in live or e.draft_ref in used:raise PlanningError('calendar_event_selection')
                d=children[e.draft_ref]
                if e.draft_version!=d['version']:raise PlanningError('calendar_stale_draft_version')
                changes=[f for f in FIELDS if getattr(e,f) is not None and getattr(e,f)!=d['facts'][f]]
                if e.all_day is not None and e.all_day!=d.get('all_day',False):changes.append('all_day')
                if not interpreted_patch and old and changes and not shared and len(live)>1 and selected!={e.draft_ref}:
                    missing=[ref for ref in live if any(f in children[ref]['missing_fields'] for f in ['time','end','date'])]
                    if selected or missing!=[e.draft_ref]:raise PlanningError('calendar_ambiguous_event_target')
            else:
                candidates=[d for d in children.values() if d['draft_ref'] not in used and d['status']=='needs_information' and not d['facts']['title'] and (not e.title or e.title in d['source_unit']['text'] or any(sources.shared_title(row,d['source_unit'],e.title,titles) for row in rows if row['source_ref']==d['source_unit']['source_ref']))]
                matching=[d for d in candidates if all(literal_fact(getattr(e,f),d['source_unit']['text'],f) is not None for f in ['date_text','time_text','end_time_text'] if getattr(e,f) is not None)]
                d=(matching or candidates)[0] if matching or candidates else None
                if not d:raise PlanningError('calendar_event_selection')
            ref=d['draft_ref'];used.add(ref);unit=d['source_unit']
            def scope(row,field,value,title):
                if row['source_ref']==unit['source_ref']:
                    try:return sources.bind(row,unit,title,field,value,titles)
                    except ValueError:return {**row,'text':''}
                if row['source_ref']==current['source_ref'] and ref in self.h.state.calendar_drafts:return row
                if sources.sharing(row,field,value,titles):return row
                return {**row,'text':''}
            arguments=e.model_dump();arguments.update(draft_ref=ref,draft_version=d['version'])
            # A new placeholder is not yet published. Existing validation runs
            # against staged copies with no checkpoint or provider effect.
            staged=self.f.prepare(arguments,new_draft=d,commit=False,scope=scope,allow_batch=True,guard_titles=titles,
                field_evidence=field_evidence.get(ref) if field_evidence is not None else None)
            if staged['facts']['title'] and (not d['facts']['title'] or 'calendar_unrepresented_event_text' in d['issues']) and sources.substantive(unit['text'],[staged['facts']['title']],facts=staged['facts']):
                staged['issues']=list(dict.fromkeys([*staged['issues'],'calendar_unrepresented_event_text']))
            children[ref]=staged
        if old and restated and old['status']=='needs_information' and not old.get('action_key'):
            self.remove_empty_shadows(b,children,current)
        # Missing model entries remain durable visible placeholders, blocking
        # the entire set's confirmation until all requested slots are covered.
        for ref,d in children.items():
            previous=self.h.state.calendar_drafts.get(ref)
            if previous and d.get('ordinal')!=previous.get('ordinal') and d['version']<=previous['version']:
                d.update(version=previous['version']+1,updated_at=current['at'])
            if d['status'] not in {'cancelled','recorded'}:d['review']=None;d['status']='needs_information'
        self._publish(b,children);return self.review(b)
    def remove_empty_shadows(self,b,children,current):
        represented=[d for d in children.values() if d['facts']['title'] and d['status'] not in {'recorded','cancelled'}]
        if not represented or not current.get('text') or any(d['facts']['title'] not in current['text'] for d in represented):return
        units=sources.units(current,[d['facts']['title'] for d in represented])
        if any(sum(sources.shadow_of(unit,d) for unit in units)!=1 for d in represented):return
        removed=set()
        for ref,d in children.items():
            if d['status']!='needs_information' or any(v is not None for v in d['facts'].values()):continue
            candidates=[other for other in represented if sources.shadow_of(d['source_unit'],other)]
            if len(candidates)!=1:continue
            other=candidates[0]
            same_source=[v for v in children.values() if v['source_unit']['source_ref']==d['source_unit']['source_ref'] and sources.shadow_of(v['source_unit'],other)]
            if len(same_source)!=1:continue
            d.update(status='cancelled',review=None,version=d['version']+1,updated_at=current['at']);removed.add(ref)
        if not removed:return
        b['members']=[ref for ref in b['members'] if ref not in removed]
        for ordinal,ref in enumerate(b['members'],1):
            d=children[ref]
            if d['ordinal']!=ordinal:d.update(ordinal=ordinal,updated_at=current['at'])
    def _publish(self,b,children):
        for ref,d in children.items():self.h.state.calendar_drafts[ref]=d;self.f.seal(d)
        self.h.state.calendar_batches[b['batch_ref']]=b;self.seal(b);self.h.state.checkpoint()
    def review(self,b):
        b=deepcopy(b);ready=True
        for ref in b['members']:
            d=deepcopy(self.child(ref))
            if d['status'] in {'recorded','cancelled'}:continue
            d['start'],d['end'],d['missing_fields'],computed=calculate(d)
            d['issues']=list(dict.fromkeys([*computed,*(x for x in d['issues'] if x=='calendar_unrepresented_event_text')]))
            self.f.review_draft(d)
            self.h.state.calendar_drafts[ref]=d;self.f.seal(d)
            ready=ready and d['status']=='review'
        active=[self.child(ref) for ref in b['members'] if self.child(ref)['status'] not in {'recorded','cancelled'}]
        b['status']='review' if active and ready else 'needs_information' if active else 'recorded' if any(self.child(ref)['status']=='recorded' for ref in b['members']) else 'cancelled'
        b['review']={'nonce':secrets.token_hex(8),'at':self.h._current['at'],'event':self.h._active_event_id,'version':b['version'],'payload_hash':members_hash(self.h,b)} if b['status']=='review' else None
        self.h.state.calendar_batches[b['batch_ref']]=b;self.seal(b);self.h.state.checkpoint()
        return {'domain':'calendar','ok':True,'status':'review' if b['status']=='review' else 'needs_information','batch_ref':b['batch_ref'],'batch_version':b['version'],'calendar_batch_view':self.view(b)}
    def tool(self,name,args,*,interpreted_patch=False,field_evidence=None):
        if not self.f.enabled:raise PlanningError('calendar_disabled')
        if name=='prepare_calendar_events':return self.prepare(args,interpreted_patch=interpreted_patch,field_evidence=field_evidence)
        p=BatchControl.model_validate(args);b=deepcopy(self.bound(p.batch_ref,p.batch_version))
        if p.operation=='review':
            if not re.search(r'ตรวจ|ดูร่าง|ทบทวน|บันทึก|สร้าง|review|save',self.h._current['text'],re.I):raise PlanningError('calendar_review_not_requested')
            b.update(version=b['version']+1,updated_at=self.h._current['at'],review=None);return self.review(b)
        text=self.h._current['text']
        removal=self.f.cancel_intent(text) or bool(re.search(r'ลบ(?:ร่าง)?(?:นัด|รายการ|อัน|คอร์ส)',text)) and not re.search(r'ไม่ต้องลบ|อย่าลบ|ห้ามลบ',text)
        if p.operation not in {'remove','cancel'} or not removal:raise PlanningError('calendar_cancel_not_requested')
        selected=set(p.event_refs)
        if p.operation=='cancel' and not selected:
            if len(b['members'])>1 and not re.search(r'ทั้งชุด|ทั้งหมด|ทุกนัด',self.h._current['text']):raise PlanningError('calendar_ambiguous_event_target')
            selected={ref for ref in b['members'] if self.child(ref)['status'] not in {'recorded','cancelled'}}
        elif selected!=self.selected(b,self.h._current['text']):raise PlanningError('calendar_ambiguous_event_target')
        if not selected or not selected<=set(b['members']):raise PlanningError('calendar_event_selection')
        for ref in selected:
            d=deepcopy(self.child(ref))
            if d['status'] in {'recorded','committing'}:raise PlanningError('calendar_action_pending')
            d.update(status='cancelled',review=None,version=d['version']+1,updated_at=self.h._current['at']);self.h.state.calendar_drafts[ref]=d;self.f.seal(d)
        b.update(version=b['version']+1,updated_at=self.h._current['at'],review=None);return self.review(b)
    def render(self,output):
        b=self.get(output['batch_ref'],active=False);view=self.view(b);rows=[];questions=[];notices={}
        for d in view['events']:
            name=d['facts']['title'] or d['source_unit']['text'].strip()
            status={'recorded':'สร้างแล้ว','cancelled':'ยกเลิกร่างแล้ว'}.get(d['status'])
            if status:rows.append(f"{d['ordinal']}. {name} {status}");continue
            fields=clarification_fields(d)
            if 'time_or_all_day' in fields:
                rows.append(f"{d['ordinal']}. {name}"+('\nวันนัด: '+d['facts']['date_text'] if d['facts']['date_text'] else ''))
                questions.append(timing_question(d['ordinal']))
            elif d['start'] and d['end']:rows.append(f"{d['ordinal']}. {name}\n"+appointment_text(d['start'],d['end'],d.get('all_day',False)))
            else:rows.append(f"{d['ordinal']}. {name}\n"+('ทั้งวัน '+(date_label(d['start']) if d['start'] else 'ยังไม่ระบุวัน') if d.get('all_day') else f"เริ่ม {d['start'] or '?'}\nสิ้นสุด {d['end'] or '?'}"))
            missing=[NAMES.get(x,x) for x in [*fields,*d['issues']] if x!='time_or_all_day']
            if missing:rows.append('ขาด/ต้องตรวจ: '+', '.join(missing))
            child=self.child(d['draft_ref']);review=child.get('review')
            notes=(['ทับนัดเดิม '+str(len(review['conflicts']))+' รายการ'] if review and review['conflicts'] else [])+approximate_notices(child)
            if notes:rows+=notes;notices[d['draft_ref']]=notes
        ready=[d for d in view['events'] if d['status']=='review' and d['start'] and d['end']]
        overlaps=[str(a['ordinal'])+' กับ '+str(b['ordinal']) for i,a in enumerate(ready) for b in ready[i+1:]
            if interval(a)[0]<interval(b)[1] and interval(b)[0]<interval(a)[1]]
        if overlaps:rows.append('เวลาในชุดทับกัน: '+', '.join(overlaps))
        data={'domain':'calendar','tool_result':output,'calendar_batch_view':view,'calendar_notices':notices,'calendar_label':self.f.label,
              'calendar_batch_notices':['เวลาในชุดทับกัน: '+', '.join(overlaps)] if overlaps else []}
        pending=sum(d['status'] not in {'recorded','cancelled'} for d in view['events'])
        message=f'ตรวจสอบนัด {pending} รายการ\nปฏิทิน: '+self.f.label+'\n'+'\n'.join(rows)
        if b['status']=='cancelled':return Result('cancelled',message+'\nยกเลิกเฉพาะร่าง ไม่มีการลบนัดที่สร้างแล้ว',data)
        if b['status']!='review':return Result('clarification',message+('\n'+'\n'.join(questions) if questions else '\nยังไม่สร้างรายการที่เหลือ กรุณาระบุเฉพาะข้อมูลที่ยังขาดหรือเลือกนัดที่จะแก้'),data)
        command='ยืนยันชุดนัด '+b['batch_ref']+' '+b['review']['nonce'];data['confirm_command']=command
        message+='\nถ้าถูกต้อง พิมพ์ “ยืนยันชุดนัด” หรือกดปุ่มเพื่อสร้างทั้งชุดครับ'
        from .conversation_ui import render_actions
        data['line_message']=render_actions(message,[{'type':'postback','label':'ยืนยันชุดนัด','data':f"c2|confirm|{b['batch_ref']}|{b['version']}|{b['review']['nonce']}",'displayText':'ยืนยันชุดนัด'}])
        return Result('preview',message,data)
    def delivered(self,b,latest=False):
        r=b['review'];receipt=self.h.state.receipts.get(r['event']) if r else None
        if not receipt or receipt.result.status!='preview' or receipt.result.data.get('confirm_command')!='ยืนยันชุดนัด '+b['batch_ref']+' '+r['nonce']:return False
        if getattr(self.h,'production',False):
            from .line_api import rendered_message,message_hash
            reply=self.h.state.replies.get(r['event'])
            if not reply or reply.status!='sent' or reply.content_hash!=message_hash(rendered_message(receipt.result)):return False
        if latest:
            row,shown=self.h._presented_result()
            if not row or row['event']!=r['event'] or shown!=receipt.result:return False
        return True
    @staticmethod
    def text_confirmation(text):
        compact=re.sub(r'\s+','',text)
        return bool(re.fullmatch(r'(?:ขอ)?(?:ยืนยัน(?:สร้าง)?(?:ชุดนัด(?:นี้|ล่าสุด)?|นัด(?:ทั้งชุด|ชุดนี้))|สร้างชุดนัดนี้เลย)(?:นะครับ|นะคะ|ครับ|ค่ะ|คะ)?[.!]?',compact))
    @staticmethod
    def refresh_requested(text):
        return bool(re.search(r'นัด|ปฏิทิน',text) and re.search(r'ตรวจ|ทบทวน|ดูร่าง|ขอร่าง',text)
            and re.search(r'เดิม|อีกครั้ง|ใหม่|ก่อน|เมื่อกี้',text) and not intent_guard(text))
    def refresh_unavailable(self):
        return Result('clarification','ยังทำร่างใหม่จากข้อมูลที่เหลือให้ครบไม่ได้ครับ ต้องการตรวจนัดชื่ออะไรหรือวันไหนครับ?',
            {'domain':'calendar','refresh_unavailable':True,'missing_fields':['appointment_target'],'write_authorized':False})
    @staticmethod
    def is_control(text):
        return bool(CalendarBatches.text_confirmation(text) or text.startswith('c2|') or re.match(r'ยืนยันชุดนัด(?:\s|$)',text)
            or re.fullmatch(r'ตรวจ(?:สอบ)?ผล(?:ชุด)?นัด(?:ล่าสุด)?|ดำเนินชุดนัดต่อ',text))
    def confirm_latest_delivered(self):
        # The named domain selects a Calendar set, not an expense or an
        # arbitrary active child. Resolve only a server presentation whose
        # exact rendered content was delivered; never ask the model to bind
        # consent. Errors after a review do not erase that review's identity.
        rows=[]
        for row in self.h.state.history:
            if (row['role']!='assistant' or row['event']==self.h._active_event_id
                or datetime.fromisoformat(row['at'])>=datetime.fromisoformat(self.h._current['at'])):continue
            receipt=self.h.state.receipts.get(row['event'])
            if not receipt or receipt.expires_at<=self.h.clock():continue
            shown=receipt.result;view=shown.data.get('calendar_batch_view')
            # A newer Calendar preparation error is still a change request;
            # do not fall back to an earlier review merely because no draft
            # could be produced. A failed confirmation alone changes no facts.
            if not view and shown.data.get('tool_result',{}).get('domain')!='calendar':continue
            if getattr(self.h,'production',False):
                from .line_api import rendered_message,message_hash
                reply=self.h.state.replies.get(row['event'])
                if (not reply or reply.status!='sent' or reply.content_hash!=message_hash(rendered_message(shown))
                    or self.h.state.event_workbooks.get(row['event'])!=storage_scope(self.h.sheets)):continue
            rows.append((row,shown,view))
        if not rows:return Result('clarification','ยังไม่มีชุดนัดที่ตรวจสอบและส่งสำเร็จ กรุณาตรวจร่างชุดนัดก่อนยืนยัน',{'domain':'calendar'})
        latest=max(datetime.fromisoformat(row['at']) for row,_,_ in rows)
        choices=[value for value in rows if datetime.fromisoformat(value[0]['at'])==latest]
        if len(choices)!=1:return Result('clarification','ต้องการยืนยันชุดนัดใดครับ? ระบุชื่อหรือวันนัดเพื่อให้ตรวจชุดที่ต้องการอีกครั้ง',{'domain':'calendar'})
        row,shown,view=choices[0]
        if not view:return Result('clarification','รายละเอียดนัดล่าสุดยังตรวจไม่ครบ ต้องการตรวจชุดนัดใดอีกครั้งครับ? ระบุข้อมูลที่ต้องการแก้ได้',{'domain':'calendar'})
        try:b=self.get(view['batch_ref']);r=b['review']
        except ValueError:return Result('stale','ชุดนัดเปลี่ยนหรือหมดอายุแล้ว กรุณาตรวจชุดนัดล่าสุดก่อนยืนยัน',{'domain':'calendar'})
        if (shown.status!='preview' or view['status']!='review' or not r
            or r['event']!=row['event'] or view['version']!=b['version']):
            return Result('stale','กรุณาตรวจชุดนัดล่าสุดที่มีข้อมูลครบก่อนยืนยัน',{'domain':'calendar'})
        newer=[d for d in self.h.state.calendar_drafts.values()
            if d.get('batch_ref')!=b['batch_ref'] and d['status'] not in {'recorded','cancelled'}
            and datetime.fromisoformat(d['expires_at'])>self.h.clock()
            and datetime.fromisoformat(d['updated_at'])>=datetime.fromisoformat(r['at'])]
        if newer:return Result('clarification','มีร่างนัดที่ใหม่กว่าหรือยังตรวจไม่ครบ ต้องการตรวจและยืนยันชุดใดครับ? ระบุชื่อหรือวันนัดได้',{'domain':'calendar'})
        return self.confirm(b['batch_ref'],r['nonce'],version=view['version'])
    def confirm(self,ref,nonce,*,version=None,latest=False):
        try:b=deepcopy(self.get(ref));r=b['review']
        except ValueError:return Result('stale','ชุดนัดเปลี่ยนหรือหมดอายุแล้ว')
        if (not self.f.enabled or b['status']!='review' or not r or not hmac.compare_digest(r['nonce'],nonce)
            or version is not None and version!=b['version'] or r['payload_hash']!=members_hash(self.h,b)
            or datetime.fromisoformat(self.h._current['at'])<=datetime.fromisoformat(r['at']) or not self.delivered(b,latest)):
            return Result('stale','กรุณาตรวจสอบชุดนัดล่าสุดก่อนยืนยัน')
        blocked=self.h._blocked_action()
        if blocked:return self.h._uncertain(blocked)
        for child in b['members']:
            d=self.child(child)
            if d['status'] in {'cancelled','recorded'}:continue
            try:conflicts=self.f.adapter.conflicts(*interval(d))
            except Exception:return Result('setup_required','ยังตรวจสอบปฏิทินไม่ได้ จึงยังไม่สร้างชุดนัด')
            if digest(conflicts)!=d['review']['conflicts_hash']:
                b.update(version=b['version']+1,updated_at=self.h._current['at'],review=None)
                return self.render(self.review(b))
        members=[]
        for child in b['members']:
            d=self.child(child)
            if d['status'] in {'cancelled','recorded'}:continue
            key=self.h.settings.owner_id+':calendar:'+digest([d['calendar_id'],child,d['version'],payload_hash(d)])[:32]
            members.append({'ref':child,'version':d['version'],'key':key,'payload':payload(d),'payload_hash':payload_hash(d)})
        frozen={'batch_ref':ref,'batch_version':b['version'],'members':members}
        key=self.h.settings.owner_id+':calendar-batch:'+digest([self.f.calendar_id,frozen])[:32]
        now=self.h.clock();deadline=min(datetime.fromisoformat(self.child(m['ref'])['expires_at']) for m in members)
        self.h.state.actions[key]=Action('calendar_batch',frozen,{'calendar_batch':True,'batch_ref':ref,'calendar_id':self.f.calendar_id,'payload_hash':digest([self.h.settings.owner_id,self.f.calendar_id,frozen])},deadline,now+timedelta(seconds=self.h.settings.webhook_ttl_seconds))
        b.update(status='committing',action_key=key)
        self.h.state.calendar_batches[ref]=b;self.seal(b)
        for m in members:
            d=self.child(m['ref']);d['status']='committing';self.f.seal(d)
        self.h.state.claims[self.h._active_event_id].action_key=key;self.h.state.checkpoint();self.h.phase_hook('calendar_batch_prepared',key)
        return self.resume(key)
    def resume(self,key,*,start_unattempted=True):
        a=self.h.state.actions[key];m=a.metadata
        if a.status=='completed':return deepcopy(a.result)
        if (not self.f.enabled or m.get('calendar_id')!=self.f.calendar_id or m.get('payload_hash')!=digest([self.h.settings.owner_id,self.f.calendar_id,a.payload])):return self.h._uncertain(key)
        b=deepcopy(self.get(m['batch_ref'],active=False))
        a.status='executing';self.h.state.checkpoint()
        processed=0
        for item in a.payload['members']:
            child=self.h.state.actions.get(item['key'])
            if child and child.status=='completed' and child.result and child.result.status=='created':continue
            if child is None and a.deadline<=self.h.clock():return self.stop(a,b,'expired')
            if processed>=2 or child is None and not start_unattempted:
                self.h.state.checkpoint();return self.progress(a,'in_progress')
            processed+=1
            if not child:
                if a.deadline<=self.h.clock():
                    return self.stop(a,b,'expired')
                d=self.child(item['ref'])
                if d['version']!=item['version'] or payload_hash(d)!=item['payload_hash'] or d['status']!='committing':return self.h._uncertain(key)
                details={'calendar_mvp':True,'calendar_batch_parent':key,'calendar_id':self.f.calendar_id,'calendar_label':self.f.label,
                    'calendar_draft_ref':item['ref'],'calendar_draft_version':item['version'],'calendar_payload_hash':item['payload_hash'],
                    'draft_expires_at':d['expires_at'],'origin_id':b['batch_ref'],'last_expires_at':(self.h.clock()+timedelta(seconds=self.h.settings.last_action_ttl_seconds)).isoformat()}
                self.h.state.actions[item['key']]=Action('create_event',deepcopy(item['payload']),details,a.deadline,a.expires_at);self.h.state.checkpoint()
            try:result=self.h._resume(item['key'])
            except Exception:
                a.status='uncertain';self.h.state.checkpoint();raise
            if result.status=='uncertain':a.status='uncertain';self.h.state.checkpoint();return self.progress(a,'uncertain')
            if result.status!='created':
                return self.stop(a,b,'partial')
        b.update(status='recorded',review=None);self.h.state.calendar_batches[b['batch_ref']]=b;self.seal(b)
        a.status='completed';a.result=self.progress(a,'created');self.h.state.checkpoint();self.h.phase_hook('calendar_batch_completed',key);return deepcopy(a.result)
    def stop(self,a,b,status):
        for item in a.payload['members']:
            action=self.h.state.actions.get(item['key']);d=self.child(item['ref'])
            if action and action.status in {'executing','uncertain'}:raise PlanningError('calendar_action_pending')
            if d['status']=='recorded':continue
            d.update(status='needs_information',review=None)
            if action and action.result and action.result.status in {'setup_required','expired'}:d['version']+=1
            self.f.seal(d)
        a.status='completed';a.result=self.progress(a,status);b.update(status='partial',review=None)
        self.h.state.calendar_batches[b['batch_ref']]=b;self.seal(b);self.h.state.checkpoint();return deepcopy(a.result)
    def progress(self,action,status):
        rows=[];created=[];pending=[]
        for item in action.payload['members']:
            child=self.h.state.actions.get(item['key']);done=bool(child and child.status=='completed' and child.result and child.result.status=='created')
            (created if done else pending).append(item['ref']);rows.append(('สร้างแล้ว: ' if done else 'ยังไม่ยืนยันผล: ')+item['payload']['title'])
        complete=status=='created' and not pending
        events=[{'title':v['title'],'start':v['start'],'end':v['end'],'all_day':v.get('all_day') is True}
                for v in (item['payload'] for item in action.payload['members'])] if complete else []
        message=(f'เพิ่มนัดทั้ง {len(created)} รายการเรียบร้อยแล้วครับ\n'+'\n'.join('- '+v['title']+' · '+appointment_text(v['start'],v['end'],v['all_day']) for v in events) if complete else 'ผลการเพิ่มนัด:\n'+'\n'.join(rows))
        if status=='in_progress':message+='\nยังมีรายการที่ยืนยันไว้แต่ยังไม่เริ่มสร้าง พิมพ์ ดำเนินชุดนัดต่อ เพื่อทำต่อภายในอายุร่าง'
        return Result(status,message,{'domain':'calendar','batch_ref':action.payload['batch_ref'],'created_refs':created,'pending_refs':pending,'needs_reconciliation':status=='uncertain',
                                      **({'created_events':events,'calendar_label':self.f.label} if complete else {})})
    def control(self,text):
        if self.text_confirmation(text):
            if self.h._active_event_kind!='text':return Result('stale','ปุ่มชุดนัดไม่ถูกต้อง',{'domain':'calendar'})
            return self.confirm_latest_delivered()
        explicit=re.fullmatch(r'ยืนยันชุดนัด (calset_[a-f0-9]{20}) ([a-f0-9]{16})',text)
        if explicit:return self.confirm(*explicit.groups())
        postback=re.fullmatch(r'c2\|confirm\|(calset_[a-f0-9]{20})\|([1-9]\d*)\|([a-f0-9]{16})',text)
        if postback:
            if self.h._active_event_kind!='postback':return Result('stale','ปุ่มชุดนัดไม่ถูกต้อง')
            ref,version,nonce=postback.groups();return self.confirm(ref,nonce,version=int(version))
        if text.startswith(('c2|','ยืนยันชุดนัด ')):return Result('stale','คำยืนยันชุดนัดไม่ตรงกับร่างล่าสุด')
        if re.fullmatch(r'ตรวจ(?:สอบ)?ผล(?:ชุด)?นัด(?:ล่าสุด)?|ดำเนินชุดนัดต่อ',text):
            pending=[k for k,a in self.h.state.actions.items() if a.metadata.get('calendar_batch') and a.status!='completed']
            if not pending and 'ชุด' not in text:return None
            if len(pending)!=1:return Result('clarification','ไม่พบชุดนัดที่ผลไม่แน่นอนเพียงชุดเดียว')
            advance=text=='ดำเนินชุดนัดต่อ'
            if advance:self.h.state.claims[self.h._active_event_id].action_key=pending[0]
            self.h.state.checkpoint();return self.resume(pending[0],start_unattempted=advance)
        if re.fullmatch(r'ยืนยัน(?:\s*(?:ครับ|ค่ะ|คะ))?',text):
            _,shown=self.h._presented_result();view=shown.data.get('calendar_batch_view') if shown else None
            if view:
                return self.confirm_latest_delivered()
        return None
