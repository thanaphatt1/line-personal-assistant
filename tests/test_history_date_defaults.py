"""Owner-approved date policy, deterministic source/defaults and confirmation controls."""
from copy import deepcopy
from datetime import datetime
import json
import pytest
from line_assistant import history_first
from line_assistant.history_context import source_ref
from line_assistant.reply_display import day
from line_assistant.history_dates import cues
from test_history_first import Harness,data,response

@pytest.mark.parametrize('at,text,expected',[
 ('2026-10-13T12:00:00+00:00','มีค่ากระดาษ 228','2026-10-13'),
 ('2026-10-13T12:00:00+00:00','วันที่ 10 มีค่ากระดาษ 228','2026-10-10'),
 ('2026-10-31T17:00:00+00:00','วันที่ 1 ค่ากระดาษ 228','2026-11-01'),
 ('2026-12-31T17:00:00+00:00','วันที่ 1 ค่ากระดาษ 228','2027-01-01'),
 ('2026-01-01T12:00:00+00:00','เมื่อวานค่ากระดาษ 228','2025-12-31'),
 ('2026-03-01T12:00:00+00:00','เมื่อวานค่ากระดาษ 228','2026-02-28'),
 ('2024-03-01T12:00:00+00:00','เมื่อวานค่ากระดาษ 228','2024-02-29'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 10 กันยายน','2026-09-10'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 10 มี.ค.','2026-03-10'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 10 มีนา','2026-03-10'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 10 เดือน 9 ปี 2568','2025-09-10'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 29 กุมภาพันธ์ 2567','2024-02-29'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 2024-02-29','2024-02-29'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 29/2/2024','2024-02-29'),
 ('2026-01-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 10 เดือนก่อน','2025-12-10'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 10 ตุลา ปีที่แล้ว','2025-10-10'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ ๑๐ เดือน ๙ ปี ๒๕๖๙','2026-09-10'),
])
def test_explicit_relative_and_defaulted_dates_normalize_without_literal_iso(at,text,expected):
 h=Harness();h.now=datetime.fromisoformat(at);r=h.prepare(text,date=expected)
 assert r.status=='preview' and h.draft()['entries'][0]['date']==expected and day(expected) in r.message and not h.sheets.entries
 assert h.draft()['entries'][0]['date_basis']['policy']=='bangkok-date-defaults-v02'

@pytest.mark.parametrize('text',['วันที่ 31 พฤศจิกายน ค่ากระดาษ 228','วันที่ 31 เดือน 4 ค่ากระดาษ 228','วันที่ 29 กุมภาพันธ์ 2025 ค่ากระดาษ 228','วันที่ 30 กุมภาพันธ์ 2024 ค่ากระดาษ 228','วันที่ 0 ค่ากระดาษ 228','วันที่ 10 เดือน 13 ค่ากระดาษ 228'])
def test_impossible_calendar_dates_clarify_without_review(text):
 h=Harness();r=h.prepare(text)
 assert r.status=='clarification' and not h.state.drafts and not h.sheets.entries
 assert 'entries[0].invalid_calendar_date' in r.data['tool_result']['invalid_fields']

@pytest.mark.parametrize('at,text,expected',[
 ('2026-10-13T12:00:00+00:00','วันที่ 20 ตุลาคม ค่ากระดาษ 228','2026-10-20'),
 ('2026-10-13T12:00:00+00:00','วันที่ 2026-10-20 ค่ากระดาษ 228','2026-10-20'),
 ('2026-10-13T12:00:00+00:00','พรุ่งนี้ค่ากระดาษ 228','2026-10-14'),
 ('2026-10-13T12:00:00+00:00','ค่ากระดาษ 228 วันที่ 2027-01-01','2027-01-01')])
def test_future_date_is_surfaced_under_unchanged_business_restriction_without_rollback(at,text,expected):
 h=Harness();h.now=datetime.fromisoformat(at);r=h.prepare(text,date=expected)
 assert r.status=='clarification' and day(expected) in r.message and 'ล่วงหน้าไม่ได้' in r.message
 assert r.data['tool_result']['date_details'][0]['normalized_date']==expected
 assert 'entries[0].future_expense_date' in r.data['tool_result']['invalid_fields'] and not h.state.drafts and not h.sheets.entries

@pytest.mark.parametrize('at,day,candidate',[
 ('2026-10-13T12:00:00+00:00',20,'2026-10-20'),
 ('2026-01-01T12:00:00+00:00',31,'2026-01-31')])
@pytest.mark.parametrize('propose_candidate',[False,True])
def test_future_day_only_asks_month_and_retains_partial_fields_without_month_rollback(at,day,candidate,propose_candidate):
 h=Harness();h.now=datetime.fromisoformat(at)
 r=h.prepare(f'วันที่ {day} มีค่ากระดาษ 228',date=candidate if propose_candidate else None)
 assert r.status=='clarification' and r.message==f'วันที่ {day} หมายถึงเดือนไหนครับ? ถ้าเป็นเดือนนี้จะเป็นวันที่อนาคต'
 d=h.draft();e=d['entries'][0]
 assert d['status']=='needs_information' and d['review'] is None and not h.sheets.entries
 assert e['date'] is None and e['date_parts']['day']==day and 'month' not in e['date_parts']
 assert (e['item'],e['amount'])==('ค่ากระดาษ','228') and e['date_basis']['default_month_candidate']==candidate
 assert r.data['tool_result']['missing_fields']==['entries[0].date.month'] and r.data['tool_result']['invalid_fields']==[]

@pytest.mark.parametrize('reply',['กันยายน','เดือนก่อน'])
def test_followup_month_resolves_the_original_day_and_preserves_item_amount(reply):
 h=Harness();h.prepare('วันที่ 20 มีค่ากระดาษ 228');ref=h.draft()['draft_ref']
 h.send('ดาวฤกษ์คืออะไร',response(text='ดาวฤกษ์เปล่งแสงได้เอง'))
 r=h.update(reply,date='2026-09-20',date_text=reply)
 assert r.status=='preview' and h.draft()['draft_ref']==ref
 assert [(e['item'],e['amount'],e['date']) for e in h.draft()['entries']]==[('ค่ากระดาษ','228','2026-09-20')]
 assert '20 ก.ย. 2026' in r.message and not h.sheets.entries

def test_amount_edit_while_awaiting_month_keeps_original_day_and_then_resolves():
 h=Harness();h.prepare('วันที่ 20 มีค่ากระดาษ 228')
 r=h.update('แก้ยอดค่ากระดาษเป็น 250 บาท',amount=250,date=None)
 assert r.status=='clarification' and h.draft()['entries'][0]['date_parts']['day']==20 and h.draft()['entries'][0]['amount']=='250'
 r=h.update('กันยายน',date='2026-09-20',date_text='กันยายน')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2026-09-20' and h.draft()['entries'][0]['amount']=='250'

def test_explicit_current_month_reply_is_future_restriction_not_another_missing_month_question():
 h=Harness();h.prepare('วันที่ 20 มีค่ากระดาษ 228')
 r=h.update('ตุลา',date='2026-10-20',date_text='ตุลา')
 assert r.status=='clarification' and '20 ต.ค. 2026 ยังไม่ถึง' in r.message and 'เดือนไหน' not in r.message
 assert 'entries[0].future_expense_date' in r.data['tool_result']['invalid_fields'] and 'entries[0].date.month' not in r.data['tool_result']['missing_fields'] and not h.sheets.entries

def test_invalid_month_reply_clarifies_and_preserves_original_partial_day():
 h=Harness();h.prepare('วันที่ 31 มีค่ากระดาษ 228')
 r=h.update('กันยายน',date=None,date_text='กันยายน')
 assert r.status=='clarification' and 'ไม่มีอยู่จริง' in r.message and h.draft()['entries'][0]['date_parts']['day']==31 and h.draft()['review'] is None
 r=h.update('สิงหาคม',date='2026-08-31',date_text='สิงหาคม')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2026-08-31' and not h.sheets.entries

@pytest.mark.parametrize('month,status,expected',[('กันยายน','preview','2026-09-20'),('ตุลาคม','clarification','2026-10-20')])
def test_relevant_explicit_month_context_precedes_future_day_month_question(month,status,expected):
 h=Harness();h.send(f'กำลังลงรายจ่ายเดือน{month} ปี 2026',response(text='รับทราบ'))
 r=send_sources(h,'วันที่ 20 ค่ากระดาษ 228',[source_ref('history-1')],date=expected)
 assert r.status==status and day(expected) in r.message and 'เดือนไหน' not in r.message and not h.sheets.entries

def test_old_confirmation_stays_stale_during_month_question_and_after_valid_month_reply():
 h=Harness();first=h.prepare('วันที่ 10 มีค่ากระดาษ 228')
 r=h.update('แก้ค่ากระดาษเป็นวันที่ 20',date=None)
 assert r.status=='clarification' and h.draft()['entries'][0]['date_parts']['day']==20 and h.draft()['review'] is None
 assert h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries
 r=h.update('กันยายน',date='2026-09-20',date_text='กันยายน')
 assert r.status=='preview' and h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries

def test_month_question_does_not_accept_an_invented_previous_month_proposal():
 h=Harness();r=h.prepare('วันที่ 20 มีค่ากระดาษ 228',date='2026-09-20')
 assert r.status=='clarification' and 'entries[0].date_source' in r.data['tool_result']['invalid_fields'] and not h.state.drafts and not h.sheets.entries

def send_sources(h,text,refs,**kw):
 def call(body):
  c=json.loads(body['input'][0]['content'])
  return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(source_refs=[*refs,c['current_source_ref']],**kw)]})
 return h.send(text,call,response(text='ขอข้อมูลครับ'))

def test_relevant_expense_month_year_header_precedes_default_but_unrelated_history_does_not():
 h=Harness();h.send('กำลังลงรายจ่ายเดือนกันยายน ปี 2568',response(text='รับทราบ'))
 r=send_sources(h,'วันที่ 10 ค่ากระดาษ 228',[source_ref('history-1')],date='2025-09-10')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2025-09-10'
 h=Harness();h.send('นัดประชุมเดือนกันยายน ปี 2568',response(text='รับทราบ'))
 r=send_sources(h,'วันที่ 10 ค่ากระดาษ 228',[source_ref('history-1')],date='2026-10-10')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2026-10-10'

def test_an_unrelated_old_date_even_with_same_item_cannot_become_a_new_expense_month():
 h=Harness();h.send('วันที่ 10 กันยายน ค่ากระดาษมีส่วนลดไหม',response(text='ตรวจจากร้าน'))
 r=send_sources(h,'วันที่ 10 ค่ากระดาษ 228',[source_ref('history-1')],date='2026-10-10')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2026-10-10'

def test_unrelated_dates_in_notes_and_item_names_are_not_date_evidence():
 h=Harness();r=h.prepare('ค่ากระดาษ 228 หมายเหตุ วันที่ 1 กันยายน',note='วันที่ 1 กันยายน')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2026-10-13'
 h=Harness();h.sheets.rows.append(('โปรเดือนมีนาคมค่ากระดาษ','Work/Office',''))
 r=h.prepare('โปรเดือนมีนาคมค่ากระดาษ 228',item='โปรเดือนมีนาคมค่ากระดาษ')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2026-10-13'

def test_explicit_relevant_draft_date_context_survives_interruption_and_day_correction():
 h=Harness();first=h.prepare('วันที่ 10 กันยายน ปี 2568 ค่ากระดาษ 228',date='2025-09-10')
 h.send('ประวัติศาสตร์ปี 2020 คืออะไร',response(text='เป็นอีกหัวข้อ'))
 r=h.update('แก้ค่ากระดาษเป็นวันที่ 8',date='2025-09-08')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2025-09-08'
 assert h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries

def test_undated_amount_correction_preserves_old_date_but_new_day_uses_new_month_year_defaults():
 h=Harness();h.now=datetime.fromisoformat('2026-12-31T16:00:00+00:00');first=h.prepare('ค่ากระดาษ 228',date='2026-12-31')
 h.now=datetime.fromisoformat('2027-01-01T12:00:00+00:00')
 r=h.update('แก้ยอดค่ากระดาษเป็น 250 บาท',amount=250)
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2026-12-31'
 r=h.update('แก้ค่ากระดาษเป็นวันที่ 1',date='2027-01-01')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2027-01-01'
 assert h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries

def test_year_only_correction_preserves_verified_day_and_month_across_month_boundary():
 h=Harness();h.now=datetime.fromisoformat('2026-10-31T16:00:00+00:00');first=h.prepare('ค่ากระดาษ 228',date='2026-10-31')
 h.now=datetime.fromisoformat('2026-11-01T12:00:00+00:00')
 r=h.update('แก้ปีค่ากระดาษเป็นปี 2025',date='2025-10-31')
 assert r.status=='preview' and h.draft()['entries'][0]['date']=='2025-10-31'
 assert h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries

def test_normalized_date_is_data_but_date_text_remains_literal_original_evidence():
 h=Harness();r=h.prepare('วันที่ 10 มีค่ากระดาษ 228',date='2026-10-10',date_text='วันที่ 10')
 assert r.status=='preview'
 h=Harness();r=h.prepare('วันที่ 10 มีค่ากระดาษ 228',date='2026-10-10',date_text='วันที่ 10 ตุลาคม')
 assert r.status=='clarification' and 'entries[0].date_text_source' in r.data['tool_result']['invalid_fields'] and not h.state.drafts

def test_invented_march_is_rejected_and_มีค่ากระดาษ_has_no_month_cue():
 assert cues('วันที่ 10 มีค่ากระดาษ 228',Harness().now)[0]=={'day':10}
 h=Harness();r=h.prepare('วันที่ 10 มีค่ากระดาษ 228',date='2026-03-10')
 assert r.status=='clarification' and 'entries[0].date_source' in r.data['tool_result']['invalid_fields'] and not h.state.drafts

def test_invalid_date_correction_revokes_confirmation_and_keeps_valid_date():
 h=Harness();first=h.prepare('วันที่ 10 มีค่ากระดาษ 228');old=deepcopy(h.draft()['entries'])
 r=h.update('แก้ค่ากระดาษเป็นวันที่ 31 กุมภาพันธ์',date=None,date_text='วันที่ 31 กุมภาพันธ์')
 assert r.status=='clarification' and h.draft()['entries']==old and h.draft()['review'] is None
 assert h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries

@pytest.mark.parametrize('text',['ค่ากระดาษเมื่อกี้วันที่ 9','เดือนกันยายน'])
def test_clear_date_correction_revokes_old_confirmation_even_if_model_does_not_call_a_tool(text):
 h=Harness();first=h.prepare('วันที่ 10 มีค่ากระดาษ 228')
 r=h.send(text,response(text='ขอทบทวนวันที่ครับ'))
 assert h.draft()['entries'][0]['date']=='2026-10-10' and h.draft()['review'] is None
 assert h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries

@pytest.mark.parametrize('text',['วันที่ 10 เดือนสิบสาม ค่ากระดาษ 228','วันที่ 10 วันเสาร์ ค่ากระดาษ 228'])
def test_unsupported_explicit_date_qualifier_is_not_repaired_by_defaults(text):
 assert cues(text,Harness().now)[2]=='unsupported_date_source'
 h=Harness();r=h.prepare(text)
 assert r.status=='clarification' and not any(d['review'] for d in h.state.drafts.values()) and not h.sheets.entries

def test_negative_control_invented_month_policy_is_detected(monkeypatch):
 original=history_first.policy_date
 def check():
  h=Harness();r=h.prepare('วันที่ 10 มีค่ากระดาษ 228')
  assert r.status=='preview' and h.draft()['entries'][0]['date']=='2026-10-10'
 def unsafe(*a,**kw):
  value,parts,basis,issue=original(*a,**kw)
  return ('2026-03-10' if value=='2026-10-10' else value),parts,basis,issue
 check()
 with monkeypatch.context() as m:
  m.setattr(history_first,'policy_date',unsafe)
  with pytest.raises(AssertionError):check()

def test_negative_control_stale_date_confirmation_is_detected(monkeypatch):
 original=history_first.HistoryFirstAssistant._confirm
 def check():
  h=Harness();first=h.prepare('วันที่ 10 มีค่ากระดาษ 228');h.update('แก้ค่ากระดาษเป็นวันที่ 9',date='2026-10-09')
  assert h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries
 def unsafe(self,ref,nonce):
  d=self.state.drafts.get(ref)
  return original(self,ref,d['review']['nonce'] if d and d.get('review') else nonce)
 check()
 with monkeypatch.context() as m:
  m.setattr(history_first.HistoryFirstAssistant,'_confirm',unsafe)
  with pytest.raises(AssertionError):check()
