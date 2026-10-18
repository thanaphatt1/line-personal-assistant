"""History routing, legacy pending/new-name fallback and retaining rollback."""
import re
from .history_context import digest
from .history_first import HistoryFirstAssistant
from .cloud_run import ExpenseAssistant
from .conversation import ConversationAssistant
from .state import Result

class ProductionHistoryAssistant(HistoryFirstAssistant,ExpenseAssistant):
    supports_postback=True
    def bind_legacy(self,legacy,*,history_enabled):
        self.legacy=legacy;self.history_enabled=history_enabled;self._legacy_turn=False
    def _legacy_route(self,text,event,stamp,at):
        self._legacy_turn=True;self.state.append('user',event,at,text=text)
        self.legacy._active_event_id=event;self.legacy._active_event_kind=self._active_event_kind
        text=re.sub(r'^เพิ่มรายการใหม่\s+','',text)
        return self.legacy._route(text,event,stamp,at)
    def _route(self,text,event,stamp,at):
        self._legacy_turn=False
        if self.historical_categories.has_pending() or self.historical_categories.is_control(text):return super()._route(text,event,stamp,at)
        if self.provisional_categories.has_pending() or self.provisional_categories.is_control(text):return super()._route(text,event,stamp,at)
        if self.saved_edits.has_pending() or self.saved_edits.is_control(text):return super()._route(text,event,stamp,at)
        if self.calendar_flow.batches.is_control(text) or self.calendar_flow.enabled and self.calendar_flow.batches.refresh_requested(text):
            # Batch postbacks and named text consent must reach Calendar even
            # when legacy expense work is pending. Its own owner/version/nonce
            # and delivered-review checks still authorize every effect.
            return super()._route(text,event,stamp,at)
        calendar_active=any(d['status'] not in {'recorded','cancelled'} for d in self.state.calendar_drafts.values())
        if (self.calendar_flow.enabled or self.state.calendar_drafts) and (
            re.fullmatch(r'ตรวจ(?:สอบ)?ผลนัด(?:ล่าสุด)?',text)
            or calendar_active and re.fullmatch(r'(?:ยืนยัน|ยกเลิก)(?:\s*(?:ครับ|ค่ะ|คะ))?',text)
            or re.fullmatch(r'ยกเลิก(?:ร่าง)?นัด(?:ล่าสุด|นี้)?',text)):
            return super()._route(text,event,stamp,at)
        if self._active_event_kind=='postback' and text.startswith('c1|') or re.fullmatch(r'ยืนยันนัด cal_[a-f0-9]{20} [a-f0-9]{16}',text):
            return super()._route(text,event,stamp,at)
        if re.fullmatch(r'(?:ยืนยัน draft_[a-f0-9]{20} [a-f0-9]{16}|ยกเลิก draft_[a-f0-9]{20})',text):
            return super()._route(text,event,stamp,at)
        if self.semantic_enabled and self._active_event_kind=='postback' and text.startswith('h2|'):
            return super()._route(text,event,stamp,at)
        if self._active_event_kind=='postback' or self.state.pending:
            return self._legacy_route(text,event,stamp,at)
        active=[d for d in self.state.drafts.values() if d['status']!='recorded']
        if not self.history_enabled:
            if active and (re.match(r'^(?:แก้|เปลี่ยน|ล้าง|บันทึก|ยืนยัน)',text) or any(d['draft_ref'] in text for d in active)):
                lines=['ร่างเดิมยังอยู่ กรุณายืนยันด้วยคำสั่งของร่าง หรือยกเลิกร่างก่อนเริ่มใหม่']
                for d in active:
                    if d['review']:lines.append('ยืนยัน '+d['draft_ref']+' '+d['review']['nonce'])
                    lines.append('ยกเลิก '+d['draft_ref'])
                return Result('clarification','\n'.join(lines))
            return self._legacy_route(text,event,stamp,at)
        if not self.saved_edits.enabled and not active and not calendar_active and re.match(r'^(?:แก้|เปลี่ยน|ล้าง)',text):
            return Result('clarification','ไม่พบร่างที่ยังแก้ไขได้ กรุณาระบุรายการใหม่หากต้องการเพิ่มรายจ่าย')
        return super()._route(text,event,stamp,at)
    def _result(self,output,model_text=''):
        result=super()._result(output,model_text)
        output=result.data.get('tool_result',output)
        ref=output.get('draft_ref') if isinstance(output,dict) else None
        pending=self.state.drafts.get(ref)
        if output and pending and pending['status']!='recorded' and not output.get('item_enrollment') and (any('item_resolution' in f or 'existing_or_new' in f for f in output.get('missing_fields',[]))
                       or output.get('matches')==[]):
            return Result('clarification',result.message+'\nถ้าต้องการเพิ่มชื่อนี้ บอกได้เลยครับ จะใช้ข้อมูลร่างเดิมแล้วถามเฉพาะหมวดที่ยังขาด',result.data)
        return result
    def _receipt(self,event,fingerprint,result):
        if getattr(self,'_legacy_turn',False):
            run=self.state.model_runs.get(event)
            if run and run.status=='completed':run.decision=None
        for step in range(2):
            run=self.state.model_runs.get('history01_'+digest([event,step]))
            if run and run.status=='completed':run.decision=None
        # Receipt and consumed proposal caches commit together; fingerprints,
        # reservations and every unknown attempt remain retained.
        return super()._receipt(event,fingerprint,result)
