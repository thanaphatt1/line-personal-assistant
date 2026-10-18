"""Owner-reviewed saved-record changes; model tools can only find/propose.

One generic Pending envelope and ordinary durable Actions retain the existing
snapshot format. Stable provider IDs and versions stay server-owned.
"""
from .expense_storage import storage_scope
from copy import deepcopy
from datetime import date,datetime,time,timedelta
from decimal import Decimal
import hmac,json,secrets
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from .adapters import Conflict,NotFound
from .catalog import normalized
from .contracts import SpendingSummary,Transaction
from .expense_edits import expense_after
from .calendar_reschedule import rescheduled_times
from .history_context import digest
from .language import BANGKOK,intent_guard
from .model_api import object_schema
from .sheet_metadata import transaction_id
from .state import Action,LastAction,Pending,Result
from .transport import ApiError,NotReady,WriteBlocked

NAMES=frozenset({'find_saved_records','prepare_saved_expense_edit','prepare_calendar_reschedule','review_saved_record_change','check_saved_record_change'})
PREFIX='saved_record_'

class Args(BaseModel):model_config=ConfigDict(extra='forbid',strict=True)
class Find(Args):
    domain:Literal['expense','calendar']
    scope:Literal['last','date_range']
    start_date:str|None=Field(max_length=10)
    end_date:str|None=Field(max_length=10)
    period_text:str|None=Field(max_length=120)
    name:str|None=Field(max_length=200)
    source_refs:list[str]=Field(min_length=1,max_length=16)
class Expense(Args):
    candidate_ref:str=Field(min_length=1,max_length=40)
    item:str|None=Field(max_length=200)
    amount:str|None=Field(max_length=16)
    date:str|None=Field(max_length=10)
    item_text:str|None=Field(max_length=200)
    amount_text:str|None=Field(max_length=120)
    date_text:str|None=Field(max_length=120)
    source_refs:list[str]=Field(min_length=1,max_length=16)
class Calendar(Args):
    candidate_ref:str=Field(min_length=1,max_length=40)
    date:str|None=Field(max_length=10)
    time:str|None=Field(max_length=8)
    end_date:str|None=Field(max_length=10)
    end_time:str|None=Field(max_length=8)
    date_text:str|None=Field(max_length=120)
    time_text:str|None=Field(max_length=120)
    end_date_text:str|None=Field(max_length=120)
    end_time_text:str|None=Field(max_length=120)
    source_refs:list[str]=Field(min_length=1,max_length=16)

def visible_record(record,domain):
    keys=['item','amount','date'] if domain=='expense' else ['title','start','end','all_day']
    return {k:deepcopy(record[k]) for k in keys}

def describe(value,domain):
    if domain=='expense':return f"{value['item']} — {value['amount']} บาท วันที่ {value['date']}"
    if value['all_day']:
        last=(date.fromisoformat(value['end'])-timedelta(days=1)).isoformat()
        return value['title']+' — '+value['start']+(' ถึง '+last if last!=value['start'] else '')+' ทั้งวัน'
    return value['title']+' — '+value['start']+' ถึง '+value['end']

class SavedEdits:
    def __init__(self,host,*,expense_editor=None,calendar_editor=None,expense_enabled=False,calendar_enabled=False):
        if type(expense_enabled) is not bool or type(calendar_enabled) is not bool:raise ValueError('Explicit saved-record flags required')
        self.h=host;self.expense=expense_editor;self.calendar=calendar_editor
        self.flags={'expense':expense_enabled,'calendar':calendar_enabled};self.request_candidates={}
        if expense_enabled and (not expense_editor or expense_editor.sheets is not host.bound_sheets):raise ValueError('Bound saved expense editor required')
        if calendar_enabled and (not calendar_editor or calendar_editor.calendar_id!=host.calendar_flow.calendar_id):raise ValueError('Bound saved Calendar editor required')
    @property
    def enabled(self):return any(self.flags.values())
    def has_pending(self):return bool(self.h.state.pending and self.h.state.pending.kind.startswith(PREFIX))
    def is_control(self,text):return text.startswith(('ยืนยันแก้ ','e1|'))
    def target(self,domain):return storage_scope(self.h.sheets) if domain=='expense' else self.h.calendar_flow.calendar_id
    def editor(self,domain):
        editor=self.expense if domain=='expense' else self.calendar
        if editor is None or domain=='expense' and (editor.sheets is not self.h.bound_sheets or storage_scope(editor.sheets)!=self.target(domain)) or domain=='calendar' and editor.calendar_id!=self.target(domain):
            raise WriteBlocked('Saved-record target/configuration is unavailable')
        return editor
    def pending(self):
        if not self.has_pending():raise ValueError('saved_edit_no_pending')
        p=self.h.state.pending;v=p.payload;copy={k:x for k,x in v.items() if k!='seal'}
        if (p.expires_at<=self.h.clock() or v.get('owner')!=self.h.settings.owner_id or v.get('ref')!=p.token
            or v.get('domain') not in {'expense','calendar'}
            or v.get('workbook')!=storage_scope(self.h.sheets) or v.get('target')!=self.target(v.get('domain'))
            or v.get('format')!='saved-record-change-v1' or v.get('seal')!=digest(copy)):
            raise ValueError('saved_edit_stale_or_foreign')
        return p
    def store(self,kind,value,*,origin=None):
        value=deepcopy(value);value.pop('seal',None);value['seal']=digest(value)
        at=datetime.fromisoformat(self.h._current['at'])
        self.h.state.pending=Pending(PREFIX+kind,value['ref'],origin or self.h._active_event_id,int(at.timestamp()*1000),
            self.h.clock()+timedelta(seconds=self.h.settings.pending_ttl_seconds),value)
        self.h.state.checkpoint();return self.h.state.pending
    def context(self):
        if not self.enabled and not self.has_pending():return {}
        value={'expense_edit_enabled':self.flags['expense'],'calendar_reschedule_enabled':self.flags['calendar'],'confirmation':'Only a new signed owner confirmation of a delivered unchanged before/after review can write. Tools never confirm.'}
        try:
            p=self.pending();v=p.payload
            value.update(domain=v['domain'],version=v['version'],stage=p.kind,review_ref=v['ref'])
            value['candidates']=[{'candidate_ref':r['ref'],'ordinal':i+1,**visible_record(r['record'],v['domain'])} for i,r in enumerate(v.get('candidates',[]))]
            if v.get('before'):value.update(before=visible_record(v['before'],v['domain']),after=deepcopy(v['after']))
        except ValueError:pass
        return {'saved_record_edits':value}
    def definitions(self):
        if not self.enabled:
            if any(a.metadata.get('saved_record_edit') and a.status in {'executing','uncertain'} for a in self.h.state.actions.values()):
                return [{'type':'function','name':'check_saved_record_change','strict':True,'description':'Read-only positive reconciliation of an existing saved-record action. Never starts or retries a write.','parameters':object_schema({})}]
            return []
        nullable={'type':['string','null']};refs={'type':'array','items':{'type':'string'},'minItems':1,'maxItems':16}
        result=[{'type':'function','name':'find_saved_records','strict':True,'description':'For editing ALREADY SAVED expense amount/name/date or rescheduling a SAVED appointment. Read only; never creates or changes a draft or saved record. Use scope=last for a recent confirmed record, with null dates/period; otherwise use owner-cited explicit ISO date_range (expense <=367dates, Calendar <=7dates). name optionally selects an exact existing item/title; never guesses a row/ID. Multiple matches require owner choice. Use prepare_expenses/prepare_calendar_event only for unsaved drafts/new entries, not this operation.',
            'parameters':object_schema({'domain':{'type':'string','enum':[k for k,v in self.flags.items() if v]},'scope':{'type':'string','enum':['last','date_range']},'start_date':nullable,'end_date':nullable,'period_text':nullable,'name':nullable,'source_refs':refs})}]
        if self.flags['expense']:
            result.append({'type':'function','name':'prepare_saved_expense_edit','strict':True,'description':'Propose one before/after saved-expense review, NEVER writes. candidate_ref only from a unique current find result or a delivered owner-choice list. Null fields retain certified values. Changes require literal owner item_text/amount_text/date_text citations; do not invent a new catalog mapping. item is an exact existing catalog label, amount is a positive decimal string, date is explicit YYYY-MM-DD. Never use a row number, fabricate an ID or pass confirmation.',
                'parameters':object_schema({'candidate_ref':{'type':'string'},**{k:nullable for k in ['item','amount','date','item_text','amount_text','date_text']},'source_refs':refs})})
        if self.flags['calendar']:
            result.append({'type':'function','name':'prepare_calendar_reschedule','strict':True,'description':'Propose a SAVED appointment before/after review, NEVER writes. Use owned candidate_ref; multiple matches first require owner choice. date/end_date are owner-interpreted YYYY-MM-DD, time/end_time local Bangkok HH:MM; cite each changed field with literal owner *_text. Null retains start components. When moving only the start or keeping duration, end_date/end_time/end_date_text/end_time_text MUST ALL BE NULL: server calculates the end. Only an explicitly stated new end date/clock permits an end field, never a duration-preserving phrase. All-day end_date is the last inclusive date; never give clock fields for all-day events. Preserve mode/title/reminders and other data. No guests, recurrence, delete/cancel or title changes. No model confirmation.',
                'parameters':object_schema({'candidate_ref':{'type':'string'},**{k:nullable for k in ['date','time','end_date','end_time','date_text','time_text','end_date_text','end_time_text']},'source_refs':refs})})
        result.extend([{'type':'function','name':name,'strict':True,'description':description,'parameters':object_schema({})} for name,description in [
            ('review_saved_record_change','Show/refresh the current saved-record selection or before/after review. Read only and not confirmation; a refreshed review revokes previous confirmation.'),
            ('check_saved_record_change','Read-only status/positive reconciliation of an already confirmed saved-record change. Never issues or retries a write.')]])
        return result
    def latest_ids(self,domain):
        last=self.h.state.last
        if not last or last.expires_at<=self.h.clock():return []
        if domain=='expense' and last.kind=='saved_expense':return list(last.refs)
        if domain=='calendar' and last.kind=='event':
            ids=list(last.refs)
            for identifier in ids:
                if not any(a.status=='completed' and a.result and a.result.data.get('id')==identifier and
                    (a.metadata.get('calendar_id')==self.target(domain) or a.metadata.get('saved_record_edit') and a.payload.get('domain')==domain and a.metadata.get('target')==self.target(domain)) for a in self.h.state.actions.values()):return []
            return ids
        if domain=='expense' and last.kind=='transactions':
            found=[(k,a) for k,a in self.h.state.actions.items() if a.status=='completed' and a.kind in {'add_transactions','catalog_expense'} and a.result and tuple(a.result.data.get('refs',()))==tuple(last.refs)]
            if len(found)==1:
                key,action=found[0];return [transaction_id(key,i) for i in range(len(action.payload['entries']))]
        return []
    def find(self,args):
        p=Find.model_validate(args)
        if not self.flags[p.domain]:raise WriteBlocked('Saved record feature is disabled')
        if self.has_pending():self.pending()
        elif self.h.state.pending:raise ValueError('another_pending_workflow')
        sources=self.h._owner_sources(p.source_refs)
        if self.h._current['source_ref'] not in p.source_refs:raise ValueError('saved_edit_current_owner_source_required')
        editor=self.editor(p.domain)
        if p.scope=='last':
            if p.start_date or p.end_date:raise ValueError('saved_edit_last_does_not_take_dates')
            ids=self.latest_ids(p.domain)
            if not ids:return self.output('saved_edit_unavailable','ยังระบุรายการที่บันทึกล่าสุดสำหรับการแก้ไขนี้ไม่ได้ครับ ช่วยระบุช่วงวันและชื่อรายการที่จะเลือกก่อน',p.domain)
            records=[editor.get(i) for i in ids];extra=0
        else:
            if not p.period_text or not any(p.period_text in r['text'] for r in sources):raise ValueError('saved_edit_date_source_required')
            period=SpendingSummary(start_date=p.start_date,end_date=p.end_date);a,b=period.start_date,period.end_date
            if not 1900<=a.year<=b.year<=2100 or p.domain=='calendar' and (b-a).days>6:raise ValueError('saved_edit_date_bound')
            result=editor.records(a,b) if p.domain=='expense' else editor.records(datetime.combine(a,time.min,BANGKOK),datetime.combine(b+timedelta(days=1),time.min,BANGKOK))
            records=result['records'];extra=result.get('unidentified_count',0)+result.get('unsupported_count',0)
        if p.name:
            if p.domain=='expense':
                tax=self.h._taxonomy();identity=tax.catalog.identity(p.name)
                if not identity.canonical:raise ValueError('saved_edit_item_identity_unknown')
                records=[r for r in records if tax.catalog.identity(r['item']).canonical==identity.canonical]
            else:records=[r for r in records if normalized(r['title'])==normalized(p.name)]
        ref='se_'+digest([self.h.settings.owner_id,storage_scope(self.h.sheets),self.target(p.domain),self.h._active_event_id])[:20]
        candidates=[{'ref':'sr_'+digest([ref,r['id'],r['version']])[:20],'record':r} for r in records[:10]]
        value={'format':'saved-record-change-v1','ref':ref,'owner':self.h.settings.owner_id,'workbook':storage_scope(self.h.sheets),
            'target':self.target(p.domain),'domain':p.domain,'version':1,'at':self.h._current['at'],'source_refs':list(p.source_refs),
            'candidates':candidates,'count':len(records),'uneditable_count':extra,'query':p.model_dump(mode='json')}
        self.store('selection',value)
        self.request_candidates={r['ref']:r for r in candidates} if len(records)==1 and not extra else {}
        return self.selection_output(value,continue_model=len(records)==1 and not extra)
    def output(self,status,message,domain,**data):return {'saved_record_edit':True,'read_only':True,'write_authorized':False,'status':status,'domain':domain,'message':message,**data}
    def selection_output(self,v,*,continue_model=False):
        domain=v['domain'];rows=v['candidates']
        message=('พบรายการที่ตรงเพียงรายการเดียวครับ เตรียมก่อน–หลังจากข้อมูลที่ขอแก้ได้ ยังไม่ได้เปลี่ยนข้อมูลใด\n' if continue_model else 'กรุณาเลือกรายการที่จะ'+('แก้ไข' if domain=='expense' else 'เลื่อน')+'ครับ ยังไม่ได้เปลี่ยนข้อมูลใด\n')
        message+='\n'.join(f"{i+1}. "+describe(r['record'],domain) for i,r in enumerate(rows)) if rows else 'ไม่พบรายการที่แก้ไขได้ตามขอบเขตนี้ครับ ยังไม่ได้เปลี่ยนข้อมูลใด'
        if v['uneditable_count']:message+=f"\nอีก {v['uneditable_count']} รายการยังแก้ผ่านบอทไม่ได้ เพราะไม่มีรหัสหรือเป็นชนิดที่ไม่รองรับ"
        if v['count']>len(rows):message+='\nแสดง 10 รายการแรก กรุณาจำกัดช่วงวันหรือชื่อให้ชัดขึ้น'
        return self.output('saved_edit_candidates',message,domain,continue_model=continue_model,owner_choice_required=not continue_model,selection_ref=v['ref'],
            candidates=[{'candidate_ref':r['ref'],'ordinal':i+1,**visible_record(r['record'],domain)} for i,r in enumerate(rows)],count=v['count'])
    def selected(self,ref,domain):
        p=self.pending();v=p.payload
        if v['domain']!=domain or not self.flags[domain]:raise ValueError('saved_edit_domain_disabled_or_mismatch')
        candidate=next((r for r in v.get('candidates',[]) if r['ref']==ref),None)
        if candidate is None:raise ValueError('saved_edit_unknown_candidate')
        if ref not in self.request_candidates:
            _,shown=self.h._presented_result()
            review=shown.data.get('saved_edit_review',{}) if shown else {}
            reviewed=review=={'review_ref':v['ref'],'review_version':v['version'],'review_hash':v.get('review_hash')}
            if not shown or shown.data.get('saved_edit_selection')!=v['ref'] and not reviewed:raise ValueError('saved_edit_selection_not_delivered_latest')
        return p,candidate['record']
    def cited_changes(self,args,fields,selection):
        refs=args.source_refs;allowed=set(selection.payload['source_refs'])|{self.h._current['source_ref']}
        if not set(refs)<=allowed or self.h._current['source_ref'] not in refs:raise ValueError('saved_edit_unrelated_owner_source')
        sources=self.h._owner_sources(refs);changes={}
        for field in fields:
            value=getattr(args,field);text=getattr(args,field+'_text')
            if value is None:
                if text is not None:raise ValueError('saved_edit_unbound_field_source')
                continue
            if not text or not any(text in r['text'] for r in sources):raise ValueError('saved_edit_literal_field_source_required')
            if field in {'date','end_date'}:
                from .history_dates import cues
                proposed=date.fromisoformat(value);proof=False
                for source in sources:
                    if text not in source['text']:continue
                    parts,_,issue=cues(text,datetime.fromisoformat(source['at']))
                    if not issue and 'day' in parts and all(getattr(proposed,k)==x for k,x in parts.items()):proof=True
                    if selection.payload['domain']=='calendar':
                        from .calendar_dates import event_date
                        try:proof=proof or event_date(text,datetime.fromisoformat(source['at']))==proposed
                        except ValueError:pass
                if not proof:raise ValueError('saved_edit_date_does_not_match_owner_source')
            if field in {'time','end_time'}:
                from .calendar_dates import event_time
                if event_time(text)!=time.fromisoformat(value):raise ValueError('saved_edit_clock_does_not_match_owner_source')
            changes[field]=value
        if not changes:raise ValueError('saved_edit_no_change')
        return changes,sources
    def prepare(self,args,domain):
        parsed=(Expense if domain=='expense' else Calendar).model_validate(args)
        p,before=self.selected(parsed.candidate_ref,domain);v=deepcopy(p.payload)
        if p.kind==PREFIX+'review':
            v.pop('nonce',None);self.store('stale',v,origin=p.origin_event_id)
        fields=['item','amount','date'] if domain=='expense' else ['date','time','end_date','end_time']
        # Redundant certified values are not new facts. Some model responses
        # echo an unchanged start date while changing only its clock.
        retained=v['after'] if p.kind==PREFIX+'review' and v.get('before',{}).get('id')==before['id'] else before
        if domain=='expense':unchanged={k:retained[k] for k in fields}
        elif retained['all_day']:
            unchanged={'date':retained['start'],'end_date':(date.fromisoformat(retained['end'])-timedelta(days=1)).isoformat()}
        else:
            start=datetime.fromisoformat(retained['start']).astimezone(BANGKOK);end=datetime.fromisoformat(retained['end']).astimezone(BANGKOK)
            unchanged={'date':start.date().isoformat(),'time':start.time().isoformat(),'end_date':end.date().isoformat(),'end_time':end.time().isoformat()}
        for field,known in unchanged.items():
            value=getattr(parsed,field)
            same=value==known
            if value is not None and field in {'time','end_time'}:same=time.fromisoformat(value)==time.fromisoformat(known)
            if value is not None and field=='amount':same=Decimal(value)==Decimal(known)
            if same:setattr(parsed,field,None);setattr(parsed,field+'_text',None)
        changes,sources=self.cited_changes(parsed,fields,p)
        if domain=='expense':
            tax=self.h._taxonomy()
            if 'item' in changes:
                identity=tax.catalog.identity(changes['item']);literal=tax.catalog.identity(parsed.item_text)
                if not identity.canonical or identity.canonical!=literal.canonical:raise ValueError('saved_edit_new_name_requires_existing_literal_identity')
                changes['item']=identity.canonical
            if 'amount' in changes:
                from .expense_sources import roles
                from .expense_evidence import SELECTOR
                from .catalog_names import spans
                valid=False
                for source in sources:
                    names=[m.span() for name in [before['item'],changes.get('item')] if name for m in spans(source['text'],name,tax.catalog)]
                    facts=roles(source['text'],datetime.fromisoformat(source['at']),names+[m.span() for m in SELECTOR.finditer(source['text'])])
                    offset=0
                    while True:
                        start=source['text'].find(parsed.amount_text,offset)
                        if start<0:break
                        end=start+len(parsed.amount_text);offset=end
                        cited=[m for m in facts['money'] if start<=m.start and m.end<=end]
                        if len(cited)==1 and cited[0].value is not None and Decimal(cited[0].value)==Decimal(changes['amount']):valid=True
                if not valid:raise ValueError('saved_edit_amount_not_owner_money')
            if p.kind==PREFIX+'review' and v.get('before',{}).get('id')==before['id']:
                changes={**v['changes'],**changes}
                if changes.get('item') and tax.catalog.identity(changes['item']).canonical!=changes['item']:raise ValueError('saved_edit_catalog_changed')
            after=expense_after(before,changes);catalog_hash=digest([[r.item,r.category,r.subcategory] for r in tax.rows])
        else:
            if p.kind==PREFIX+'review' and v.get('before',{}).get('id')==before['id']:changes={**v['changes'],**changes}
            after=rescheduled_times(before,changes);catalog_hash=None
        current=self.editor(domain).get(before['id'])
        if current['version']!=before['version']:raise Conflict('Selected saved record changed; find and review it again')
        v.update(before=current,after=after,changes=changes,catalog_hash=catalog_hash,source_refs=list(dict.fromkeys(v['source_refs']+parsed.source_refs)))
        return self.new_review(v,origin=p.origin_event_id)
    def new_review(self,v,*,origin=None):
        v=deepcopy(v);v['version']+=1;v['at']=self.h._current['at'];v['nonce']=secrets.token_hex(8)
        for candidate in v.get('candidates',[]):
            if candidate['record']['id']==v['before']['id']:candidate['record']=deepcopy(v['before'])
        v['review_hash']=digest({k:v[k] for k in ['owner','workbook','target','domain','ref','version','before','after','changes','catalog_hash']})
        self.store('review',v,origin=origin)
        return self.review_output(v)
    def review_output(self,v):
        message='ตรวจรายการก่อน–หลังครับ ยังไม่ได้แก้ข้อมูล\nก่อน: '+describe(v['before'],v['domain'])+'\nหลัง: '+describe(v['after'],v['domain'])
        message+='\nถ้าถูกต้อง พิมพ์ “ยืนยัน” หรือ “ยืนยันแก้ '+v['ref']+' '+v['nonce']+'” เพื่อแก้รายการนี้ครับ'
        return self.output('saved_edit_review',message,v['domain'],review_ref=v['ref'],review_version=v['version'],review_hash=v['review_hash'])
    def result(self,output):
        status='preview' if output['status']=='saved_edit_review' else 'clarification' if output['status']=='saved_edit_candidates' else 'answer'
        data={'domain':output['domain'],'saved_record_edit':True,'tool_result':deepcopy(output)}
        if output['status']=='saved_edit_candidates':data['saved_edit_selection']=output['selection_ref']
        if output['status']=='saved_edit_review':data['saved_edit_review']={k:output[k] for k in ['review_ref','review_version','review_hash']}
        return Result(status,output['message'],data)
    def refresh(self):
        p=self.pending();v=deepcopy(p.payload)
        if p.kind==PREFIX+'selection':return self.selection_output(v)
        if not self.flags[v['domain']]:raise WriteBlocked('Saved-record changes are disabled')
        current=self.editor(v['domain']).get(v['before']['id'])
        v['before']=current;v['after']=expense_after(current,v['changes']) if v['domain']=='expense' else rescheduled_times(current,v['changes'])
        if v['domain']=='expense':v['catalog_hash']=digest([[r.item,r.category,r.subcategory] for r in self.h._taxonomy().rows])
        return self.new_review(v,origin=p.origin_event_id)
    def tool(self,name,args):
        domain='calendar' if name=='prepare_calendar_reschedule' else args.get('domain','expense')
        if self.has_pending():domain=self.h.state.pending.payload.get('domain',domain)
        try:
            if name=='find_saved_records':return self.find(args)
            if name=='prepare_saved_expense_edit':return self.prepare(args,'expense')
            if name=='prepare_calendar_reschedule':return self.prepare(args,'calendar')
            if args:raise ValueError('saved_edit_no_status_arguments')
            if name=='review_saved_record_change':return self.refresh()
            if name=='check_saved_record_change':
                key=next((k for k,a in self.h.state.actions.items() if a.metadata.get('saved_record_edit') and a.status!='completed'),None)
                return self.output('saved_edit_status',self.resume(key).message,self.h.state.actions[key].payload['domain']) if key else self.output('saved_edit_status','ไม่มีการแก้ไขที่รอตรวจผลครับ','expense')
        except (ValueError,Conflict,NotFound,NotReady,ApiError,WriteBlocked):
            return self.output('saved_edit_unavailable','ยังไม่ได้แก้ข้อมูลครับ รายการ/ข้อมูลต้นทางอาจเปลี่ยนหรือยังไม่ชัดเจน กรุณาค้นหาและตรวจรายการล่าสุดอีกครั้ง',domain if domain in {'expense','calendar'} else 'expense')
        raise ValueError('unknown_saved_edit_tool')

    def control(self,text):
        exact=None
        if text.startswith('ยืนยันแก้ '):
            parts=text.split()
            if len(parts)==3:exact=(parts[1],parts[2])
            else:return Result('stale','คำยืนยันแก้ไขไม่ตรงกับรายการล่าสุดครับ ยังไม่ได้แก้ข้อมูล')
        elif text.startswith('e1|'):
            parts=text.split('|')
            if len(parts)==4 and parts[1]=='confirm':exact=(parts[2],parts[3])
            else:return Result('stale','ปุ่มแก้ไขไม่ตรงกับรายการล่าสุดครับ ยังไม่ได้แก้ข้อมูล')
        elif text not in {'ยืนยัน','ยืนยันครับ','ยืนยันค่ะ','ยืนยันคะ'}:return None
        if not self.has_pending():return Result('stale','ไม่พบรายการแก้ไขที่รอยืนยันครับ') if exact else None
        try:p=self.pending()
        except ValueError:return Result('stale','รายการแก้ไขหมดอายุหรือเปลี่ยนแล้วครับ ยังไม่ได้แก้ข้อมูล')
        if p.kind!=PREFIX+'review':return Result('clarification','ต้องเลือกและตรวจรายการก่อน–หลังให้ครบก่อนยืนยันครับ ยังไม่ได้แก้ข้อมูล')
        v=p.payload;row,shown=self.h._presented_result()
        wanted={'review_ref':v['ref'],'review_version':v['version'],'review_hash':v['review_hash']}
        if not row or not shown or shown.data.get('saved_edit_review')!=wanted:
            return Result('stale','ต้องแสดงรายการก่อน–หลังล่าสุดให้ตรวจอีกครั้งครับ ยังไม่ได้แก้ข้อมูล')
        current=datetime.fromisoformat(self.h._current['at'])
        if current<=datetime.fromisoformat(v['at']) or current<=datetime.fromisoformat(row['at']):
            return Result('stale','คำยืนยันเกิดก่อนรายการก่อน–หลังล่าสุดครับ ยังไม่ได้แก้ข้อมูล')
        if exact and (exact[0]!=v['ref'] or not hmac.compare_digest(exact[1],v['nonce'])):
            return Result('stale','รหัสยืนยันแก้ไขหมดอายุหรือเปลี่ยนแล้วครับ ยังไม่ได้แก้ข้อมูล')
        if not self.flags[v['domain']]:return Result('setup_required','ยังไม่ได้เปิดการแก้รายการที่บันทึกแล้วครับ ยังไม่ได้แก้ข้อมูล')
        blocked=self.h._blocked_action()
        if blocked:return self.resume(blocked) if self.h.state.actions[blocked].metadata.get('saved_record_edit') else self.h._uncertain(blocked)
        if v['domain']=='expense' and digest([[r.item,r.category,r.subcategory] for r in self.h._taxonomy().rows])!=v['catalog_hash']:
            v=deepcopy(v);v.pop('nonce',None);self.store('stale',v,origin=p.origin_event_id)
            return Result('stale','Database เปลี่ยนแล้วครับ กรุณาตรวจรายการก่อน–หลังใหม่ ยังไม่ได้แก้ข้อมูล')
        payload={'format':'saved-record-change-v1','domain':v['domain'],'id':v['before']['id'],'before':deepcopy(v['before']),
            'after':deepcopy(v['after']),'changes':deepcopy(v['changes']),'catalog_hash':v['catalog_hash']}
        key=self.h.settings.owner_id+':savededit:'+digest([v['workbook'],v['target'],v['ref'],v['version']])
        now=self.h.clock()
        if key not in self.h.state.actions:
            self.h.state.actions[key]=Action('edit_saved_expense' if v['domain']=='expense' else 'reschedule_saved_event',payload,
                {'saved_record_edit':True,'owner':self.h.settings.owner_id,'workbook':v['workbook'],'target':v['target'],
                 'payload_hash':digest(payload),'review_ref':v['ref'],'review_version':v['version'],'review_hash':v['review_hash'],
                 'confirmation_event':self.h._active_event_id,'confirmation_source_at':self.h._current['at'],'request_fence':None},
                p.expires_at,now+timedelta(seconds=self.h.settings.webhook_ttl_seconds))
        self.h.state.claims[self.h._active_event_id].action_key=key;self.h.state.checkpoint()
        return self.resume(key)

    def complete(self,key,receipt):
        action=self.h.state.actions[key];domain=action.payload['domain']
        message=('ตรวจแล้ว รายการตรงกับค่าที่คุณยืนยันครับ' if receipt.get('proof')=='observed_desired_state' else 'แก้ไขรายจ่ายแล้วครับ' if domain=='expense' else 'เลื่อนนัดแล้วครับ')
        message+='\nก่อน: '+describe(receipt['before'],domain)+'\nหลัง: '+describe(receipt['after'],domain)
        result=Result('updated',message,{'domain':domain,'saved_record_edit':True,'id':receipt['id'],'before':receipt['before'],
            'after':receipt['after'],'verification':receipt['proof'],'action_ref':key})
        action.status='completed';action.result=deepcopy(result)
        if self.has_pending() and self.h.state.pending.token==action.metadata['review_ref']:self.h.state.pending=None
        self.h.state.last=LastAction('saved_expense' if domain=='expense' else 'event',(receipt['id'],),self.h.clock()+timedelta(seconds=self.h.settings.last_action_ttl_seconds))
        self.h.state.checkpoint();return result

    def resume(self,key):
        action=self.h.state.actions[key];m=action.metadata;payload=action.payload;domain=payload.get('domain')
        if (not m.get('saved_record_edit') or m.get('owner')!=self.h.settings.owner_id or m.get('workbook')!=storage_scope(self.h.sheets)
            or domain not in {'expense','calendar'} or m.get('target')!=self.target(domain) or m.get('payload_hash')!=digest(payload)):
            return Result('uncertain','รายการแก้ไขยังผูกกับปลายทางเดิมครับ จึงหยุดไว้จนกว่าจะตรวจสอบข้อมูลได้',{'needs_reconciliation':True})
        if action.status=='completed':return deepcopy(action.result)
        try:editor=self.editor(domain)
        except WriteBlocked:return self.h._uncertain(key)
        if action.status in {'executing','uncertain'}:
            try:proof=editor.reconcile(key,payload)
            except Exception:proof=None
            if proof and proof.outcome=='committed':return self.complete(key,proof.receipt)
            action.status='uncertain';self.h.state.checkpoint();return self.h._uncertain(key)
        if action.deadline<=self.h.clock() or not self.flags[domain]:
            action.status='completed';action.result=Result('expired' if action.deadline<=self.h.clock() else 'setup_required','รายการแก้ไขหมดอายุหรือถูกปิดใช้งานครับ ยังไม่ได้แก้ข้อมูล')
            self.h.state.checkpoint();return deepcopy(action.result)
        action.status='executing';self.h.state.checkpoint();self.h.phase_hook('saved_edit_executing',key)
        def bind(body,proof):
            if proof['version']!=payload['before']['version']:raise Conflict('Saved record version changed')
            action.metadata['request_fence']={'proof':deepcopy(proof),'body_hash':digest(body),'at':self.h.clock().isoformat()}
        try:receipt=editor.apply(payload,key,bind_request=bind)
        except (Conflict,NotFound,WriteBlocked,NotReady) as error:
            action.status='completed';action.result=Result('conflict' if isinstance(error,Conflict) else 'not_found' if isinstance(error,NotFound) else 'setup_required',
                'รายการหรือสิทธิ์เปลี่ยนแล้วครับ ยังไม่ได้แก้ข้อมูล กรุณาค้นหาและตรวจรายการก่อน–หลังใหม่')
            if self.has_pending() and self.h.state.pending.token==m['review_ref']:
                v=deepcopy(self.h.state.pending.payload);v.pop('nonce',None);self.store('stale',v)
            self.h.state.checkpoint();return deepcopy(action.result)
        except Exception:
            action.status='uncertain';self.h.state.checkpoint();return self.h._uncertain(key)
        self.h.phase_hook('saved_edit_effect_returned',key)
        return self.complete(key,receipt)
