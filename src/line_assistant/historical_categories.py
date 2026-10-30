"""Complete historical reads, ten-name reviews and separately confirmed mapping.

Only current-page aggregates enter model context. Source rows/IDs remain on the
server; pages never truncate the complete configured input read. No transaction
mutation, new taxonomy or automatic consent is available to model tools.
"""
from .expense_storage import storage_scope
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal
import hmac,secrets
from pydantic import Field
from typing import Literal
from .adapters import Conflict
from .catalog import normalized
from .history_context import digest
from .model_api import object_schema
from .provisional_categories import ProvisionalCategories,Strict,Pair,catalog_hash
from .state import Action,Pending,Result
from .transport import ApiError,NotReady,OutcomeUnknown,WriteBlocked

KIND='historical_category_review'
NAMES={'scan_historical_categories','propose_historical_categories','review_historical_categories','check_historical_mapping'}
PAGE_SIZE=10

class Scan(Strict):
    mode:Literal['all','calendar_month','range']
    start_date:str|None=Field(max_length=10)
    end_date:str|None=Field(max_length=10)
    period_text:str|None=Field(max_length=120)
    relative_offset:int|None=Field(ge=-120,le=120)
    source_refs:list[str]=Field(min_length=1,max_length=16)

class Label(Strict):
    group_ref:str=Field(min_length=1,max_length=40)
    status:Literal['proposed','ambiguous','deferred']
    category:str|None=Field(max_length=200)
    subcategory:str|None=Field(max_length=200)
    candidates:list[Pair]=Field(max_length=3)

class Propose(Strict):
    review_ref:str=Field(min_length=1,max_length=40)
    proposals:list[Label]=Field(min_length=1,max_length=10)

class Change(Label):
    source_text:str=Field(min_length=1,max_length=1000)

class Review(Strict):
    review_ref:str=Field(min_length=1,max_length=40)
    operation:Literal['revise','next','previous','refresh','cancel']
    changes:list[Change]=Field(max_length=10)
    selection:Literal['keep','all_clear','only']
    selected_refs:list[str]=Field(max_length=10)
    source_refs:list[str]=Field(min_length=1,max_length=16)


class HistoricalCategories(ProvisionalCategories):
    def has_pending(self):return bool(self.h.state.pending and self.h.state.pending.kind==KIND)
    def is_control(self,text):
        return text.startswith(('บันทึกหมวดย้อนหลัง','ใช้ข้อเสนอย้อนหลังครั้งนี้','ยกเลิกจัดหมวดย้อนหลัง')) or self.has_pending() and text in {'บันทึกหมวดถาวร','รวมข้อเสนอครั้งนี้','ยกเลิก','ยืนยัน','ยืนยันครับ','ยืนยันค่ะ','ใช่'}

    def definitions(self):
        status=any(a.metadata.get('historical_classification') and a.status in {'executing','uncertain'} for a in self.h.state.actions.values())
        check={'type':'function','name':'check_historical_mapping','strict':True,'description':'Read-only positive reconciliation of an already confirmed historical mapping. Never retries writes.','parameters':object_schema({})}
        if not self.enabled:return [check] if status else []
        pair=object_schema({'category':{'type':'string'},'subcategory':{'type':'string'}})
        source_item={'type':'string'}
        current=getattr(self.h,'_current',None)
        if current and current.get('source_ref'):
            from .raw_history import speakers
            owned=[r['source_ref'] for r in speakers(self.h.state,self.h.clock(),production=getattr(self.h,'production',False)) if r['role']=='user']
            source_item['enum']=list(dict.fromkeys([current['source_ref'],*owned]))
        sources={'type':'array','minItems':1,'maxItems':16,'items':source_item,'description':'Include the exact current_source_ref. Copy whole owned source references; do not abbreviate or invent them.'}
        review_ref={'type':'string'};group_ref={'type':'string'};proposal_min=1;proposal_max=PAGE_SIZE
        try:
            v=self.pending().payload
            review_ref['enum']=[v['ref']];group_ref['enum']=[g['group_ref'] for g in v['groups']]
            proposal_min=proposal_max=len(v['groups'])
        except ValueError:
            _,shown=self.h._presented_result()
            if shown and shown.data.get('historical_continuation'):review_ref['enum']=[shown.data['historical_continuation']['ref']]
        label={'group_ref':group_ref,'status':{'type':'string','enum':['proposed','ambiguous','deferred']},'category':{'type':['string','null']},'subcategory':{'type':['string','null']},'candidates':{'type':'array','maxItems':3,'items':pair}}
        return [
            {'type':'function','name':'scan_historical_categories','strict':True,'description':'Scan ALREADY SAVED history for ALL names without confirmed categories, grouped with counts/date ranges. DEFAULT mode=all with null date fields when owner gives no period. Reads complete configured grid;10names is display pagination, not a data cap. Optional calendar_month or explicit range<=367dates uses literal owner period/source evidence. Never changes transactions or mappings. Server returns a current ten-name page; use remaining step to propose labels for that page.',
             'parameters':object_schema({'mode':{'type':'string','enum':['all','calendar_month','range']},'start_date':{'type':['string','null']},'end_date':{'type':['string','null']},'period_text':{'type':['string','null']},'relative_offset':{'type':['integer','null']},'source_refs':sources})},
            {'type':'function','name':'propose_historical_categories','strict':True,'description':'Propose existing canonical category/subcategory pairs for EVERY group on the current historical page (max10), using owned group_ref/review_ref from context. Missing mapping alone need not be semantic ambiguity. Clear names: proposed with empty candidates. Unclear meaning/possible alias: ambiguous with null category/subcategory and up to3existing pairs. Never approve, confirm, persist, invent categories or classify unseen pages. Server renders the review.',
             'parameters':object_schema({'review_ref':review_ref,'proposals':{'type':'array','minItems':proposal_min,'maxItems':proposal_max,'items':object_schema(label)}})},
            {'type':'function','name':'review_historical_categories','strict':True,'description':'Read-only historical review/navigation/correction. To SHOW THE SAME REVIEW AGAIN while retaining labels and selection: operation=revise, changes=[], selection=keep, selected_refs=[]. Do not use refresh or propose for this. refresh re-scans the source and resets the page labels/selection; reserve it for an explicit request to scan again or changed source. Interpret natural owner revisions such as all correct except X should be Y: operation=revise, changes for X with literal source_text, selection=all_clear. For only X/Y: selection=only, selected_refs from CURRENT displayed page. Changes use existing canonical pairs; unresolved/deferred remain open. Every revision shows a fresh version/nonce before separate permanent or provisional confirmation. next/previous/refresh read pages; never writes. After a completed page use the latest continuation review_ref and next. cancel cancels only the uncommitted review. For navigation use changes=[], selection=keep, selected_refs=[].',
             'parameters':object_schema({'review_ref':review_ref,'operation':{'type':'string','enum':['revise','next','previous','refresh','cancel']},'changes':{'type':'array','maxItems':10,'items':object_schema({**label,'source_text':{'type':'string'}})},'selection':{'type':'string','enum':['keep','all_clear','only']},'selected_refs':{'type':'array','maxItems':10,'items':group_ref},'source_refs':sources})},check]

    def owner_sources(self,refs):
        if self.h._current['source_ref'] not in refs:raise ValueError('current_owner_source_required')
        return self.h._owner_sources(refs)

    def snapshot(self,scope):
        self.h._taxonomy_cache=None;tax=self.h._taxonomy()
        entries=self.h.bound_sheets.classification_entries()
        source_hash=digest(sorted(digest(r) for r in entries))
        by_name={normalized(r.item):r for r in tax.rows};groups={};selected=0
        for entry in entries:
            if scope['start'] and not scope['start']<=entry['date']<=scope['end']:continue
            selected+=1;identity=tax.catalog.identity(entry['item'])
            row=by_name.get(normalized(identity.canonical)) if identity.canonical else None
            if row and row.category.strip():continue
            reason='unassigned_category' if row else 'ambiguous_item_identity' if identity.status=='ambiguous_spacing' else 'missing_catalog_item'
            key=normalized(entry['item'])
            if key not in groups:
                choices=list(identity.suggestions or tax.catalog.match(entry['item']).suggestions)
                groups[key]={'group_ref':'hg_'+digest([storage_scope(self.h.sheets),key])[:20],'item':entry['item'],'stored_names':[],
                    'reason':reason,'candidate_items':choices[:3],'count':0,'expense':Decimal(0),'first_date':entry['date'],'last_date':entry['date']}
            g=groups[key];g['count']+=1;g['expense']+=Decimal(entry['amount'])
            g['first_date']=min(g['first_date'],entry['date']);g['last_date']=max(g['last_date'],entry['date'])
            if entry['item'] not in g['stored_names']:g['stored_names'].append(entry['item'])
        ordered=[]
        for _,g in sorted(groups.items()):
            g['expense']=f"{g['expense']:.2f}";g['stored_names'].sort();g['item']=g['stored_names'][0]
            g['stored_names_count']=len(g['stored_names']);g['stored_names']=g['stored_names'][:3]
            ordered.append(g)
        return {'source_hash':source_hash,'catalog_hash':catalog_hash(tax),'groups':ordered,'source_count':len(entries),'scope_count':selected}

    def fresh(self,v):
        from .raw_history import speakers
        owned={r['source_ref'] for r in speakers(self.h.state,self.h.clock(),production=getattr(self.h,'production',False)) if r['role']=='user'}
        if not set(v['source_refs'])<=owned:raise ValueError('historical_scope_source_expired')
        current=self.snapshot(v['scope'])
        if current['source_hash']!=v['source_hash'] or current['catalog_hash']!=v['catalog_hash']:raise Conflict('Historical source or catalog changed; refresh review')
        return current

    def store(self,v):
        v=deepcopy(v);v.pop('seal',None);v.pop('review_hash',None)
        v['version']=v.get('version',0)+1;v['at']=self.h._current['at'];v['nonce']=secrets.token_hex(8)
        v['review_hash']=digest(v);v['seal']=digest(v)
        at=datetime.fromisoformat(v['at']);self.h.state.pending=Pending(KIND,v['ref'],v['origin_event'],int(at.timestamp()*1000),self.h.clock()+timedelta(seconds=self.h.settings.pending_ttl_seconds),v)
        self.h.state.checkpoint();return v

    def require_newer(self):
        if self.has_pending() and datetime.fromisoformat(self.h._current['at'])<=datetime.fromisoformat(self.h.state.pending.payload['at']):
            raise ValueError('historical_operation_precedes_review')

    def page(self,v,snapshot,offset):
        skipped=set(v.get('skip_refs',[]));available=[g for g in snapshot['groups'] if g['group_ref'] not in skipped]
        if not available:
            if self.has_pending():self.h.state.pending=None;self.h.state.checkpoint()
            return {'historical_categories':True,'status':'historical_empty','message':f"อ่านข้อมูลครบแล้วครับ ไม่มีชื่อในหน้าถัดไป พบชื่อยังไม่มีหมวดทั้งหมด {len(snapshot['groups'])} ชื่อ; ชื่อที่ข้ามจากชุดก่อนยังตรวจใหม่ได้ ไม่ได้แก้รายการเดิมหรือ mapping",'read_only':True,'total_names':len(snapshot['groups']),'shown':0,'remaining_names':0,'scope':deepcopy(v['scope'])}
        offset=max(0,min(offset,max(0,(len(available)-1)//PAGE_SIZE*PAGE_SIZE)))
        v.update(source_hash=snapshot['source_hash'],catalog_hash=snapshot['catalog_hash'],offset=offset,
            total_names=len(snapshot['groups']),scope_count=snapshot['scope_count'],source_count=snapshot['source_count'],
            skipped_names=sum(g['group_ref'] in skipped for g in snapshot['groups']),remaining_names=max(0,len(available)-offset-PAGE_SIZE),
            prefix_refs=[g['group_ref'] for g in available[:offset]],
            groups=[{**deepcopy(g),'status':'ambiguous','category':None,'subcategory':None,'candidates':[],'included':False} for g in available[offset:offset+PAGE_SIZE]],stage='unclassified',selection='all_clear')
        return self.render(self.store(v),continue_model=bool(v['groups']))

    def scan(self,args):
        if not self.enabled or self.h.state.pending and not self.has_pending():raise WriteBlocked('Another workflow is pending')
        self.require_newer()
        p=Scan.model_validate(args);self.owner_sources(p.source_refs)
        scope={'mode':p.mode,'start':None,'end':None}
        if p.mode=='all':
            if any(getattr(p,k) is not None for k in ['start_date','end_date','period_text','relative_offset']):raise ValueError('all_history_has_no_hidden_date_filter')
        else:
            from .read_queries import execute
            out=execute(self.h,'query_expenses',{'start_date':p.start_date,'end_date':p.end_date,'date_mode':p.mode,'relative_offset':p.relative_offset,'period_text':p.period_text,'source_refs':p.source_refs,'filter':None,'view':'total','measure':'expense'})
            if out.get('complete') is not True:raise NotReady('Explicit historical date scope is unavailable')
            scope.update(start=out['scope']['start'],end=out['scope']['end'])
        snapshot=self.snapshot(scope)
        v={'format':'historical-categories-v1','owner':self.h.settings.owner_id,'workbook':storage_scope(self.h.sheets),
            'ref':'hc_'+digest([self.h.settings.owner_id,self.h._active_event_id,scope])[:20],'origin_event':self.h._active_event_id,
            'scope':scope,'source_refs':p.source_refs,'review_source_refs':[],'skip_refs':[],'page_number':1}
        return self.page(v,snapshot,0)

    def label(self,g,p):
        pairs=set(self.pairs());r=p.model_dump();r.pop('source_text',None);r.pop('group_ref',None)
        if r['status']=='proposed':
            if (r['category'],r['subcategory']) not in pairs or r['candidates']:raise ValueError('existing_canonical_pair_required')
        elif r['category'] is not None or r['subcategory'] is not None or any((c['category'],c['subcategory']) not in pairs for c in r['candidates']):raise ValueError('ambiguous_or_deferred_pair')
        if g['reason']!='missing_catalog_item' or g['candidate_items']:
            r.update(status='ambiguous',category=None,subcategory=None,candidates=r['candidates'] or ([{'category':p.category,'subcategory':p.subcategory}] if p.status=='proposed' else []))
        return {**g,**r,'included':r['status']=='proposed'}

    def propose(self,args):
        if not self.enabled:raise WriteBlocked('Historical classification disabled')
        p=Propose.model_validate(args);v=deepcopy(self.pending().payload)
        if p.review_ref!=v['ref']:raise ValueError('foreign_historical_review')
        # The first proposal follows scan/navigation in the same owner turn.
        # Once reviewed, only the revision tool may change labels/selection.
        if v['stage']!='unclassified' or datetime.fromisoformat(self.h._current['at'])<datetime.fromisoformat(v['at']):raise ValueError('historical_proposal_not_current')
        self.fresh(v);by_ref={r.group_ref:r for r in p.proposals}
        if len(by_ref)!=len(p.proposals) or set(by_ref)!={g['group_ref'] for g in v['groups']}:raise ValueError('propose_current_page_completely')
        v['groups']=[self.label(g,by_ref[g['group_ref']]) for g in v['groups']];v['stage']='review'
        return self.render(self.store(v))

    def presented(self,v):
        _,shown=self.h._presented_result()
        return shown and shown.data.get('historical_category_review')=={'ref':v['ref'],'version':v['version'],'hash':v['review_hash']}

    def review(self,args):
        if not self.enabled:raise WriteBlocked('Historical classification disabled')
        self.require_newer()
        p=Review.model_validate(args);sources=self.owner_sources(p.source_refs)
        if not self.has_pending():
            _,shown=self.h._presented_result();v=deepcopy(shown.data.get('historical_continuation')) if shown else None
            if not v or v['ref']!=p.review_ref or p.operation not in {'next','refresh'}:raise ValueError('no_historical_page')
            if v['owner']!=self.h.settings.owner_id or v['workbook']!=storage_scope(self.h.sheets):raise ValueError('foreign_historical_continuation')
            if p.changes or p.selection!='keep' or p.selected_refs:raise ValueError('navigation_cannot_change_labels')
            snapshot=self.snapshot(v['scope']);v.update(ref='hc_'+digest([v['ref'],self.h._active_event_id])[:20],origin_event=self.h._active_event_id,review_source_refs=[],stage='unclassified')
            if p.operation=='refresh':v.update(skip_refs=[],page_number=1)
            return self.page(v,snapshot,0)
        v=deepcopy(self.pending().payload)
        same_subset=p.selection=='only' and v.get('selection')=='only' and set(p.selected_refs)=={g['group_ref'] for g in v['groups'] if g['included']}
        redisplay=p.operation=='revise' and not p.changes and (p.selection=='keep' and not p.selected_refs or same_subset)
        # A read-only redisplay restores a fresh review after an intervening
        # response. Actual revisions and consent still require latest delivery.
        if p.review_ref!=v['ref'] or p.operation!='refresh' and not redisplay and not self.presented(v):raise ValueError('historical_review_not_delivered_latest')
        if p.operation=='cancel':return self.cancel()
        if p.operation in {'next','previous','refresh'}:
            if p.changes or p.selection!='keep' or p.selected_refs:raise ValueError('navigation_cannot_change_labels')
            snapshot=self.snapshot(v['scope']) if p.operation=='refresh' else self.fresh(v)
            if p.operation=='next' and not v['remaining_names'] or p.operation=='previous' and not v['offset']:
                output=self.render(v);output['message']='ไม่มีหน้าถัดไปครับ\n'+output['message'] if p.operation=='next' else 'อยู่หน้าแรกแล้วครับ\n'+output['message'];return output
            offset=v['offset']+(PAGE_SIZE if p.operation=='next' else -PAGE_SIZE if p.operation=='previous' else 0)
            if p.operation=='refresh':v.update(skip_refs=[],page_number=1)
            else:v['page_number']=max(1,v['page_number']+(1 if p.operation=='next' else -1))
            return self.page(v,snapshot,offset)
        self.fresh(v)
        if v['stage']!='review':raise ValueError('proposals_required_before_revision')
        by_ref={c.group_ref:c for c in p.changes};known={g['group_ref'] for g in v['groups']}
        if len(by_ref)!=len(p.changes) or not set(by_ref)<=known or not set(p.selected_refs)<=known:raise ValueError('correction_outside_displayed_page')
        if any(not any(c.source_text in source['text'] for source in sources) for c in p.changes):raise ValueError('literal_owner_correction_required')
        included={g['group_ref']:g['included'] for g in v['groups']}
        v['groups']=[self.label(g,by_ref[g['group_ref']]) if g['group_ref'] in by_ref else g for g in v['groups']]
        if p.selection!='only' and p.selected_refs:raise ValueError('unexpected_selected_refs')
        for g in v['groups']:
            if p.selection=='all_clear':g['included']=g['status']=='proposed'
            elif p.selection=='only':g['included']=g['status']=='proposed' and g['group_ref'] in p.selected_refs
            else:g['included']=g['status']=='proposed' and included[g['group_ref']]
        if p.selection!='keep':v['selection']=p.selection
        v['review_source_refs']=list(dict.fromkeys([*v.get('review_source_refs',[]),*p.source_refs]))
        return self.render(self.store(v))

    def render(self,v,*,continue_model=False):
        scope='ทุกช่วงวันที่ในชีต' if not v['scope']['start'] else v['scope']['start']+' ถึง '+v['scope']['end']
        lines=[f"ตรวจชื่อที่ยังไม่มีหมวด: {scope}",f"อ่านครบ {v['scope_count']} รายการในช่วงนี้ พบ {v['total_names']} ชื่อ หน้านี้ {len(v['groups'])} ชื่อ เหลือถัดไป {v['remaining_names']} ชื่อ"]
        if v['skipped_names']:lines.append(f"อีก {v['skipped_names']} ชื่อจากชุดก่อนยังไม่มี mapping; เริ่มตรวจใหม่เพื่อกลับไปดูได้")
        if v.get('selection')=='only':
            excluded=sum(not g['included'] for g in v['groups'])
            lines.append(f'แสดงรายละเอียดครบเฉพาะชื่อที่เลือก อีก {excluded} ชื่อในหน้านี้ไม่ได้เลือก จึงไม่แสดงและไม่รวมในการยืนยัน; ขอทบทวนทุกชื่อเพื่อเลือกเพิ่มได้')
        for i,g in enumerate(v['groups'],1):
            if v.get('selection')=='only' and not g['included']:continue
            label=(g['category']+('/'+g['subcategory'] if g['subcategory'] else '') if g['included'] else 'ไม่ได้เลือกในชุดนี้') if g['status']=='proposed' else 'พักไว้' if g['status']=='deferred' else 'ยังไม่ชัดเจน'
            selected='เลือกไว้' if g['included'] else 'ยังไม่เลือก'
            lines.append(f"{i}. {g['item']} → {label} [{selected}] | {g['count']} รายการ / {g['expense']} บาท | {g['first_date']}–{g['last_date']}")
            if g['candidates'] and v.get('selection')!='only':lines.append('ตัวเลือกที่ยังไม่ยืนยัน: '+', '.join(c['category']+('/'+c['subcategory'] if c['subcategory'] else '') for c in g['candidates']))
            if g['stored_names_count']>1 and (g['included'] or v.get('selection')!='only'):lines.append('ชื่อที่จัดกลุ่ม: '+', '.join(g['stored_names'])+f" ({g['stored_names_count']} รูปแบบการเขียน; แสดงไม่เกิน 3)")
        lines+=['ยังไม่ได้แก้รายการเดิมหรือ Database; ข้อเสนอเป็นหมวดชั่วคราวที่ต้องตรวจ','บอกแก้ได้ เช่น “ถูกหมด ยกเว้นชื่อ X ให้เป็นหมวด Y” หรือเลือกเฉพาะบางชื่อ แล้วตรวจร่างใหม่','พิมพ์ “ใช้ข้อเสนอย้อนหลังครั้งนี้” เพื่อสรุปชั่วคราวเฉพาะชื่อที่เลือกในหน้านี้ ไม่จำ mapping']
        if self.mapping_enabled:lines+=['พิมพ์ “บันทึกหมวดย้อนหลัง” เพื่อยืนยันเพิ่ม mapping เฉพาะชื่อที่เลือกและชัดเจนในหน้านี้','mapping ถาวรมีผลกับชื่อเดียวกันทุกช่วงย้อนหลังและคำถามในอนาคต ไม่ได้จำกัดเฉพาะช่วงที่แสดง']
        else:lines.append('การบันทึก mapping ถาวรยังปิดอยู่')
        lines.append('ขอหน้าถัดไป/หน้าก่อน/ตรวจใหม่ หรือยกเลิกได้; การเปลี่ยนหน้าจะไม่บันทึกข้อเสนอของหน้าก่อน')
        message='\n'.join(lines);confirmable=len(message.encode('utf-16-le'))<=9000
        if not confirmable:
            message='ชื่อหรือหมวดในชุดนี้ยาวเกินหน้าตรวจที่แสดงได้ครบครับ ยังยืนยันชุดนี้ไม่ได้ กรุณาเลือกเฉพาะบางชื่อให้แสดงรายละเอียดใหม่\n'+'\n'.join(f"{i}. {g['item']} | {g['count']} รายการ | {g['first_date']}–{g['last_date']}" for i,g in enumerate(v['groups'],1))+'\nยังไม่ได้แก้ mapping; รายละเอียดหมวดบางส่วนยังไม่แสดง จึงไม่รับคำยืนยัน'
        return {'historical_categories':True,'status':'historical_scan' if continue_model else 'historical_review','message':message,'read_only':True,'write_authorized':False,'confirmation_available':confirmable,
            'continue_model':continue_model,'review':{'ref':v['ref'],'version':v['version'],'hash':v['review_hash']},'groups':deepcopy(v['groups']),
            'total_names':v['total_names'],'shown':len(v['groups']),'remaining_names':v['remaining_names'],'skipped_names':v['skipped_names'],'scope':deepcopy(v['scope']),'page_number':v['page_number']}

    def context(self):
        if not self.enabled:return {}
        context={'existing_category_pairs':[{'category':c,'subcategory':s} for c,s in self.pairs()],'mapping_writes_enabled':self.mapping_enabled,'page_size':PAGE_SIZE,'consent':'Only displayed selected clear groups; a correction is never permanent consent.'}
        try:
            v=self.pending().payload;context.update(review_ref=v['ref'],stage=v['stage'],groups=deepcopy(v['groups']),scope=deepcopy(v['scope']),remaining_names=v['remaining_names'],confirmation_available=self.render(v)['confirmation_available'])
        except ValueError:
            _,shown=self.h._presented_result()
            if shown and shown.data.get('historical_continuation'):context.update(review_ref=shown.data['historical_continuation']['ref'],completed_page=True)
        return {'historical_categories':context}

    def result(self,output):
        data={'domain':'expense','historical_categories':True,'tool_result':deepcopy(output)}
        if output.get('review'):data['historical_category_review']=deepcopy(output['review'])
        if output.get('cancelled'):return Result('cancelled',output['message'],data)
        return Result('preview' if output['status'] in {'historical_scan','historical_review'} else 'answer',output['message'],data)

    def cancel(self):
        blocked=self.h._blocked_action()
        if blocked:raise WriteBlocked('Uncertain external effect cannot be cancelled')
        self.require_newer()
        self.h.state.pending=None;self.h.state.checkpoint()
        return {'historical_categories':True,'status':'historical_cancelled','cancelled':True,'message':'ยกเลิกเฉพาะร่างจัดหมวดย้อนหลังแล้วครับ ไม่ได้แก้รายการเดิมหรือ mapping','read_only':True}

    def tool(self,name,args):
        try:
            if name=='scan_historical_categories':return self.scan(args)
            if name=='propose_historical_categories':return self.propose(args)
            if name=='review_historical_categories':return self.review(args)
            if args:raise ValueError('status_arguments')
            key=next((k for k,a in self.h.state.actions.items() if a.metadata.get('historical_classification') and a.status!='completed'),None)
            return {'historical_categories':True,'status':'historical_status','message':self.resume(key).message if key else 'ไม่มี mapping ย้อนหลังที่รอตรวจผลครับ','read_only':True}
        except (ValueError,Conflict,NotReady,WriteBlocked,ApiError,OutcomeUnknown):
            return {'historical_categories':True,'status':'historical_unavailable','message':'ข้อมูลหรือ Database เปลี่ยน ข้อเสนอหมดอายุ หรือยังไม่ชัดเจนครับ กรุณาตรวจใหม่ ยังไม่ได้แก้ mapping หรือรายการเดิม','read_only':True}

    def continuation(self,v):
        return {k:deepcopy(v[k]) for k in ['owner','workbook','ref','origin_event','scope','source_refs','review_source_refs','page_number']}|{'skip_refs':list(dict.fromkeys([*v.get('skip_refs',[]),*v.get('prefix_refs',[]),*(g['group_ref'] for g in v['groups'])]))}

    def control(self,text):
        if not self.is_control(text):return None
        if text in {'ยืนยัน','ยืนยันครับ','ยืนยันค่ะ','ใช่','ยกเลิก'}:
            _,shown=self.h._presented_result()
            if shown and shown.status=='preview' and not shown.data.get('historical_categories') and any(shown.data.get(k) for k in ['confirm_command','calendar_view','calendar_batch_view']):
                # Let the displayed workflow enforce its own version, nonce,
                # delivery and ambiguity checks. A waiting historical review
                # cannot claim this confirmation/cancellation.
                return None
        prefix=text.split()[0] if text.split() else ''
        allowed={'บันทึกหมวดย้อนหลัง','บันทึกหมวดถาวร','ใช้ข้อเสนอย้อนหลังครั้งนี้','รวมข้อเสนอครั้งนี้','ยกเลิกจัดหมวดย้อนหลัง','ยกเลิก','ยืนยัน','ยืนยันครับ','ยืนยันค่ะ','ใช่'}
        if prefix not in allowed:return Result('stale','คำสั่งไม่ตรงกับร่างจัดหมวดล่าสุดครับ ยังไม่ได้แก้ mapping')
        if prefix in {'ยกเลิกจัดหมวดย้อนหลัง','ยกเลิก'} and text!=prefix:return Result('stale','คำสั่งยกเลิกไม่ตรงครับ ยังไม่ได้แก้ mapping')
        for prefix in ['บันทึกหมวดย้อนหลัง','ใช้ข้อเสนอย้อนหลังครั้งนี้']:
            if text.startswith(prefix+' ') and not text[len(prefix)+1:].startswith('hc_'):return None
        if text in {'ยกเลิก','ยกเลิกจัดหมวดย้อนหลัง'} and self.has_pending():
            try:return self.result(self.cancel())
            except WriteBlocked:return self.h._uncertain(self.h._blocked_action())
            except ValueError:return Result('stale','ข้อความยกเลิกมาก่อนร่างล่าสุดครับ')
        if text in {'ยืนยัน','ยืนยันครับ','ยืนยันค่ะ','ใช่'}:return Result('clarification','ต้องการใช้ชั่วคราวเฉพาะหน้านี้ หรือบันทึกหมวดย้อนหลังถาวรครับ? ยังไม่ได้แก้ mapping')
        mode='query' if text in {'ใช้ข้อเสนอย้อนหลังครั้งนี้','รวมข้อเสนอครั้งนี้'} or text.startswith('ใช้ข้อเสนอย้อนหลังครั้งนี้ ') else 'permanent'
        try:
            v=self.pending().payload
            exact=text.split()
            if len(exact)>1 and (len(exact)!=3 or exact[1]!=v['ref'] or not hmac.compare_digest(exact[2].encode(),v['nonce'].encode())):raise ValueError('stale_historical_nonce')
            if text not in {'ใช้ข้อเสนอย้อนหลังครั้งนี้','รวมข้อเสนอครั้งนี้','บันทึกหมวดย้อนหลัง','บันทึกหมวดถาวร'} and len(exact)!=3:raise ValueError('mixed_confirmation')
            if not self.enabled or v['stage']!='review' or not self.presented(v) or not self.render(v)['confirmation_available'] or datetime.fromisoformat(self.h._current['at'])<=datetime.fromisoformat(v['at']):raise ValueError('historical_review_not_confirmable')
            if mode=='query':self.fresh(v)
        except (ValueError,Conflict,NotReady,WriteBlocked,ApiError,OutcomeUnknown):return Result('stale','ร่างหรือข้อมูลเปลี่ยน/หมดอายุครับ กรุณาตรวจใหม่ ยังไม่ได้แก้ mapping')
        selected=[g for g in v['groups'] if g['included'] and g['status']=='proposed']
        if not selected:return Result('clarification','ยังไม่มีชื่อที่เลือกและชัดเจนพอครับ กรุณาระบุหมวดหรือเลือกชื่อก่อน ยังไม่ได้แก้ Database')
        if mode=='query':
            totals={}
            for g in selected:
                pair=(g['category'],g['subcategory']);a=totals.setdefault(pair,{'category':pair[0],'subcategory':pair[1],'expense':Decimal(0),'count':0});a['expense']+=Decimal(g['expense']);a['count']+=g['count']
            summary=[{**a,'expense':f"{a['expense']:.2f}"} for a in totals.values()]
            message='ใช้หมวดชั่วคราวเฉพาะชุดที่คุณตรวจครั้งนี้ครับ ไม่ได้บันทึก mapping และยังไม่ใช่ยอดครบทุกชื่อ\n'+'\n'.join(f"{a['category']}/{a['subcategory']}: {a['expense']} บาท / {a['count']} รายการ" for a in summary)
            result=Result('summary',message,{'historical_categories':True,'query_only_consent':True,'provisional_summary':summary,'included_names':[g['item'] for g in selected],'historical_continuation':self.continuation(v)})
            self.h.state.pending=None;self.h.state.checkpoint();return result
        if not self.mapping_enabled:return Result('setup_required','การบันทึกหมวดย้อนหลังถาวรยังปิดอยู่ครับ')
        blocked=self.h._blocked_action()
        if blocked:return self.resume(blocked) if self.h.state.actions[blocked].metadata.get('historical_classification') else self.h._uncertain(blocked)
        rows=[{k:g[k] for k in ['item','category','subcategory']} for g in selected]
        payload={'rows':rows,'catalog_hash':v['catalog_hash']};key=self.h.settings.owner_id+':mapping:'+digest([v['workbook'],v['ref'],v['review_hash']]);now=self.h.clock()
        basis={k:deepcopy(v[k]) for k in ['source_hash','catalog_hash','source_refs','scope']}
        metadata={'provisional_mapping':True,'historical_classification':True,'owner':v['owner'],'workbook':v['workbook'],'payload_hash':digest(payload),'review_ref':v['ref'],'review_hash':v['review_hash'],'confirmation_event':self.h._active_event_id,'request_fence':None,'historical_basis':basis,'historical_continuation':self.continuation(v)}
        self.h.state.actions[key]=Action('save_proposed_mapping',payload,metadata,self.pending().expires_at,now+timedelta(seconds=self.h.settings.webhook_ttl_seconds));self.h.state.claims[self.h._active_event_id].action_key=key;self.h.state.checkpoint()
        return self.resume(key)

    def resume(self,key):
        action=self.h.state.actions[key];m=action.metadata
        owned=(m.get('historical_classification') and m.get('owner')==self.h.settings.owner_id
            and m.get('workbook')==storage_scope(self.h.sheets) and m.get('payload_hash')==digest(action.payload))
        if owned and action.status not in {'completed','executing','uncertain'} and self.mapping_enabled and action.deadline>self.h.clock():
            # A crash may happen after durable confirmation but before the first
            # attempt. That ready Action must validate source again at commit.
            # Executing/uncertain actions only reconcile and never retry.
            try:self.fresh(m['historical_basis'])
            except (ValueError,KeyError,Conflict,NotReady,WriteBlocked,ApiError,OutcomeUnknown):
                result=Result('stale','ข้อมูลหรือ Database เปลี่ยนหลังร่างที่ยืนยันครับ กรุณาตรวจใหม่ ยังไม่ได้แก้ mapping')
                action.status='completed';action.result=deepcopy(result)
                if self.has_pending() and self.h.state.pending.token==m['review_ref']:
                    # Recovery may bypass a new owner/model turn. Invalidate
                    # the old review without minting time/source authority.
                    v=deepcopy(self.h.state.pending.payload);v.pop('seal',None);v.pop('review_hash',None)
                    v['stage']='stale';v['version']+=1;v['nonce']=secrets.token_hex(8)
                    v['review_hash']=digest(v);v['seal']=digest(v);self.h.state.pending.payload=v
                self.h.state.checkpoint();return result
        return super().resume(key)

    def complete(self,key,receipt):
        action=self.h.state.actions[key];continuation=deepcopy(action.metadata.get('historical_continuation'))
        message=('ตรวจแล้ว mapping ตรงกับค่าที่คุณยืนยันครับ' if receipt['proof']=='observed_mapping_state' else 'บันทึก mapping ที่คุณยืนยันแล้วครับ')+'\n'+'\n'.join(r['item']+' → '+r['category']+('/'+r['subcategory'] if r['subcategory'] else '') for r in receipt['rows'])
        message+='\nmapping มีผลกับชื่อเดียวกันทุกช่วงย้อนหลังและคำถามในอนาคต รายการเดิมไม่ได้ถูกแก้'
        result=Result('mapping_saved',message,{'historical_categories':True,'mapping_rows':deepcopy(receipt['rows']),'verification':receipt['proof'],'historical_continuation':continuation})
        action.status='completed';action.result=deepcopy(result)
        if self.has_pending() and self.h.state.pending.token==action.metadata['review_ref']:self.h.state.pending=None
        self.h.state.checkpoint();return result
