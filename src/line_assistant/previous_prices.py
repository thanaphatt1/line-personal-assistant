"""Owner-selected past expense amounts; no unit-price inference or effects."""
from .expense_storage import storage_scope
from copy import deepcopy
from datetime import date,datetime
from decimal import Decimal
import re

from .catalog import normalized
from .history_context import digest
from .language import BANGKOK,intent_guard

FORMAT='previous-expense-price-v1'
FOREIGN=re.compile(r'\b(?:USD|EUR|GBP|JPY|CNY|SGD|AUD|CAD)\b|ดอลลาร์|ยูโร|เงินเยน|หยวน',re.I)
ACCEPT=re.compile(r'(?:ใช้|เอา|ตาม)(?:ราคา|ยอด|จำนวนเงิน)?\s*(?:เดิม|ก่อน|ครั้งก่อน)|เท่าเดิม',re.I)
NO=re.compile(r'ไม่|อย่า|ยกเลิก|พักไว้|\bcancel\b',re.I)
ALL=re.compile(r'ทั้งหมด|ทุกรายการ|ทั้งสอง|ทั้งคู่|ทั้งสาม')


def validate(entry):
    """Validate optional persisted metadata without loading provider data."""
    if not isinstance(entry,dict):raise ValueError('previous_price_entry')
    value=entry.get('previous_price')
    if value is None:return
    keys={'format','state','ref','item','amount','source_date','varied','source_hash','target_hash',
          'cutoff','offered_at','accepted_at','accepted_source_ref'}
    if not isinstance(value,dict) or set(value)!=keys or value['format']!=FORMAT:raise ValueError('previous_price_format')
    if value['state'] not in {'offered','accepted'} or value['item']!=entry['item']:raise ValueError('previous_price_identity')
    if (not isinstance(value['amount'],str) or not re.fullmatch(r'\d+\.\d{2}',value['amount'])
        or not 0<Decimal(value['amount'])<10000000 or type(value['varied']) is not bool):raise ValueError('previous_price_amount')
    if not isinstance(value['ref'],str) or not re.fullmatch(r'price_[a-f0-9]{20}',value['ref']):raise ValueError('previous_price_ref')
    if any(not isinstance(value[k],str) or not re.fullmatch(r'[a-f0-9]{64}',value[k]) for k in ['source_hash','target_hash']):raise ValueError('previous_price_binding')
    if any(not isinstance(value[k],str) for k in ['source_date','cutoff','offered_at']):raise ValueError('previous_price_date')
    if not date.fromisoformat(value['source_date'])<date.fromisoformat(value['cutoff']):raise ValueError('previous_price_date')
    if datetime.fromisoformat(value['offered_at']).utcoffset() is None:raise ValueError('previous_price_time')
    if value['state']=='offered':
        if entry['amount'] is not None or value['accepted_at'] is not None or value['accepted_source_ref'] is not None:raise ValueError('previous_price_not_selected')
    else:
        if (not isinstance(entry['amount'],str) or Decimal(entry['amount'])!=Decimal(value['amount'])
            or not isinstance(entry.get('source_refs'),list) or not isinstance(value['accepted_at'],str)
            or value['accepted_source_ref'] not in entry['source_refs']
            or datetime.fromisoformat(value['accepted_at']).utcoffset() is None):raise ValueError('previous_price_selection')


def public(value):
    return {k:deepcopy(value[k]) for k in ['ref','state','amount','source_date','varied']}


class PreviousPrices:
    def __init__(self,host,*,enabled=False):
        if type(enabled) is not bool or enabled and not host.raw_history_enabled:raise ValueError('previous_prices_require_raw_history')
        self.h=host;self.enabled=enabled;self._cache_event=None;self._cache=None

    def rows(self,*,fresh=False):
        event=self.h._active_event_id
        if fresh or event!=self._cache_event:
            self._cache=self.h.bound_sheets.classification_entries();self._cache_event=event
        return self._cache

    def candidate(self,item,cutoff,*,fresh=False):
        tax=self.h._taxonomy()
        if tax.catalog.match(item).status!='exact':return None
        rows=[r for r in self.rows(fresh=fresh) if normalized(r['item'])==normalized(item) and '1900-01-01'<=r['date']<cutoff]
        if not rows:return None
        latest=max(r['date'] for r in rows);recent=[r for r in rows if r['date']==latest]
        amounts={f"{Decimal(r['amount']):.2f}" for r in recent}
        # The Sheet does not encode billing plan, quantity or currency per row.
        # Do not reinterpret annotated totals or choose between same-day prices.
        if len(amounts)!=1 or any(r['note'].strip() for r in recent):return None
        amount=next(iter(amounts))
        if not 0<Decimal(amount)<10000000:return None
        return {'amount':amount,'source_date':latest,'varied':len({Decimal(r['amount']) for r in rows})>1,
                'source_hash':digest(sorted(digest(r) for r in rows))}

    def scoped_facts(self,entry):
        row=self.h._current;units=self.h._units([row],entry['item'],entry)
        from .expense_sources import roles
        values=[]
        for text,at,ref in units:
            if ref!=row['source_ref']:continue
            spans=[m.span() for m in self.h._item_spans(text,entry['item'])]
            facts=roles(text,at,spans)
            values.append((text,facts))
        return values

    def eligible_request(self,entry):
        if intent_guard(self.h._current['text']) or FOREIGN.search(self.h._current['text']):return False
        return not any(facts['money'] or facts['counts'] for text,facts in self.scoped_facts(entry))

    def explicit_amount(self,entry,amount):
        if amount is None or FOREIGN.search(self.h._current['text']):return False
        return any(len(facts['money'])==1 and facts['money'][0].value is not None
                   and Decimal(facts['money'][0].value)==Decimal(amount) for text,facts in self.scoped_facts(entry))

    def observe(self,output):
        if output.get('previous_price_lookup') is not None:return output
        if not self.enabled or not output.get('ok') or output.get('status')!='needs_information':return output
        d=self.h.state.drafts.get(output.get('draft_ref'))
        if not d:return output
        missing=[e for e in d['entries'] if e['amount'] is None and e['item'] and e['date']]
        if not missing:return output
        today=self.h.clock().astimezone(BANGKOK).date().isoformat();unavailable=False
        for entry in missing:
            entry.pop('previous_price',None)
            if sum(normalized(e['item'] or '')==normalized(entry['item']) for e in d['entries'])!=1 or not self.eligible_request(entry):continue
            cutoff=min(today,entry['date'])
            try:value=self.candidate(entry['item'],cutoff)
            except Exception:unavailable=True;continue
            if value is None:continue
            entry['previous_price']={'format':FORMAT,'state':'offered',
                'ref':'price_'+digest([d['draft_ref'],d['version'],entry['entry_ref'],value])[:20],
                'item':entry['item'],**value,'target_hash':digest(self.h.state.workbook_id or storage_scope(self.h.sheets)),
                'cutoff':cutoff,'offered_at':self.h._current['at'],'accepted_at':None,'accepted_source_ref':None}
            validate(entry)
        self.h._seal(d);self.h.state.checkpoint()
        return {**output,'entries':deepcopy(d['entries']),'previous_price_lookup':'unavailable' if unavailable else 'complete'}

    def selection(self,value,old):
        ref=value.previous_price_ref
        if ref is None:return None
        if not self.enabled or not old or not self.eligible_request(old):raise ValueError('previous_price_not_requested')
        offer=old.get('previous_price')
        if not offer or offer['state']!='offered' or offer['ref']!=ref:raise ValueError('previous_price_not_current')
        validate(old)
        if value.item is not None and normalized(value.item)!=normalized(old['item']):raise ValueError('previous_price_identity')
        if value.amount is not None and Decimal(str(value.amount))!=Decimal(offer['amount']):raise ValueError('previous_price_amount_conflict')
        _,shown=self.h._presented_result();out=shown.data.get('tool_result',{}) if shown else {}
        d=next((d for d in self.h.state.drafts.values() if any(e['entry_ref']==old['entry_ref'] for e in d['entries'])),None)
        if (not d or out.get('draft_ref')!=d['draft_ref'] or out.get('draft_version')!=d['version']
            or not any(e.get('previous_price')==offer for e in out.get('entries',[]))):raise ValueError('previous_price_not_delivered_latest')
        from .line_api import visible_text
        visible=visible_text(shown)
        if not self.h._has_item(visible,old['item']) or offer['amount'] not in visible or offer['source_date'] not in visible:raise ValueError('previous_price_not_visible')
        text=self.h._current['text'].strip()
        if NO.search(text) or not (ACCEPT.search(text) or re.fullmatch(r'(?:ใช่|ได้|ตกลง)(?:ครับ|ค่ะ|คะ)?',text)):raise ValueError('previous_price_owner_choice_required')
        active=[x for x in self.h.state.drafts.values() if x['status']!='recorded']
        unresolved=[e for x in active for e in x['entries'] if e['amount'] is None]
        named=self.h._has_item(text,old['item'])
        all_refs={e['previous_price']['ref'] for e in d['entries'] if e['amount'] is None and e.get('previous_price')}
        selected=getattr(self.h,'_previous_price_refs',set())
        all_selected=bool(ALL.search(text) and len(active)==1 and len(all_refs)==sum(e['amount'] is None for e in d['entries']) and selected==all_refs)
        if not named and len(unresolved)!=1 and not all_selected:raise ValueError('previous_price_ambiguous_target')
        self.check(offer,old,fresh=True)
        return {**deepcopy(offer),'state':'accepted','accepted_at':self.h._current['at'],'accepted_source_ref':self.h._current['source_ref']}

    def redisplay(self,output):
        if not self.enabled or not output.get('ok') or len(output.get('drafts',[]))!=1:return output
        view=output['drafts'][0];d=self.h.state.drafts.get(view['draft_ref'])
        if not d or not any(e.get('previous_price',{}).get('state')=='offered' for e in d['entries']):return output
        refreshed=self.observe({**output,'status':'needs_information','draft_ref':d['draft_ref'],
            'draft_version':d['version'],'entries':deepcopy(d['entries']),'missing_fields':deepcopy(d['missing_fields'])})
        return {**refreshed,'drafts':[v for v in self.h._workflow() if v['draft_ref']==d['draft_ref']]}

    def allows_bare_selection(self,p,d):
        if not self.enabled or not p.entries or any(not e.previous_price_ref or any(getattr(e,k) is not None for k in ['item','amount','date','date_text','date_ref','date_context','note','category','subcategory']) for e in p.entries):return False
        active=[v for v in self.h.state.drafts.values() if v['status']!='recorded']
        if len(active)!=1:return False
        unresolved=[e for e in d['entries'] if e['amount'] is None]
        refs={e['previous_price']['ref'] for e in unresolved if e.get('previous_price')}
        selected={e.previous_price_ref for e in p.entries}
        return bool(len(unresolved)==len(refs) and selected==refs and (len(refs)==1 or ALL.search(self.h._current['text'])))

    def check(self,proof,entry,*,fresh=False):
        if not self.enabled:raise ValueError('previous_price_disabled')
        if proof['target_hash']!=digest(self.h.state.workbook_id or storage_scope(self.h.sheets)):raise ValueError('previous_price_target_changed')
        if entry['item']!=proof['item'] or entry.get('date') and entry['date']<=proof['source_date']:raise ValueError('previous_price_date_or_item_changed')
        current=self.candidate(entry['item'],proof['cutoff'],fresh=fresh)
        if not current or any(current[k]!=proof[k] for k in ['amount','source_date','source_hash','varied']):raise ValueError('previous_price_source_changed')

    def bases(self,d):
        result=[]
        for i,e in enumerate(d['entries']):
            proof=e.get('previous_price')
            if proof and proof['state']=='accepted':
                validate(e);self.check(proof,e,fresh=not result)
                result.append({'index':i,'proof':deepcopy(proof)})
        return result

    def check_action(self,action):
        for row in action.metadata.get('previous_price_bases',[]):
            entry=action.payload['entries'][row['index']];proof=row['proof']
            if Decimal(entry['amount'])!=Decimal(proof['amount']):raise ValueError('previous_price_action_changed')
            self.check(proof,entry,fresh=True)

    @staticmethod
    def prompt(output):
        lines=[]
        for entry in output.get('entries',[]):
            value=entry.get('previous_price')
            if value and value['state']=='offered':
                label=entry['item'].replace('\n',' ')[:200]
                lines.append(f"{label}: ยอดที่เคยบันทึกล่าสุด {value['amount']} บาท วันที่ {value['source_date']}"+(' (ประวัติมีหลายยอด)' if value['varied'] else ''))
        if not lines:return None
        return 'ยังไม่ได้ระบุยอดและยังไม่ได้บันทึก:\n'+'\n'.join(lines)+'\nจะใช้ยอดเดิมที่แสดงนี้ หรือระบุยอดใหม่ครับ? ถ้ามีหลายรายการ กรุณาระบุชื่อหรือเลือกทั้งหมด\nเมื่อเลือกแล้วจะมีร่างให้ตรวจและยืนยันอีกครั้ง'
