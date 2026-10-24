"""Delivered-record buttons select stable IDs; only a separate review can write."""
from copy import deepcopy
from datetime import datetime,timedelta
from urllib.parse import quote
import hmac,re,secrets

from .conversation_ui import render_actions,utf16_limit
from .expense_storage import storage_scope
from .history_context import digest
from .line_api import message_hash,rendered_message
from .sheet_metadata import transaction_id
from .state import Result
from .adapters import Conflict,NotFound
from .transport import ApiError,NotReady,WriteBlocked

LABELS={'amount':'แก้ยอด','date':'เปลี่ยนวันที่','source':'ดูต้นทาง','delete':'นำรายการออก'}


class RecordActions:
    def __init__(self,host,*,enabled=False):self.h=host;self.enabled=enabled
    def is_control(self,text):return text.startswith('r1|')
    def attach(self,event,result):
        if not self.enabled or result.data.get('record_actions') or result.data.get('domain')=='catalog_alias':return result
        claim=self.h.state.claims.get(event);action=self.h.state.actions.get(claim.action_key) if claim else None
        if not action or action.status!='completed':return result
        records=[]
        if result.status=='recorded' and action.kind in {'add_transactions','catalog_expense'}:
            if not getattr(self.h.sheets.target,'transaction_id_metadata',None) and getattr(self.h.sheets,'backend',None)!='firestore':return result
            records=[{'id':transaction_id(claim.action_key,i),**{k:e[k] for k in ('item','amount','date')}} for i,e in enumerate(action.payload['entries'])]
        elif result.status=='updated' and action.kind=='edit_saved_expense':
            records=[{'id':action.payload['id'],**action.payload['after']}]
        if not records:return result
        value={'owner':self.h.settings.owner_id,'scope':storage_scope(self.h.sheets),'event':event,
            'ref':'record_'+digest([event,claim.action_key])[:20],'nonce':secrets.token_hex(8),'records':records,
            'expires_at':(self.h.clock()+timedelta(seconds=self.h.settings.last_action_ttl_seconds)).isoformat()}
        value['seal']=digest(value)
        operations=['amount','date','source',*(['delete'] if self.h.saved_edits.delete_enabled else [])]
        actions=[self.button(op,value,'*' if len(records)>1 else '0') for op in operations]
        offer=self.alias_offer(action) if result.status=='recorded' else None
        if offer:actions.append(offer)
        self.h.state.owner_features_used=True
        return Result(result.status,result.message,{**result.data,'record_actions':value,'line_message':render_actions(result.message,actions)})
    def alias_offer(self,action):
        """Suggest remembering a chosen spelling. The button only sends owner text; the alias review still decides."""
        aliases=getattr(self.h,'catalog_aliases',None)
        draft=self.h.state.drafts.get(action.metadata.get('draft_ref'))
        if not aliases or not aliases.enabled or not draft:return None
        try:catalog=self.h._taxonomy().catalog
        except Exception:return None
        from .catalog import normalized
        for entry in draft['entries']:
            chosen=entry.get('item_resolution')
            if not chosen or normalized(chosen['from'])==normalized(chosen['to']) or catalog.identity(chosen['from']).canonical:continue
            text=f"จำว่า {chosen['from']} คือ {chosen['to']}"
            if len(text)<=300:return {'type':'message','label':utf16_limit('จำชื่อ '+chosen['from'],20),'text':text}
        return None
    def button(self,operation,value,index,*,label=None):
        return {'type':'postback','label':utf16_limit(label or LABELS[operation],20),
            'displayText':utf16_limit(label or LABELS[operation],100),
            'data':'|'.join(['r1',operation,value['ref'],str(index),value['nonce']])}
    def delivered(self,ref,nonce):
        store=getattr(self.h.state,'record_store',None)
        if store is not None and store.mode=='ready':store.prefetch_receipt_ref(ref)
        for event,receipt in self.h.state.receipts.items():
            value=receipt.result.data.get('record_actions')
            if not value or value.get('ref')!=ref:continue
            if (receipt.expires_at<=self.h.clock() or value.get('owner')!=self.h.settings.owner_id
                or value.get('scope')!=storage_scope(self.h.sheets) or value.get('event')!=event
                or value.get('seal')!=digest({k:v for k,v in value.items() if k!='seal'})
                or datetime.fromisoformat(value['expires_at'])<=self.h.clock()
                or not hmac.compare_digest(value['nonce'],nonce)):raise ValueError('stale_record_button')
            if getattr(self.h,'production',False):
                reply=self.h.state.replies.get(event)
                if (not reply or reply.status!='sent' or reply.content_hash!=message_hash(rendered_message(receipt.result))
                    or self.h.state.event_workbooks.get(event)!=storage_scope(self.h.sheets)):raise ValueError('undelivered_record_button')
            elif not any(r['role']=='assistant' and r['event']==event for r in self.h.state.history):raise ValueError('undelivered_record_button')
            return value
        raise ValueError('stale_record_button')
    def control(self,text):
        if not self.is_control(text):return None
        if not self.enabled or self.h._active_event_kind!='postback':return Result('stale','ปุ่มรายการนี้ยังใช้ไม่ได้หรือหมดอายุแล้วครับ')
        try:
            parts=text.split('|')
            if len(parts)!=5:raise ValueError('invalid_record_button')
            _,operation,ref,index,nonce=parts
            if operation not in LABELS or not re.fullmatch(r'record_[a-f0-9]{20}',ref) or not re.fullmatch(r'[a-f0-9]{16}',nonce):raise ValueError('invalid_record_button')
            if not self.h.saved_edits.flags['expense'] or operation=='delete' and not self.h.saved_edits.delete_enabled:raise ValueError('disabled_record_action')
            value=self.delivered(ref,nonce);records=value['records']
            selection={'ref':ref,'operation':operation,'nonce':nonce}
            if index=='*':
                if len(records)<2:raise ValueError('invalid_record_selection')
                message=LABELS[operation]+'รายการไหนครับ?\n'+'\n'.join(f"{i}. {r['item']} — {r['amount']} บาท วันที่ {r['date']}" for i,r in enumerate(records,1))
                actions=[self.button(operation,value,str(i),label=f"{i+1}. {r['item']}") for i,r in enumerate(records)]
                return Result('clarification',message,{'record_action_selection':selection,'line_message':render_actions(message,actions)})
            if not index.isascii() or not index.isdecimal() or str(int(index))!=index or not 0<=int(index)<len(records):raise ValueError('invalid_record_selection')
            if len(records)>1:
                _,shown=self.h._presented_result()
                if not shown or shown.data.get('record_action_selection')!=selection:raise ValueError('undelivered_record_selection')
            record=self.h.saved_edits.editor('expense').get(records[int(index)]['id'])
            if operation=='source':return self.source(record)
            output=self.h.saved_edits.select_records([record],'expense',[self.h._current['source_ref']],
                {'domain':'expense','scope':'record_button'},requested_field=operation)
            if operation=='delete':
                output=self.h.saved_edits.prepare_delete({'candidate_ref':output['candidates'][0]['candidate_ref'],'source_refs':[self.h._current['source_ref']]},owner_button=True)
                return self.h.saved_edits.result(output)
            result=self.h.saved_edits.result(output)
            question='ต้องการเปลี่ยนยอดเป็นเท่าไรครับ?' if operation=='amount' else 'ต้องการเปลี่ยนเป็นวันที่เท่าไรครับ?'
            from .reply_display import day
            message=f"{record['item']} — {record['amount']} บาท วันที่ {day(record['date'])}\n"+question
            return Result('clarification',message,{**result.data,'record_action_focus':operation})
        except (ValueError,Conflict,NotFound,ApiError,NotReady,WriteBlocked):
            # Buttons never fall through to the LLM as fabricated owner facts.
            return Result('stale','ยังเปิดรายการนี้ไม่ได้ครับ รายการหรือปุ่มอาจเปลี่ยนหรือหมดอายุ กรุณาค้นหารายการล่าสุดอีกครั้ง')
    def source(self,record):
        sheets=self.h.sheets
        from .reply_display import day
        message=f"ต้นทางของ {record['item']} — {record['amount']} บาท วันที่ {day(record['date'])}"
        if getattr(sheets,'backend',None)=='firestore':
            message+='\nฐานข้อมูลรายจ่าย '+sheets.target.dataset_id+'\nรหัสรายการ: '+record['id']
            url='https://console.cloud.google.com/firestore/databases?project='+quote(sheets.target.project_id,safe='')
            label='เปิด Firestore'
        else:
            row=record['row'];target=sheets.target
            url='https://docs.google.com/spreadsheets/d/'+quote(target.spreadsheet_id,safe='')+'/edit#gid='+str(target.expense_sheet_id)+'&range=A'+str(row)+':D'+str(row)
            message+='\nชีต '+target.expense_tab+' แถวที่ '+str(row);label='เปิดใน Google Sheets'
        return Result('answer',message,{'domain':'record_source','read_only':True,
            'line_message':render_actions(message,[{'type':'uri','label':label,'uri':url}])})
