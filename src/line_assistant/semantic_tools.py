"""Owner-scoped semantic preparation/read tools; model calls cannot grant writes."""
from .expense_storage import storage_scope
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
import secrets,re
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from .contracts import SpendingSummary
from .history_context import digest,WINDOW,draft_fingerprint
from .history_dates import cues,resolve
from .conversation_evidence import command_text
from .conversation_ui import render_actions
from .line_api import message_hash,rendered_message,visible_text
from .language import amounts
from .state import Result

class Control(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    operation:Literal['review','confirm','cancel']
    draft_ref:str|None=None
    draft_version:int|None=Field(default=None,ge=1)

class Summary(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    start_date:str
    end_date:str
    source_refs:list[str]|None=Field(default=None,max_length=16)

def compact(output):
    def entry(e):return {k:v for k,v in e.items() if k in {'entry_ref','item','amount','date','note'} or k in {'date_parts','mapping'} and v}
    result={k:deepcopy(v) for k,v in output.items() if k not in {'entries','drafts','cancelled_draft'}}
    if 'entries' in output:result['entries']=[entry(e) for e in output['entries']]
    if 'drafts' in output:result['drafts']=[{**{k:deepcopy(v) for k,v in d.items() if k!='entries'},'entries':[entry(e) for e in d['entries']]} for d in output['drafts']]
    return result

def summary(h,args):
    p=Summary.model_validate(args)
    period=SpendingSummary.model_validate({'start_date':p.start_date,'end_date':p.end_date})
    if not all(1900<=d.year<=2100 for d in [period.start_date,period.end_date]):raise ValueError('summary_date_bounds')
    sources=h._owner_sources(p.source_refs) if p.source_refs else [h._current]
    # Recognized explicit single-day evidence must agree. Unrecognized natural
    # period language is handled by the model, then bounded typed validation.
    for row in sources:
        at=datetime.fromisoformat(row['at']);parts,_,issue=cues(command_text(row['text']),at)
        if parts and not issue and period.start_date==period.end_date:
            value,_,_,problem=resolve([(row['text'],at,row['source_ref'])],None,row['source_ref'],at)
            if not problem and value!=period.start_date.isoformat():raise ValueError('summary_date_evidence_mismatch')
    totals=h.sheets.summary(period.start_date,period.end_date)
    from .read_queries import summary_output
    return summary_output(totals,period.model_dump(mode='json'))

def confirmed_date(h):
    rows=sorted((r for r in h.state.history if r['role']=='assistant' and datetime.fromisoformat(r['at'])>h.clock()-WINDOW),key=lambda r:r['at'],reverse=True)
    for row in rows:
        receipt=h.state.receipts.get(row['event']);claim=h.state.claims.get(row['event'])
        action=h.state.actions.get(claim.action_key) if claim else None
        if not receipt or receipt.result.status!='recorded' or not action or action.status!='completed' or action.result!=receipt.result:continue
        if action.kind not in {'add_transactions','catalog_expense'}:continue
        dates={e['date'] for e in action.payload['entries']}
        return {'date':dates.pop(),'confirmed_event':row['event']} if len(dates)==1 else None
    return None

def date_targets(h):
    value=confirmed_date(h)
    if not value:return []
    if getattr(h,'production',False):
        event=value['confirmed_event'];reply=h.state.replies.get(event);receipt=h.state.receipts.get(event)
        if (h.state.event_workbooks.get(event)!=storage_scope(h.sheets) or not reply or reply.status!='sent'
            or not receipt or reply.content_hash!=message_hash(rendered_message(receipt.result))):return []
    return [{'date_ref':'date_'+digest([h.settings.owner_id,getattr(getattr(h.sheets,'target',None),'spreadsheet_id',None),value])[:20],**value}]

def inspect_drafts(h,ref=None):
    drafts=[h._draft(k) for k,d in h.state.drafts.items() if d['status']!='recorded' and (ref is None or k==ref)]
    if ref and not drafts:raise ValueError('draft_unavailable')
    selected={d['draft_ref'] for d in drafts}
    result={'ok':True,'status':'draft_read','read_only':True,'confirmation_required':False,'financial_write_authorized':False,
        'entries':[deepcopy(e) for d in drafts for e in d['entries']],
        'drafts':[{**v,'draft_version':v['version'],
            'category_selection_required':any(r['step'] in {'choose_category','choose_subcategory'} for r in v['item_resolutions']),
            'item_resolution_required':any(r['step']=='choose_existing_or_new' for r in v['item_resolutions'])}
            for v in h._workflow() if v['draft_ref'] in selected]}
    if h.state.rolling_summary:
        from . import raw_history as raw
        summary=raw.verified_summary(h.state,h.state.rolling_summary,h.clock(),production=getattr(h,'production',False))
        result.update(history_summary=summary['summary'],history_source_refs=summary['source_refs'])
    return result

def action(ref,version,token,op,label):
    return {'type':'postback','label':label,'data':'|'.join(['h2',ref,str(version),token,op]),'displayText':label}

def actions(h,d):
    nonce=d['review']['nonce']
    return [action(d['draft_ref'],d['version'],nonce,'save','บันทึก'),action(d['draft_ref'],d['version'],nonce,'cancel','ยกเลิกร่างนี้')]

def delivered(h,payload):
    for event,receipt in h.state.receipts.items():
        if receipt.expires_at<=h.clock():continue
        msg=receipt.result.data.get('line_message',{})
        offered=[v.get('action',{}).get('data') for v in msg.get('contents',{}).get('footer',{}).get('contents',[])]
        if payload not in offered:continue
        if getattr(h,'production',False):
            if h.state.event_workbooks.get(event)!=storage_scope(h.sheets):continue
            reply=h.state.replies.get(event)
            if not reply or reply.status!='sent' or reply.content_hash!=message_hash(rendered_message(receipt.result)):continue
        return True
    return False

def postback(h,payload):
    parts=payload.split('|')
    if len(parts)!=5 or parts[0]!='h2':return Result('stale','ปุ่มนี้ใช้ไม่ได้แล้ว กรุณาตรวจร่างล่าสุด')
    _,ref,version,token,op=parts
    try:version=int(version)
    except ValueError:return Result('stale','ปุ่มนี้ใช้ไม่ได้แล้ว กรุณาตรวจร่างล่าสุด')
    try:d=h._draft(ref)
    except (ValueError,KeyError):
        # A consumed owner-delivered control is not an undo instruction.
        # Reconcile it from owned receipts/actions before using the generic
        # expiry response; forged or foreign controls reveal no saved facts.
        if op in {'save','cancel'} and delivered(h,payload):
            d=h.state.drafts.get(ref);proof=h.state.draft_proofs.get(ref)
            if d and d['status']=='recorded' and d['version']==version and d.get('owner_id')==h.settings.owner_id and proof and proof.get('fingerprint')==draft_fingerprint(d):
                actions=[a for a in h.state.actions.values() if a.metadata.get('draft_ref')==ref and a.status=='completed' and a.result and a.result.status=='recorded']
                if actions:
                    rows='\n'.join(f"{e['item']} {e['amount']} บาท วันที่ {e['date']}" for e in d['entries'])
                    return Result('stale','รายการนี้บันทึกไปแล้ว\n'+rows+('\nการยกเลิกร่างไม่ลบรายการที่บันทึกแล้ว' if op=='cancel' else '\nไม่ได้บันทึกซ้ำ'),{'resolved_outcome':'recorded','financial_effects':0})
            for receipt in h.state.receipts.values():
                cancelled=receipt.result.data.get('tool_result',{}).get('cancelled_draft') if receipt.result.status=='cancelled' else None
                if cancelled and cancelled.get('draft_ref')==ref and cancelled.get('owner_id')==h.settings.owner_id and cancelled.get('version')==version:
                    return Result('stale','ร่างนี้ถูกยกเลิกแล้ว จึงไม่ได้บันทึกจากปุ่มนี้',{'resolved_outcome':'cancelled','financial_effects':0})
        return Result('stale','ร่างนี้หมดอายุหรือเปลี่ยนแล้ว')
    if d['version']!=version or not delivered(h,payload):return Result('stale','ปุ่มนี้ไม่ตรงกับร่างที่ส่งให้ตรวจล่าสุด')
    if datetime.fromisoformat(h._current['at'])<=datetime.fromisoformat(d['updated_at']):return Result('stale','การเลือกนี้เกิดก่อนร่างล่าสุด')
    if op=='select':
        if token!=h._view(d)['fingerprint'][:16]:return Result('stale','ร่างนี้เปลี่ยนแล้ว')
        return h._result(review(h,d))
    if op not in {'save','cancel'} or not d.get('review') or token!=d['review']['nonce']:
        return Result('stale','ปุ่มนี้ไม่ตรงกับการตรวจข้อมูลล่าสุด')
    if op=='save':return h._confirm(ref,token)
    blocked=h._blocked_action()
    if blocked:return h._uncertain(blocked)
    return h._result(cancel(h,d))

def choose(h):
    drafts=[h._draft(k) for k,d in h.state.drafts.items() if d['status']!='recorded']
    return {'ok':True,'status':'choose_draft','targets':[{'draft_ref':d['draft_ref'],'draft_version':d['version'],
            'token':h._view(d)['fingerprint'][:16],'items':[display_item(e.get('item')) for e in d['entries']]} for d in drafts]}

def display_item(value):
    return value if isinstance(value,str) and value.strip() else 'ยังไม่มีชื่อ'

def review(h,d):
    tax=h.sheets.taxonomy()
    if d['missing_fields']:return h._enrollment_result(d)
    for e in d['entries']:
        mapping=e['mapping']
        if mapping:
            if (mapping['category'],mapping['subcategory']) not in {(r.category,r.subcategory) for r in tax.rows}:raise ValueError('taxonomy_changed')
        elif tax.catalog.match(e['item']).status!='exact':raise ValueError('item_resolution_required')
    fresh=deepcopy(d);fresh.update(version=d['version']+1,updated_at=h._current['at'],
                                  catalog_hash=digest([[r.item,r.category,r.subcategory] for r in tax.rows]))
    fresh['review']={'nonce':secrets.token_hex(8),'payload_hash':digest(h._payload(fresh)),
                     'catalog_hash':fresh['catalog_hash'],'at':h._current['at']}
    h.state.drafts[fresh['draft_ref']]=fresh;h._seal(fresh)
    return h._enrollment_result(fresh)

def cancel(h,d):
    h.state.drafts.pop(d['draft_ref']);h.state.draft_proofs.pop(d['draft_ref'],None)
    # Keep the cancelled facts in the server receipt/history for recovery; no
    # provider or completed/uncertain action ledger is removed.
    return {'ok':True,'status':'cancelled','items':[display_item(e.get('item')) for e in d['entries']],
            'cancelled_draft':deepcopy(d)}

def control(h,args):
    p=Control.model_validate(args);active=[d for d in h.state.drafts.values() if d['status']!='recorded']
    if not active:return {'ok':True,'status':'no_pending'}
    d=h._draft(p.draft_ref) if p.draft_ref else h._draft(active[0]['draft_ref']) if len(active)==1 else None
    if d is None:return choose(h)
    h._bound_update(SimpleNamespace(draft_ref=d['draft_ref'],draft_version=p.draft_version or d['version']),d)
    if datetime.fromisoformat(h._current['at'])<=datetime.fromisoformat(d['updated_at']):raise ValueError('older_draft_update')
    _,shown=h._presented_result();view=shown.data.get('tool_result',{}) if shown else {}
    focused=view.get('draft_ref')==d['draft_ref'] and view.get('draft_version')==d['version']
    named=any(e['item'] and h._has_item(command_text(h._current['text']),e['item']) for e in d['entries'])
    if len(active)>1 and not focused and not named:return choose(h)
    blocked=h._blocked_action()
    if blocked:return {'ok':False,'error':'uncertain_write','clarification_required':True}
    if p.operation=='cancel':return cancel(h,d)
    if p.operation=='confirm' and not any(v['call']['name']=='prepare_expenses' and v['result'].get('draft_ref')==d['draft_ref'] for v in h.trace):
        source=command_text(h._current['text'])
        _,ds,problem=cues(source,datetime.fromisoformat(h._current['at']))
        names=[m.span() for e in d['entries'] if e['item'] for m in h._item_spans(source,e['item'])]
        values=amounts(source,(*ds,*names))
        if problem or ds or values and (len(values)!=1 or len(d['entries'])!=1 or values[0].value!=d['entries'][0]['amount']):
            return {'ok':False,'error':'changes_need_review','clarification_required':True}
    # Model "confirm" means present the bound owner action, never execute it.
    return review(h,d)

def result(h,output):
    status=output.get('status')
    if status=='cancelled':return Result('cancelled','ยกเลิกร่าง '+', '.join(display_item(v) for v in output['items'])+' แล้ว ยังไม่มีการลบรายการที่บันทึกไว้',{'tool_result':output})
    if status=='no_pending':return Result('no_pending','ไม่มีร่างที่ค้างอยู่',{'tool_result':output})
    if status=='choose_draft':
        targets=output['targets'];buttons=[action(t['draft_ref'],t['draft_version'],t['token'],'select',str(i+1)+'. '+', '.join(display_item(v) for v in t['items'])[:16]) for i,t in enumerate(targets)]
        text='ต้องการร่างไหนครับ?\n'+'\n'.join(str(i+1)+'. '+', '.join(display_item(v) for v in t['items']) for i,t in enumerate(targets))
        return Result('clarification',text,{'tool_result':output,'line_message':render_actions(text,buttons)})
    return None
