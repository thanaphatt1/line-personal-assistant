"""Bounded category proposals. Query consent and permanent mapping are distinct."""
from .expense_storage import storage_scope
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal
from urllib.parse import quote
import hmac,secrets
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from .adapters import Conflict,Reconciliation
from .catalog import normalized
from .history_context import digest
from .model_api import object_schema
from .state import Action,Pending,Result
from .transport import ApiError,NotReady,OutcomeUnknown,WriteBlocked,LiveDisabled

NAMES={'propose_expense_categories','review_provisional_categories','check_provisional_mapping'}
KIND='provisional_category_review'
def catalog_hash(tax):return digest([[r.item,r.category,r.subcategory] for r in tax.rows])
def mapping_body(target,rows):
    if (not isinstance(rows,list) or not 1<=len(rows)<=10 or any(set(r)!={'item','category','subcategory'}
        or any(not isinstance(r[k],str) or len(r[k])>200 for k in r) or not r['item'] or not r['category'] for r in rows)):
        raise ValueError('bounded_mapping_rows_required')
    return {'requests':[{'appendCells':{'sheetId':target.database_sheet_id,'rows':[{'values':[{'userEnteredValue':{'stringValue':r[k]}} for k in ['item','category','subcategory']]} for r in rows],'fields':'userEnteredValue'}}]}

class Strict(BaseModel):model_config=ConfigDict(extra='forbid',strict=True)
class Pair(Strict):
    category:str=Field(min_length=1,max_length=200)
    subcategory:str=Field(max_length=200)
class Proposal(Strict):
    item:str=Field(min_length=1,max_length=200)
    status:Literal['proposed','ambiguous']
    category:str|None=Field(max_length=200)
    subcategory:str|None=Field(max_length=200)
    candidates:list[Pair]=Field(max_length=3)
class Propose(Strict):
    query_ref:str=Field(min_length=1,max_length=40)
    proposals:list[Proposal]=Field(min_length=1,max_length=10)

class MappingEditor:
    def __init__(self,sheets,*,enabled=False,single_writer_reviewed=False):
        self.sheets=sheets;self.enabled=enabled;self.policy=single_writer_reviewed
        if enabled and not single_writer_reviewed:raise ValueError('Reviewed Database single-writer policy required')
    def reconcile(self,key,payload):
        try:
            tax=self.sheets.taxonomy()
            for desired in payload['rows']:
                matches=[r for r in tax.rows if normalized(r.item)==normalized(desired['item'])]
                if len(matches)!=1 or [matches[0].item,matches[0].category,matches[0].subcategory]!=[desired[k] for k in ['item','category','subcategory']]:return Reconciliation('unknown')
            return Reconciliation('committed',{'rows':deepcopy(payload['rows']),'proof':'observed_mapping_state'})
        except Exception:return Reconciliation('unknown')
    def apply(self,payload,key,*,bind):
        s=self.sheets;t=s.target
        if not self.enabled or not self.policy or not t.writes_enabled or not t.input_access_verified:raise WriteBlocked('Mapping changes are disabled')
        tax=s.taxonomy()
        if catalog_hash(tax)!=payload['catalog_hash']:raise Conflict('Catalog changed after review')
        pairs={(r.category,r.subcategory) for r in tax.rows if r.category}
        for r in payload['rows']:
            if (r['category'],r['subcategory']) not in pairs or tax.catalog.identity(r['item']).canonical or tax.catalog.match(r['item']).suggestions:raise Conflict('Mapping requires fresh explicit resolution')
        tab=s.metadata()[t.database_tab];grid=tab['properties']['gridProperties']
        if grid['rowCount']>t.max_rows or grid['columnCount']!=26 or any(not p.get('warningOnly',False) for p in tab.get('protectedRanges',[])):raise WriteBlocked('Database capacity/protection needs review')
        raw=s.values(t.database_tab,f"A1:Z{grid['rowCount']}",formulas=True)
        last=max((i+1 for i,row in enumerate(raw) if any(v not in ('',None) for v in row)),default=1)
        if last+len(payload['rows'])>grid['rowCount']:raise WriteBlocked('Database append capacity unavailable')
        body=mapping_body(t,payload['rows']);bind(body);s.effect_guard()
        try:response=s.client.request('POST',s.base+':batchUpdate',body=body)
        except ApiError as e:
            if e.status in {401,403}:raise WriteBlocked('Existing Database write permission unavailable') from None
            raise
        if response.get('spreadsheetId')!=t.spreadsheet_id or len(response.get('replies',[]))!=1:raise OutcomeUnknown('Mapping acknowledgement uncertain')
        proof=self.reconcile(key,payload)
        if proof.outcome!='committed':raise OutcomeUnknown('Mapping readback uncertain')
        return {**proof.receipt,'proof':'ack_and_mapping_readback'}

class ProvisionalCategories:
    def __init__(self,host,*,enabled=False,mapping_enabled=False,single_writer_reviewed=False):
        self.h=host;self.enabled=enabled;self.mapping_enabled=mapping_enabled;self.current_basis=None
        if mapping_enabled and not enabled:raise ValueError('Mapping writes require category proposal feature')
        from .expense_storage import mapping_editor
        self.editor=mapping_editor(host.bound_sheets,enabled=mapping_enabled,single_writer_reviewed=single_writer_reviewed)
    def has_pending(self):return bool(self.h.state.pending and self.h.state.pending.kind==KIND)
    def is_control(self,text):return text in {'รวมข้อเสนอครั้งนี้','บันทึกหมวดถาวร'} or text.startswith(('รวมข้อเสนอครั้งนี้ ','บันทึกหมวด '))
    def pairs(self):return sorted({(r.category,r.subcategory) for r in self.h._taxonomy().rows if r.category})
    def eligible(self,output):
        return (self.enabled and output.get('error_code')=='mapping_incomplete' and output.get('input_read_complete') is True
            and not output.get('scope',{}).get('calendar_basis') and output.get('scope',{}).get('group_by','none')=='none'
            and output.get('scope',{}).get('filter') and next(iter(output['scope']['filter'])) in {'category','subcategory'}
            and (not self.h.state.pending or self.has_pending()))
    def make_basis(self,args,output):
        scope=output['scope'];q={**deepcopy(args),'start_date':scope['start'],'end_date':scope['end'],'date_mode':'range','relative_offset':None}
        return {'ref':'pq_'+digest([self.h.settings.owner_id,output])[:20],'args':q,'output':deepcopy(output),'catalog_hash':catalog_hash(self.h._taxonomy())}
    def observe(self,args,output):
        if not self.eligible(output):return output
        self.current_basis=self.make_basis(args,output)
        return {**output,'provisional_query_ref':self.current_basis['ref'],'category_proposals_available':True}
    def basis(self,ref=None):
        if self.current_basis and (ref is None or self.current_basis['ref']==ref):return deepcopy(self.current_basis)
        if self.has_pending():
            value=self.pending().payload
            if ref is None or value['basis']['ref']==ref:return deepcopy(value['basis'])
        _,shown=self.h._presented_result()
        basis=shown.data.get('provisional_basis') if shown else None
        if basis and (ref is None or basis['ref']==ref):return deepcopy(basis)
        raise ValueError('provisional_query_not_delivered_or_current')
    def pending(self):
        if not self.has_pending():raise ValueError('no_provisional_review')
        p=self.h.state.pending;v=p.payload
        if (p.expires_at<=self.h.clock() or v.get('owner')!=self.h.settings.owner_id or v.get('workbook')!=storage_scope(self.h.sheets)
            or v.get('ref')!=p.token or v.get('seal')!=digest({k:x for k,x in v.items() if k!='seal'})):raise ValueError('stale_provisional_review')
        return p
    def definitions(self):
        status=any(a.metadata.get('provisional_mapping') and a.status in {'executing','uncertain'} for a in self.h.state.actions.values())
        if not self.enabled:return [{'type':'function','name':'check_provisional_mapping','strict':True,'description':'Read-only positive reconciliation of an already confirmed catalog mapping. Never retries a write.','parameters':object_schema({})}] if status else []
        pair=object_schema({'category':{'type':'string'},'subcategory':{'type':'string'}})
        return [{'type':'function','name':'propose_expense_categories','strict':True,'description':'Propose bounded labels ONLY for unresolved names in the current/delivered mapping-incomplete query. Never confirms, changes totals or writes Database. Use exact existing category/subcategory pairs. Missing Database mapping alone is not semantic ambiguity: a literal name clearly describing one pair should be proposed with candidates=[]. Unclear names or possible mistaken aliases are ambiguous with category/subcategory null and zero to3 candidate pairs. No numerical confidence. No canonical item/alias creation. max10 items. The server separately renders confirmed/proposed/ambiguous values and distinct query-only/permanent consent.',
            'parameters':object_schema({'query_ref':{'type':'string'},'proposals':{'type':'array','minItems':1,'maxItems':10,'items':object_schema({'item':{'type':'string'},'status':{'type':'string','enum':['proposed','ambiguous']},'category':{'type':['string','null']},'subcategory':{'type':['string','null']},'candidates':{'type':'array','maxItems':3,'items':pair}})}})},
            *[{'type':'function','name':name,'strict':True,'description':description,'parameters':object_schema({})} for name,description in [('review_provisional_categories','Refresh and display a pending provisional category review; read only; revokes old consent.'),('check_provisional_mapping','Read-only positive reconciliation of an already confirmed mapping; never starts/retries a write.')]]]
    def context(self):
        if not self.enabled:return {}
        value={'existing_category_pairs':[{'category':c,'subcategory':s} for c,s in self.pairs()],'mapping_writes_enabled':self.mapping_enabled,'consent':'Query-only inclusion never means permanent mapping consent.'}
        try:
            b=self.basis();value.update(query_ref=b['ref'],unresolved=b['output']['mapping_diagnostics']['items'])
            if self.has_pending():value['proposals']=deepcopy(self.pending().payload['proposals'])
            else:
                _,shown=self.h._presented_result()
                if shown and shown.data.get('provisional_proposals'):value['proposals']=deepcopy(shown.data['provisional_proposals'])
        except ValueError:pass
        return {'provisional_categories':value}
    def fresh(self,basis):
        from .read_queries import execute
        from .raw_history import speakers
        # A cold confirmation has no model-visible context. Revalidate only
        # the sealed query's original owner sources still in verified history;
        # this temporary read scope never authorizes financial writes.
        refs=set(basis['args']['source_refs'])
        owned={r['source_ref'] for r in speakers(self.h.state,self.h.clock(),production=getattr(self.h,'production',False)) if r['role']=='user'}
        if not refs<=owned:raise ValueError('provisional_query_owner_source_expired')
        previous=self.h.visible;self.h.visible=previous|refs
        self.h._taxonomy_cache=None
        try:output=execute(self.h,'query_expenses',basis['args'])
        finally:self.h.visible=previous
        if not self.eligible(output) or output!=basis['output'] or catalog_hash(self.h._taxonomy())!=basis['catalog_hash']:raise Conflict('Query data or catalog changed; read again')
        return output
    def propose(self,args):
        if not self.enabled or self.h.state.pending and not self.has_pending():raise WriteBlocked('Another workflow is pending')
        p=Propose.model_validate(args);basis=self.basis(p.query_ref);self.fresh(basis)
        issues={r['item']:r for r in basis['output']['mapping_diagnostics']['items']};pairs=set(self.pairs());rows=[];seen=set();tax=self.h._taxonomy()
        for proposed in p.proposals:
            if proposed.item not in issues or proposed.item in seen:raise ValueError('unowned_or_duplicate_provisional_item')
            seen.add(proposed.item);r=proposed.model_dump();issue=issues[r['item']]
            if r['status']=='proposed':
                if (r['category'],r['subcategory']) not in pairs or r['candidates']:raise ValueError('unknown_category_pair')
            elif r['category'] is not None or r['subcategory'] is not None or any((c['category'],c['subcategory']) not in pairs for c in r['candidates']):raise ValueError('ambiguous_category_arguments')
            # Fuzzy suggestions never become identity or confirmed labels.
            alias=tax.catalog.match(r['item']).suggestions
            if issue['reason']!='missing_catalog_item' or alias:
                r.update(status='ambiguous',category=None,subcategory=None,candidates=r['candidates'] or ([{'category':proposed.category,'subcategory':proposed.subcategory}] if proposed.status=='proposed' else []))
                r['reason']='existing_name_or_mapping_requires_owner_resolution'
            r.update(expense=issue['expense'],count=issue['count']);rows.append(r)
        at=self.h._current['at'];ref='pc_'+digest([basis['ref'],self.h._active_event_id,rows])[:20]
        value={'owner':self.h.settings.owner_id,'workbook':storage_scope(self.h.sheets),'ref':ref,'version':1,'at':at,'nonce':secrets.token_hex(8),'basis':basis,'proposals':rows}
        value['review_hash']=digest(value);value['seal']=digest(value)
        self.h.state.pending=Pending(KIND,ref,self.h._active_event_id,int(datetime.fromisoformat(at).timestamp()*1000),self.h.clock()+timedelta(seconds=self.h.settings.pending_ttl_seconds),value);self.h.state.checkpoint()
        return self.render(value)
    def totals(self,v):
        b=v['basis']['output'];kind,wanted=next(iter(b['scope']['filter'].items()));selected=[r for r in v['proposals'] if r['status']=='proposed' and r[kind]==wanted]
        return {'expense':f"{sum((Decimal(r['expense']) for r in selected),Decimal(0)):.2f}",'count':sum(r['count'] for r in selected),'items':[r['item'] for r in selected]}
    def render(self,v):
        b=v['basis']['output'];partial=b['partial'];total=self.totals(v)
        lines=[f"ยืนยันจาก Database: {partial['expense']} บาท / {partial['count']} รายการ (ยอดบางส่วน)",f"เสนอสำหรับหมวดที่ถาม: {total['expense']} บาท / {total['count']} รายการ ยังไม่รวมในยอดยืนยัน"]
        for r in v['proposals']:
            label=(r['category']+('/'+r['subcategory'] if r['subcategory'] else '')) if r['status']=='proposed' else 'ยังไม่ชัดเจน'+(' '+', '.join(c['category']+('/'+c['subcategory'] if c['subcategory'] else '') for c in r['candidates']) if r['candidates'] else '')
            lines.append(r['item']+' → '+label+f" ({r['expense']} บาท / {r['count']} รายการ)")
        remaining=b['mapping_diagnostics']['unresolved_item_count']-len(v['proposals'])
        if remaining:lines.append(f'อีก {remaining} ชื่อยังไม่มีข้อเสนอ')
        lines+=['ยังยืนยันยอดครบไม่ได้ และยังไม่ได้แก้ Database','ถ้าตรวจแล้ว พิมพ์ “รวมข้อเสนอครั้งนี้” เพื่อนับข้อเสนอเฉพาะคำถามนี้']
        if self.mapping_enabled:lines.append('หากต้องการจำหมวดถาวรแยกต่างหาก พิมพ์ “บันทึกหมวดถาวร” เพื่อเพิ่มเฉพาะชื่อที่เสนอไว้ ไม่รวมชื่อที่ยังไม่ชัดเจน')
        else:lines.append('การบันทึก mapping ถาวรยังปิดอยู่')
        return {'provisional_categories':True,'status':'provisional_review','message':'\n'.join(lines),'read_only':True,'write_authorized':False,'review':{'ref':v['ref'],'version':v['version'],'hash':v['review_hash']},'confirmed_partial':deepcopy(partial),'proposed_partial':total,'proposals':deepcopy(v['proposals'])}
    def result(self,output):
        data={'domain':'expense','tool_result':deepcopy(output),'provisional_categories':True}
        if output.get('review'):
            v=self.pending().payload;data.update(provisional_category_review=output['review'],provisional_basis=deepcopy(v['basis']),read_query=deepcopy(v['basis']['output']['scope']))
        return Result('preview' if output['status']=='provisional_review' else 'answer',output['message'],data)
    def tool(self,name,args):
        try:
            if name=='propose_expense_categories':return self.propose(args)
            if args:raise ValueError('unexpected_status_arguments')
            if name=='review_provisional_categories':
                if self.has_pending():v=self.pending().payload;basis=v['basis'];rows=v['proposals']
                else:
                    basis=self.basis();_,shown=self.h._presented_result();rows=shown.data.get('provisional_proposals',[]) if shown else []
                return self.propose({'query_ref':basis['ref'],'proposals':[{k:r[k] for k in ['item','status','category','subcategory','candidates']} for r in rows]})
            key=next((k for k,a in self.h.state.actions.items() if a.metadata.get('provisional_mapping') and a.status!='completed'),None)
            message=self.resume(key).message if key else 'ไม่มี mapping ที่รอตรวจผลครับ'
            return {'provisional_categories':True,'status':'provisional_status','message':message,'read_only':True}
        except (ValueError,Conflict,NotReady,WriteBlocked,ApiError,OutcomeUnknown):return {'provisional_categories':True,'status':'provisional_unavailable','message':'ข้อมูลหรือ Database เปลี่ยน หรือข้อเสนอยังไม่ชัดเจนครับ กรุณาค้นหาและตรวจใหม่ ยังไม่ได้รวมยอดหรือแก้ mapping','read_only':True}
    def control(self,text):
        if not self.is_control(text):
            return Result('clarification','กรุณาเลือกว่าจะรวมข้อเสนอเฉพาะคำถามนี้ หรือบันทึกหมวดถาวร โดยใช้คำสั่งที่แสดงครับ ยังไม่ได้แก้ mapping') if self.has_pending() and text in {'ยืนยัน','ยืนยันครับ','ยืนยันค่ะ'} else None
        mode='query' if text.startswith('รวมข้อเสนอครั้งนี้ ') else 'permanent';parts=text.split()
        if text=='รวมข้อเสนอครั้งนี้':mode='query'
        try:
            p=self.pending();v=p.payload;row,shown=self.h._presented_result()
            if text in {'รวมข้อเสนอครั้งนี้','บันทึกหมวดถาวร'}:parts=[text,v['ref'],v['nonce']]
            if len(parts)!=3 or parts[1]!=v['ref'] or not hmac.compare_digest(parts[2],v['nonce']):raise ValueError()
            if not shown or shown.data.get('provisional_category_review')!={'ref':v['ref'],'version':v['version'],'hash':v['review_hash']}:raise ValueError()
            if datetime.fromisoformat(self.h._current['at'])<=datetime.fromisoformat(v['at']):raise ValueError()
            if not self.enabled:raise WriteBlocked()
            self.fresh(v['basis'])
        except (ValueError,Conflict,NotReady,WriteBlocked,ApiError,OutcomeUnknown):return Result('stale','ข้อเสนอหมดอายุ เปลี่ยน หรือยังไม่ได้ส่งให้ตรวจล่าสุดครับ กรุณาค้นหาและตรวจใหม่ ยังไม่ได้รวมยอดหรือแก้ mapping')
        if mode=='query':
            t=self.totals(v);base=v['basis']['output'];partial=base['partial'];combined=f"{Decimal(partial['expense'])+Decimal(t['expense']):.2f}"
            message=f"ยอดยืนยันจาก Database {partial['expense']} บาท / {partial['count']} รายการ\nข้อเสนอที่คุณอนุญาตให้นับเฉพาะคำถามนี้ {t['expense']} บาท / {t['count']} รายการ\nชื่อที่นับครั้งนี้: "+(', '.join(t['items']) or 'ไม่มี')+f"\nรวมสองส่วน {combined} บาท (ยอดบางส่วนรวมข้อเสนอ ยังไม่ใช่ยอดยืนยันจาก Database)\nยังไม่ได้บันทึก mapping ถาวร; ชื่อที่ไม่ชัดเจนหรือไม่มีข้อเสนอยังไม่นับ"
            self.h.state.pending=None;self.h.state.checkpoint()
            return Result('summary',message,{'domain':'expense','read_query':deepcopy(base['scope']),'tool_result':deepcopy(base),'provisional_categories':True,'query_only_consent':True,'proposed_partial':t,'combined_partial':{'expense':combined,'count':partial['count']+t['count'],'complete':False},'provisional_basis':deepcopy(v['basis']),'provisional_proposals':deepcopy(v['proposals'])})
        if not self.mapping_enabled:return Result('setup_required','การบันทึก mapping ถาวรยังปิดอยู่ครับ')
        blocked=self.h._blocked_action()
        if blocked:return self.resume(blocked) if self.h.state.actions[blocked].metadata.get('provisional_mapping') else self.h._uncertain(blocked)
        rows=[{k:r[k] for k in ['item','category','subcategory']} for r in v['proposals'] if r['status']=='proposed']
        if not rows:return Result('clarification','ยังไม่มีชื่อที่ชัดเจนพอให้บันทึกครับ ยังไม่ได้แก้ Database')
        key=self.h.settings.owner_id+':mapping:'+digest([v['workbook'],v['ref'],v['review_hash']]);payload={'rows':rows,'catalog_hash':v['basis']['catalog_hash']};now=self.h.clock()
        self.h.state.actions[key]=Action('save_proposed_mapping',payload,{'provisional_mapping':True,'owner':v['owner'],'workbook':v['workbook'],'payload_hash':digest(payload),'review_ref':v['ref'],'review_hash':v['review_hash'],'confirmation_event':self.h._active_event_id,'request_fence':None},p.expires_at,now+timedelta(seconds=self.h.settings.webhook_ttl_seconds))
        self.h.state.claims[self.h._active_event_id].action_key=key;self.h.state.checkpoint();return self.resume(key)
    def complete(self,key,receipt):
        a=self.h.state.actions[key];message=('ตรวจแล้ว mapping ตรงกับค่าที่คุณยืนยันครับ' if receipt['proof']=='observed_mapping_state' else 'บันทึก mapping ที่คุณยืนยันแล้วครับ')+'\n'+'\n'.join(r['item']+' → '+r['category']+('/'+r['subcategory'] if r['subcategory'] else '') for r in receipt['rows'])
        result=Result('mapping_saved',message,{'provisional_categories':True,'mapping_rows':deepcopy(receipt['rows']),'verification':receipt['proof']});a.status='completed';a.result=deepcopy(result)
        if self.has_pending() and self.h.state.pending.token==a.metadata['review_ref']:self.h.state.pending=None
        self.h.state.checkpoint();return result
    def resume(self,key):
        a=self.h.state.actions[key];m,p=a.metadata,a.payload
        if m.get('historical_classification') and self is not self.h.historical_categories:return self.h.historical_categories.resume(key)
        if not m.get('provisional_mapping') or m.get('owner')!=self.h.settings.owner_id or m.get('workbook')!=storage_scope(self.h.sheets) or m.get('payload_hash')!=digest(p):return self.h._uncertain(key)
        if a.status=='completed':return deepcopy(a.result)
        if a.status in {'executing','uncertain'}:
            proof=self.editor.reconcile(key,p)
            if proof.outcome=='committed':return self.complete(key,proof.receipt)
            a.status='uncertain';self.h.state.checkpoint();return self.h._uncertain(key)
        if not self.mapping_enabled or a.deadline<=self.h.clock():
            a.status='completed';a.result=Result('expired','การบันทึก mapping หมดอายุหรือถูกปิดครับ ยังไม่ได้แก้ Database');self.h.state.checkpoint();return deepcopy(a.result)
        a.status='executing';self.h.state.checkpoint()
        def bind(body):a.metadata['request_fence']={'body_hash':digest(body),'catalog_hash':p['catalog_hash']}
        try:receipt=self.editor.apply(p,key,bind=bind)
        except (Conflict,NotReady,WriteBlocked):
            a.status='completed';a.result=Result('conflict','Database หรือสิทธิ์เปลี่ยนครับ ยังไม่ได้บันทึก mapping กรุณาค้นหาและตรวจใหม่');self.h.state.checkpoint();return deepcopy(a.result)
        except Exception:
            a.status='uncertain';self.h.state.checkpoint();return self.h._uncertain(key)
        return self.complete(key,receipt)

def authorize_mapping(http,service,method,path,body,params):
    c,s=http.config,http.state
    if c.expense_backend!='sheets':return False
    if not (service=='sheets' and method=='POST' and params is None and path=='/v4/spreadsheets/'+c.spreadsheet_id+':batchUpdate'
        and c.provisional_mapping_enabled and c.provisional_categories_enabled and c.saved_expense_concurrency=='reviewed_single_writer'
        and c.history_enabled and s and s.holder and s.production_ready and s.owner_id==c.owner_id and s.workbook_id==c.spreadsheet_id):return False
    for key,a in s.actions.items():
        m,p=a.metadata,a.payload;f=m.get('request_fence')
        if m.get('historical_classification') and not c.historical_categories_enabled:continue
        if (not key.startswith(c.owner_id+':mapping:') or a.kind!='save_proposed_mapping' or a.status!='executing' or not m.get('provisional_mapping')
            or m.get('owner')!=c.owner_id or m.get('workbook')!=c.spreadsheet_id or m.get('payload_hash')!=digest(p)
            or not f or f!={'body_hash':digest(body),'catalog_hash':p['catalog_hash']} or body!=mapping_body(c.sheet_target(),p['rows'])):continue
        attempted=getattr(http,'mapping_attempted_keys',set())
        if key in attempted:raise LiveDisabled('Mapping mutation already attempted; reconcile only')
        attempted.add(key);http.mapping_attempted_keys=attempted;return True
    return False
