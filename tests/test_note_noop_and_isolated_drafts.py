"""Runtime isolates with valid scripted seeds; these are not new LLM evidence."""
from copy import deepcopy
from pathlib import Path
import socket
import pytest
from line_assistant.adapters import SheetLayout
from line_assistant.conversation_mocks import ConversationSheets
from line_assistant.domain_runtime import ToolError
from line_assistant.draft_patch import DraftPatchAssistant
from test_draft_patch import Harness,patch,change,full,expected,envelope
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def offline(monkeypatch):
 def deny(*a,**k):raise AssertionError('No network for isolated runtime checks')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

TEXTS = {
 'A1':'รถรางทดสอบ 29 บาท วันที่ 8 หมายเหตุ เดินทางไปห้องทดสอบ',
 'A2':'กันยายน', 'A3':'วันที่ 10 กันยายน 2026', 'A4':'ล้างหมายเหตุเดิม',
 'B1':'สมุดลายจุดทดสอบ 47 บาท','B2':'หมวด Office','B3':'สรุปรายจ่ายวันนี้',
 'C1':'แฟ้มทดสอบ 75 บาท หมายเหตุ เก็บป้ายโปรไว้','C2':'หมายเหตุ สำหรับงานทดสอบ',
 'D1':'วันที่ 8 กันยายน 2026',
}
def row(case):return {'text':TEXTS[case]}
def harness():
 from test_draft_patch import ROWS
 rows=[*ROWS,('รถรางทดสอบ','Transportation',''),('แฟ้มทดสอบ','Work/Education',''),('วัสดุทดสอบ','Office','')]
 return Harness(sheets=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),rows))

def seed(case):
 h=harness()
 if case.startswith('A'):
  result=h.send(row('A1')['text'],patch(item='รถรางทดสอบ',amount=29,day=8,note='เดินทางไปห้องทดสอบ'))
  assert result.status=='clarification' and h.state.pending.kind=='conversation'
  if case in {'A3','A4'}:
   result=h.send(row('A2')['text'],patch('update',month=9))
   assert result.status=='preview'
 elif case.startswith('B'):
  result=h.send(row('B1')['text'],patch(item='สมุดลายจุดทดสอบ',amount=47))
  assert result.status=='clarification' and h.state.pending.kind=='conversation'
  if case=='B3':
   result=h.send(row('B2')['text'],patch('update',category='Office'))
   assert result.status=='preview'
 elif case=='C2':
  result=h.send(row('C1')['text'],patch(item='แฟ้มทดสอบ',amount=75,note='เก็บป้ายโปรไว้'))
  assert result.status=='preview'
 else:raise AssertionError('Unknown scripted seed')
 assert not h.sheets.entries
 return h,result


def check_blank_clear():
 h=Harness();r=h.send('รถสองแถวสายสวน 35 บาท',patch(item='รถสองแถวสายสวน',amount=35,ops=[change('note',None,'clear')]))
 assert r.status=='preview' and full(h)==expected() and not h.sheets.entries

def test_clear_on_blank_without_note_source_is_normalized_to_keep():check_blank_clear()

def test_noop_clear_does_not_invent_an_item_or_other_required_fields():
 h=Harness();r=h.send('รถสองแถวสายสวน 35 บาท',patch(amount=35,ops=[change('note',None,'clear')]))
 assert r.status=='clarification' and (full(h) is None or full(h)['entries'][0]['item'] is None and not full(h)['ready']) and not h.sheets.entries

def test_empty_note_noop_allows_real_amount_edit_without_clearing_authority():
 h=Harness();h.draft();r=h.send('แก้ยอดเป็น 50 บาท',patch('update',amount=50,ops=[change('note',None,'clear')]))
 assert r.status=='preview' and full(h)==expected(amount='50') and not h.sheets.entries

def check_destructive_clear():
 h=Harness();old=h.draft('รถสองแถวสายสวน 35 บาท หมายเหตุ เก็บไว้',note='เก็บไว้');facts=full(h)['entries']
 r=h.send('แก้ยอดเป็น 50 บาท',patch('update',amount=50,ops=[change('note',None,'clear')]))
 assert r.status=='clarification' and full(h)['entries']==facts and not full(h)['ready'] and not h.sheets.entries
 assert h.click(old,'บันทึก').status=='stale'

def test_unsolicited_clear_of_existing_note_preserves_facts_and_blocks_stale_review():check_destructive_clear()

def check_supplied_note_clear():
 h=Harness();r=h.send('รถสองแถวสายสวน 35 บาท หมายเหตุ เก็บไว้',patch(item='รถสองแถวสายสวน',amount=35,ops=[change('note',None,'clear')]))
 assert r.status=='clarification' and full(h) is None and not h.sheets.entries
 assert h.state.pending.payload['source'].endswith('หมายเหตุ เก็บไว้')

def test_clear_cannot_suppress_a_note_supplied_in_the_initial_owner_message():check_supplied_note_clear()

def test_clear_cannot_suppress_retained_authenticated_source_note():
 h=Harness();h.send('รถสองแถวสายสวน 35 บาท วันที่ 4 หมายเหตุ เก็บไว้',RuntimeError('offline seed'))
 assert h.state.pending.kind=='tool_source'
 r=h.send('กันยา',patch(item='รถสองแถวสายสวน',amount=35,day=4,month=9,ops=[change('note',None,'clear')]))
 assert r.status=='clarification' and full(h) is None and 'หมายเหตุ เก็บไว้' in h.state.pending.payload['source'] and not h.sheets.entries

@pytest.mark.parametrize('text',['ล้างหมายเหตุเดิม','หมายเหตุ:'])
def test_owner_explicit_clear_still_works_from_valid_note_draft(text):
 h=Harness();h.draft('รถสองแถวสายสวน 35 บาท หมายเหตุ เก็บไว้',note='เก็บไว้')
 r=h.send(text,patch('update',ops=[change('note',None,'clear')]))
 assert r.status=='preview' and full(h)==expected() and not h.sheets.entries


@pytest.mark.parametrize('case',['A2','A3','A4','B2','C2'])
def test_scripted_correct_later_action_from_valid_runtime_seed(case):
 h,old=seed(case)
 values={'A2':{'month':9},'A3':{'day':10,'month':9,'year':2026},'A4':{},'B2':{'category':'Office'},'C2':{'note':'สำหรับงานทดสอบ'}}[case]
 ops=[change('note',None,'clear')] if case=='A4' else []
 r=h.send(row(case)['text'],patch('update',ops=ops,**values))
 assert r.status=='preview' and full(h)['ready'] and not h.sheets.entries
 e=full(h)['entries'][0]
 if case in {'A2','A3'}:assert e['date']==('2026-09-08' if case=='A2' else '2026-09-10') and e['note']=='เดินทางไปห้องทดสอบ'
 if case=='A4':assert e['note']=='' and e['date']=='2026-09-08'
 if case=='B2':assert e['category']=='Office' and e['item']=='สมุดลายจุดทดสอบ' and e['amount']=='47'
 if case=='C2':assert e['note']=='สำหรับงานทดสอบ' and e['amount']=='75'
 if old.status=='preview':assert h.click(old,'บันทึก').status=='stale'

@pytest.mark.parametrize('case',['A2','A3','A4','B2','C2'])
def test_clarify_intent_cannot_apply_hidden_entry_changes(case):
 h,_=seed(case);facts=full(h)['entries']
 value=envelope('clarify',question_field='ambiguous',entries=patch('update',amount=999)['entries']);r=h.send(row(case)['text'],value)
 assert r.status=='clarification' and full(h)['entries']==facts and not full(h)['ready'] and not h.sheets.entries
 # Clarification ignores edit metadata and cannot silently mutate a draft.

def test_summary_preserves_seeded_pending_draft():
 h,_=seed('B3');before=deepcopy(h.state.pending)
 r=h.send(row('B3')['text'],envelope('summary',start_date='2026-10-15',end_date='2026-10-15'))
 assert r.status=='summary' and r.data['expense']=='0.00' and h.state.pending==before and not h.sheets.entries

def test_failed_create_recovery_isolated_with_scripted_correct_semantics():
 h=harness();h.send(row('A1')['text'],RuntimeError('offline seed'))
 assert h.state.pending.kind=='tool_source'
 r=h.send(row('D1')['text'],patch(item='รถรางทดสอบ',amount=29,day=8,month=9,year=2026))
 assert r.status=='preview' and full(h)['entries'][0]['date']=='2026-09-08' and full(h)['entries'][0]['note']=='เดินทางไปห้องทดสอบ' and not h.sheets.entries

def test_complete_scripted_sequence_reviews_updates_and_confirms_once_in_mocks():
 h,_=seed('A2');h.send(row('A2')['text'],patch('update',month=9))
 h.send(row('A3')['text'],patch('update',day=10,month=9,year=2026))
 ready=h.send(row('A4')['text'],patch('update',ops=[change('note',None,'clear')]))
 assert ready.status=='preview' and not h.sheets.entries
 event=h.event('บันทึก');r=h.service.handle(event)
 assert r.status=='recorded' and len(h.sheets.entries)==1
 calls=len(h.port.requests)
 assert h.service.handle(deepcopy(event))==r and len(h.sheets.entries)==1 and len(h.port.requests)==calls



def test_negative_control_restoring_blanket_clear_rejection_fails_noop_behavior(monkeypatch):
 normal=DraftPatchAssistant._normalize_empty_note_clears
 def old(self,p,text):
  if any(c.field=='note' and c.op=='clear' for e in p.entries for c in e.changes):raise ToolError('note_clear_not_requested')
  return normal(self,p,text)
 check_blank_clear()
 with monkeypatch.context() as m:
  m.setattr(DraftPatchAssistant,'_normalize_empty_note_clears',old)
  with pytest.raises(AssertionError):check_blank_clear()

def test_negative_control_treating_incoming_note_clear_as_noop_is_detected(monkeypatch):
 def unsafe(self,p,text):
  return p.model_copy(update={'entries':[e.model_copy(update={'changes':[c.model_copy(update={'op':'unchanged','value':None}) if c.field=='note' and c.op=='clear' else c for c in e.changes]}) for e in p.entries]})
 check_supplied_note_clear()
 with monkeypatch.context() as m:
  m.setattr(DraftPatchAssistant,'_normalize_empty_note_clears',unsafe)
  with pytest.raises(AssertionError):check_supplied_note_clear()

def test_negative_control_removing_note_provenance_guards_detects_destructive_clear(monkeypatch):
 import line_assistant.domain_runtime as runtime
 check_destructive_clear()
 with monkeypatch.context() as m:
  m.setattr(DraftPatchAssistant,'_normalize_empty_note_clears',lambda self,p,text:p)
  m.setattr(runtime,'apply_note',lambda proposed,previous,*a,**k:('' if proposed=='' else previous,None))
  with pytest.raises(AssertionError):check_destructive_clear()
