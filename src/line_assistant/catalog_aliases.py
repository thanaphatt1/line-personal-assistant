"""Owner-confirmed vocabulary, scoped to one catalog and persisted across turns."""
from copy import deepcopy
from datetime import datetime,timedelta
import hmac,secrets
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal

from .catalog import ItemCatalog,normalized
from .conversation_ui import render_actions,utf16_limit
from .expense_storage import storage_scope
from .history_context import digest
from .model_api import object_schema
from .source_spans import finditer
from .state import Action,Pending,Result
from .transport import WriteBlocked

MAX_ALIASES=100
PREFIX='catalog_alias_'
NAMES={'prepare_catalog_alias','list_catalog_aliases','review_catalog_alias'}


class Proposal(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    operation:Literal['set','remove']
    alias:str=Field(min_length=1,max_length=200)
    item:str|None=Field(max_length=200)
    source_refs:list[str]=Field(min_length=1,max_length=16)


def validate_records(records):
    if not isinstance(records,dict) or len(records)>MAX_ALIASES:raise ValueError('Invalid owner alias records')
    for key,row in records.items():
        if (not isinstance(row,dict) or set(row)!={'alias','item','source_refs','confirmed_at'}
            or any(not isinstance(row[k],str) or not 1<=len(row[k])<=200 for k in ('alias','item'))
            or key!=normalized(row['alias']) or not isinstance(row['source_refs'],list)
            or not 1<=len(row['source_refs'])<=16 or any(not isinstance(r,str) for r in row['source_refs'])
            or not isinstance(row['confirmed_at'],str)
            or datetime.fromisoformat(row['confirmed_at']).utcoffset() is None):
            raise ValueError('Invalid owner alias binding')


class CatalogAliases:
    def __init__(self,host,*,enabled=False):self.h=host;self.enabled=enabled
    def has_pending(self):return bool(self.h.state.pending and self.h.state.pending.kind==PREFIX+'review')
    def is_control(self,text):return text.startswith('a1|')
    def context(self):
        if not self.enabled:return {}
        return {'catalog_aliases':[{'alias':v['alias'],'item':v['item']} for v in self.h.state.catalog_aliases.values()],
                'catalog_alias_review':deepcopy(self.h.state.pending.payload) if self.has_pending() else None}
    def overlay(self,taxonomy):
        if not self.enabled:return taxonomy
        # Removed/renamed targets and new canonical-name collisions deactivate
        # an alias; they never override a real catalog identity.
        aliases={v['alias']:v['item'] for v in self.h.state.catalog_aliases.values()
            if taxonomy.catalog.items.get(normalized(v['item']))==v['item'] and normalized(v['alias']) not in taxonomy.catalog.items}
        taxonomy.catalog=ItemCatalog([r.item for r in taxonomy.rows],aliases)
        return taxonomy
    def definitions(self):
        if not self.enabled:return []
        return [
            {'type':'function','name':'prepare_catalog_alias','strict':True,
             'description':'Prepare a durable owner vocabulary mapping or its removal. Only when the owner requests remembering/changing/forgetting a name equivalence. Both alias and target must be supported by original owner wording; item is an exact current catalog name, null for removal. Never infer a synonym or create a new catalog item. Shows a review; only fresh owner confirmation changes memory.',
             'parameters':object_schema({'operation':{'type':'string','enum':['set','remove']},'alias':{'type':'string','maxLength':200},
                'item':{'type':['string','null'],'maxLength':200},'source_refs':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':16}})},
            {'type':'function','name':'list_catalog_aliases','strict':True,
             'description':'Read a page of owner-confirmed vocabulary for this catalog. Ten aliases per page; page null means first. No writes; an unavailable target is not silently remapped.',
             'parameters':object_schema({'page':{'type':['integer','null'],'minimum':1,'maximum':10}})},
            {'type':'function','name':'review_catalog_alias','strict':True,
             'description':'Redisplay the pending vocabulary change for a fresh owner confirmation after an interruption. Does not change memory.',
             'parameters':object_schema({})}]
    def check_pair(self,alias,item,*,catalog=None):
        if catalog is None:catalog=self.h._taxonomy().catalog
        if catalog.items.get(normalized(item))!=item:raise ValueError('alias_target_unavailable')
        if normalized(alias) in catalog.items:raise ValueError('alias_canonical_collision')
        if '\n' in alias or '\r' in alias:raise ValueError('alias_invalid_label')
    def tool(self,name,args):
        if not self.enabled:return {'catalog_alias':True,'ok':False,'message':'ยังไม่เปิดการจำชื่อเรียกครับ'}
        try:
            if name=='review_catalog_alias':
                if args or not self.has_pending():raise ValueError('alias_missing_review')
                v=self.h.state.pending.payload
                if (v.get('owner')!=self.h.settings.owner_id or v.get('scope')!=storage_scope(self.h.sheets)
                    or v.get('seal')!=digest({k:x for k,x in v.items() if k!='seal'})):raise ValueError('alias_invalid_review')
                sources=self.h._owner_sources(v['source_refs'])
                refs=[]
                for label in [v['alias'],*([v['item']] if v['operation']=='set' else [])]:
                    source=next((r for r in sources if finditer(r['text'],label)),None)
                    if source is None:raise ValueError('alias_source_required')
                    refs.append(source['source_ref'])
                return self.tool('prepare_catalog_alias',{k:deepcopy(v[k]) for k in ('operation','alias','item')}|
                    {'source_refs':list(dict.fromkeys([*refs,self.h._current['source_ref']]))})
            if name=='list_catalog_aliases':
                if set(args)-{'page'}:raise ValueError('alias_invalid_arguments')
                page=args.get('page') if args.get('page') is not None else 1
                if type(page) is not int or not 1<=page<=10:raise ValueError('alias_invalid_arguments')
                records=list(self.h.state.catalog_aliases.values());pages=max(1,(len(records)+9)//10)
                if page>pages:raise ValueError('alias_invalid_arguments')
                rows=[{'alias':v['alias'],'item':v['item']} for v in records[(page-1)*10:page*10]]
                message='ชื่อเรียกที่จำไว้ครับ'+(f' (หน้า {page}/{pages})' if pages>1 else '')+'\n'
                message+='\n'.join(utf16_limit(v['alias'],200)+' → '+utf16_limit(v['item'],200) for v in rows)
                return {'catalog_alias':True,'ok':True,'page':page,'page_count':pages,'count':len(records),'aliases':rows,
                    'message':message if rows else 'ยังไม่มีชื่อเรียกที่จำไว้ครับ'}
            p=Proposal.model_validate(args)
            if self.h.state.pending and not self.has_pending():raise ValueError('alias_other_pending')
            sources=self.h._owner_sources(p.source_refs)
            if self.h._current['source_ref'] not in p.source_refs:raise ValueError('alias_owner_source_required')
            if not any(finditer(r['text'],p.alias) for r in sources):raise ValueError('alias_source_required')
            key=normalized(p.alias);before=deepcopy(self.h.state.catalog_aliases.get(key))
            if p.operation=='set':
                if p.item is None or not any(finditer(r['text'],p.item) for r in sources):raise ValueError('alias_target_source_required')
                self.check_pair(p.alias,p.item)
                if before is None and len(self.h.state.catalog_aliases)>=MAX_ALIASES:raise ValueError('alias_capacity')
            elif p.item is not None or before is None:raise ValueError('alias_missing_removal')
            value={'owner':self.h.settings.owner_id,'scope':storage_scope(self.h.sheets),**p.model_dump(),
                'before':before,'at':self.h._current['at'],'nonce':secrets.token_hex(8)}
            ref='alias_'+digest([value,self.h._active_event_id])[:20];value['ref']=ref
            value['seal']=digest(value)
            self.h.state.pending=Pending(PREFIX+'review',ref,self.h._active_event_id,
                int(datetime.fromisoformat(value['at']).timestamp()*1000),self.h.clock()+timedelta(seconds=self.h.settings.pending_ttl_seconds),value)
            self.h.state.owner_features_used=True;self.h.state.checkpoint()
            effect=f"จำชื่อเรียกนี้ไว้ใช้ครั้งต่อไป:\n{p.alias} → {p.item}" if p.operation=='set' else f"เลิกจำชื่อเรียก {p.alias}"
            if before and p.operation=='set':effect+=f"\nเดิมหมายถึง {before['item']}"
            return {'catalog_alias':True,'ok':True,'review':{'ref':ref,'nonce':value['nonce'],'seal':value['seal']},
                'message':effect+'\nใช้ตามนี้เลยไหมครับ? พิมพ์ยืนยันหรือกดปุ่มได้'}
        except (ValueError,WriteBlocked) as error:
            code=str(error)
            message={
                'alias_target_unavailable':'ยังไม่พบชื่อปลายทางนี้ในทะเบียนครับ ต้องเพิ่มชื่อจริงให้เรียบร้อยก่อน',
                'alias_canonical_collision':'ชื่อเรียกนี้เป็นชื่อจริงในทะเบียนอยู่แล้วครับ จึงนำไปชี้ชื่ออื่นไม่ได้',
                'alias_other_pending':'ยังมีรายการอื่นรอยืนยันครับ กรุณาทำรายการนั้นให้เสร็จหรือยกเลิกก่อน',
                'alias_source_required':'ยังจับคู่ชื่อเรียกกับข้อความที่คุณบอกไม่ได้ครับ',
                'alias_target_source_required':'ยังจับคู่ชื่อปลายทางกับข้อความที่คุณบอกไม่ได้ครับ',
                'alias_missing_removal':'ไม่พบชื่อเรียกนี้ที่จำไว้ครับ',
                'alias_capacity':'ชื่อเรียกที่จำไว้เต็มแล้วครับ กรุณาลบชื่อที่ไม่ใช้ก่อน',
            }.get(code,'ยังเตรียมชื่อเรียกไม่ได้ครับ กรุณาระบุชื่อเรียกและชื่อจริงให้ชัดเจน')
            return {'catalog_alias':True,'ok':False,'error_code':code if code in {'alias_target_unavailable','alias_canonical_collision','alias_other_pending','alias_source_required','alias_target_source_required','alias_missing_removal','alias_capacity'} else 'alias_invalid_arguments','message':message}
    def result(self,output):
        review=output.get('review');data={'domain':'catalog_alias','tool_result':deepcopy(output)}
        if review:
            data['catalog_alias_review']=review
            label='จำไว้ครั้งต่อไป' if self.has_pending() and self.h.state.pending.payload['operation']=='set' else 'เลิกจำชื่อนี้'
            data['line_message']=render_actions(output['message'],[{'type':'postback','label':label,'data':'a1|'+review['ref']+'|'+review['nonce']},
                {'type':'postback','label':'ยกเลิก','data':'a1|cancel|'+review['nonce']}])
        return Result('preview' if review else 'answer' if output.get('ok') else 'clarification',output['message'],data)
    def control(self,text):
        exact=text.split('|') if text.startswith('a1|') else None
        if not self.has_pending():return Result('stale','ปุ่มชื่อเรียกหมดอายุแล้วครับ') if exact else None
        p=self.h.state.pending;v=p.payload
        if text not in {'ยืนยัน','ยืนยันครับ','ยืนยันค่ะ','ยืนยันคะ','ยกเลิก','ยกเลิกครับ'} and not exact:return None
        if (p.expires_at<=self.h.clock() or v.get('owner')!=self.h.settings.owner_id or v.get('scope')!=storage_scope(self.h.sheets)
            or v.get('ref')!=p.token or v.get('seal')!=digest({k:x for k,x in v.items() if k!='seal'})):
            return Result('stale','รายการชื่อเรียกเปลี่ยนหรือหมดอายุแล้วครับ')
        if exact and (len(exact)!=3 or not hmac.compare_digest(exact[2],v['nonce']) or exact[1] not in {v['ref'],'cancel'}):
            return Result('stale','ปุ่มชื่อเรียกไม่ตรงกับรายการนี้ครับ')
        if text.startswith('ยกเลิก') or exact and exact[1]=='cancel':
            self.h.state.pending=None;self.h.state.checkpoint();return Result('cancelled','ยกเลิกการเปลี่ยนชื่อเรียกแล้วครับ')
        row,shown=self.h._presented_result()
        if (not row or not shown or shown.data.get('catalog_alias_review')!={'ref':v['ref'],'nonce':v['nonce'],'seal':v['seal']}
            or datetime.fromisoformat(self.h._current['at'])<=max(datetime.fromisoformat(v['at']),datetime.fromisoformat(row['at']))):
            return Result('stale','ต้องแสดงชื่อเรียกให้ตรวจสอบอีกครั้งก่อนยืนยันครับ')
        if not self.enabled:return Result('setup_required','ยังไม่เปิดการเปลี่ยนชื่อเรียกครับ')
        blocked=self.h._blocked_action()
        if blocked:return self.h._uncertain(blocked)
        key=self.h.settings.owner_id+':catalogalias:'+digest([v['scope'],v['ref'],v['seal']])
        if key not in self.h.state.actions:
            self.h.state.actions[key]=Action('change_catalog_alias',deepcopy(v),
                {'catalog_alias':True,'owner':v['owner'],'scope':v['scope'],'payload_hash':digest(v),
                 'confirmed_at':self.h._current['at']},p.expires_at,
                self.h.clock()+timedelta(seconds=self.h.settings.webhook_ttl_seconds))
        self.h.state.claims[self.h._active_event_id].action_key=key;self.h.state.checkpoint()
        return self.resume(key)
    def resume(self,key):
        a=self.h.state.actions[key];v=a.payload
        confirmed=a.metadata.get('confirmed_at')
        if (a.kind!='change_catalog_alias' or a.metadata!={'catalog_alias':True,'owner':self.h.settings.owner_id,
            'scope':storage_scope(self.h.sheets),'payload_hash':digest(v),'confirmed_at':confirmed}
            or not isinstance(confirmed,str) or datetime.fromisoformat(confirmed).utcoffset() is None
            or datetime.fromisoformat(confirmed)<=datetime.fromisoformat(v['at'])):
            raise WriteBlocked('Owner vocabulary action binding changed')
        if a.status=='completed':return deepcopy(a.result)
        if not self.enabled or a.deadline<=self.h.clock():
            result=Result('setup_required','ชื่อเรียกหมดอายุหรือยังไม่เปิดใช้งานครับ ยังไม่ได้เปลี่ยนชื่อเรียก')
        else:
            try:
                key_name=normalized(v['alias'])
                if self.h.state.catalog_aliases.get(key_name)!=v['before']:raise ValueError('alias_changed')
                if v['operation']=='set':
                    self.check_pair(v['alias'],v['item'],catalog=self.h.sheets.taxonomy().catalog)
                    if key_name not in self.h.state.catalog_aliases and len(self.h.state.catalog_aliases)>=MAX_ALIASES:raise ValueError('alias_capacity')
                    self.h.state.catalog_aliases[key_name]={'alias':v['alias'],'item':v['item'],'source_refs':v['source_refs'],'confirmed_at':confirmed}
                    result=Result('recorded',f"จำแล้วครับ {v['alias']} หมายถึง {v['item']}",{'domain':'catalog_alias','catalog_alias_saved':True})
                else:
                    self.h.state.catalog_aliases.pop(key_name)
                    result=Result('recorded',f"เลิกจำชื่อเรียก {v['alias']} แล้วครับ",{'domain':'catalog_alias','catalog_alias_saved':True})
            except ValueError:result=Result('conflict','ชื่อเรียกหรือทะเบียนเปลี่ยนแล้วครับ กรุณาตรวจสอบใหม่ ยังไม่ได้เปลี่ยนชื่อเรียก')
        a.status='completed';a.result=deepcopy(result)
        if self.has_pending() and self.h.state.pending.token==v['ref']:self.h.state.pending=None
        self.h.state.owner_features_used=True;self.h._taxonomy_cache=None;self.h.state.checkpoint()
        return result
