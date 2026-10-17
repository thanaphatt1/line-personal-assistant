"""Synthetic decisions and transport; no inference or cloud access."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import json,socket
import pytest
from fastapi.testclient import TestClient
import behavior_support as ev
from line_assistant.state import Reply,Result
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.history_context import HistoryState
from line_assistant.history_first import HistoryFirstAssistant
from line_assistant.config import Settings
from test_history_first import response
from test_production_history import Hosted

@pytest.fixture(autouse=True)
def offline(monkeypatch):
 def deny(*a,**k):raise AssertionError('Local-only followthrough checks')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def known(*,plain=False):
 h=ev.Harness(ev.Port(),'known-read')
 if plain:
  original=h.service._result
  def result(*a,**kw):
   value=original(*a,**kw)
   if value.status=='preview':value=Result(value.status,value.message,{k:v for k,v in value.data.items() if k!='line_message'})
   return value
  h.service._result=result
 from test_expense_interface_v05 import prepare,read
 assert prepare(h,'กาแฟ 50 บาทเมื่อวาน',date_text='เมื่อวาน').status=='preview'
 before=deepcopy(h.state.drafts)
 result=read(h)
 assert result.data['tool_result']['read_only'] and h.state.drafts==before
 return h

@pytest.mark.parametrize('text',['โอเค บันทึกเลย','ยืนยันครับ'])
@pytest.mark.parametrize('plain',[False,True])
def test_synthetic_prepare_and_read_decisions_allow_explicit_text_save_once(text,plain):
 h=known(plain=plain);event,done,outcome=h.run_turn(text,0)
 assert done.status=='recorded' and outcome['calls']==0 and len(h.sheets.entries)==1
 assert h.sheets.entries[0].item=='กาแฟ' and h.sheets.entries[0].amount=='50'
 replay=ev.cold_replay(h,event,done)
 assert replay['identical_result'] and replay['raw_originals_retained']
 assert replay['model_calls']==replay['fake_rows_added']==replay['fake_catalog_batches_added']==0
 from test_read_queries import plan
 h.port.values=[plan(start='2026-10-13')]
 _,totals,_=h.run_turn('สรุปยอดเมื่อวาน',1)
 assert totals.data['tool_result']['expense']=='50.00'

def test_cold_restore_keeps_delivered_review_and_pure_read_focus():
 h=known();state=HistoryState();state.clock=lambda:h.now;state.enable_raw_history()
 for row in h.state.raw_journal.rows.values():state.raw_journal.put(deepcopy(row))
 restore_history(state,history_snapshot(h.state),'synthetic-workflow-owner')
 h.state=state;h.service=HistoryFirstAssistant(Settings(owner_id='synthetic-workflow-owner'),h.sheets,port=h.port,state=state,clock=lambda:h.now,
  history_first_enabled=True,item_enrollment_enabled=True,semantic_enabled=True,raw_history_enabled=True)
 assert h.run_turn('โอเค บันทึกเลย',0)[1].status=='recorded' and len(h.sheets.entries)==1

@pytest.mark.parametrize('invalid',['missing_preview','undelivered_preview','preview_hash','expired_preview','read_hash','read_payload','unreviewed','owner','catalog'])
def test_explicit_save_cannot_create_authority_from_invalid_or_changed_review(invalid):
 h=known();events=[r['event'] for r in h.state.history if r['role']=='assistant'];first,last=events[0],events[-1]
 d=next(iter(h.state.drafts.values()))
 if invalid=='missing_preview':h.state.receipts.pop(first)
 elif invalid=='expired_preview':h.state.receipts[first]=replace(h.state.receipts[first],expires_at=h.now-timedelta(seconds=1))
 elif invalid in {'undelivered_preview','preview_hash','read_hash'}:
  row=next(r for r in h.state.history if r['role']=='assistant' and r['event']==(last if invalid=='read_hash' else first))
  row['delivery_verified']=False if invalid=='undelivered_preview' else True;row['content_hash']='forged'
 elif invalid=='read_payload':h.state.receipts[last].result.data['tool_result']['entries'][0]['amount']='999'
 elif invalid=='unreviewed':d['review']=None;h.service._seal(d)
 elif invalid=='owner':d['owner_id']='foreign'
 elif invalid=='catalog':h.sheets.rows.append(('ขนม','Food/Drink','Food'))
 assert h.run_turn('โอเค บันทึกเลย',0)[1].status!='recorded' and not h.sheets.entries

@pytest.mark.parametrize('text',['ใช่','ใช่ครับ','โอเค บันทึกเลย แต่เปลี่ยนยอดเป็น 75 บาท','"โอเค บันทึกเลย"','ถ้าบันทึกเลยจะเกิดอะไรขึ้น'])
def test_generic_or_mixed_acknowledgment_after_read_does_not_save(text):
 h=known();assert h.run_turn(text,0)[1].status!='recorded' and not h.sheets.entries

@pytest.mark.parametrize('bad',['preview_reply','read_reply','preview_workbook','read_workbook'])
def test_production_carry_requires_sent_hashes_and_same_workbook_for_both_messages(bad):
 h=known();h.service.production=True;h.sheets.target=SimpleNamespace(spreadsheet_id='mock-book')
 events=[r['event'] for r in h.state.history if r['role']=='assistant'];first,last=events[0],events[-1]
 for row in h.state.history:
  if row['role']=='assistant':
   h.state.replies[row['event']]=Reply(row['content_hash'],h.now+timedelta(hours=1),status='sent')
   h.state.event_workbooks[row['event']]='mock-book'
 h.service._current['at']=(h.now+timedelta(seconds=1)).isoformat()
 h.service._active_event_id='synthetic-next-confirm'
 assert h.service._read_confirmable_preview() is not None
 event=first if bad.startswith('preview') else last
 if bad.endswith('reply'):h.state.replies[event].content_hash='wrong'
 else:h.state.event_workbooks[event]='foreign-book'
 assert h.service._read_confirmable_preview() is None and not h.sheets.entries

def test_signed_production_factory_plain_preview_read_explicit_save_and_recreated_replay(monkeypatch):
 original=HistoryFirstAssistant._result
 def plain(self,*a,**kw):
  result=original(self,*a,**kw)
  if result.status=='preview':return Result(result.status,result.message,{k:v for k,v in result.data.items() if k!='line_message'})
  return result
 monkeypatch.setattr(HistoryFirstAssistant,'_result',plain)
 api=Hosted()
 with TestClient(api.app(raw_history_enabled=True)) as client:
  preview=api.create(client,'ค่ากระดาษ 228 บาทวันที่ 10 ตุลาคม 2026',date_text='10 ตุลาคม 2026')
  assert preview.status=='preview' and 'line_message' not in preview.data
  def read(body):
   c=json.loads(body['input'][0]['content']);return response(name='read_expense_drafts',args={'draft_ref':c['available_drafts'][0]['draft_ref']})
  result,_=api.send_text(client,'ขอตรวจค่ากระดาษก่อนบันทึก',read)
  assert result.data['tool_result']['read_only'];calls=api.models
  done,event=api.send_text(client,'โอเค บันทึกเลย');assert done.status=='recorded' and api.models==calls
 with TestClient(api.app(raw_history_enabled=True)) as client:assert api.send(client,event).status_code==200
 assert api.google.sheet_writes==1 and api.models==calls
