"""Owner outcomes, literal dates, pure reads and fresh consent; no model score."""
from copy import deepcopy
import json,socket
import pytest
import behavior_support as ev
from line_assistant import semantic_tools as semantic
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.history_context import HistoryState
from line_assistant.history_first import HistoryFirstAssistant
from line_assistant.config import Settings
from test_history_first import response
from test_raw_workflow_v02 import Port

def harness(case='date-read'):return ev.Harness(Port(),case)
def turn(h,text,*values):
    h.port.values=list(values);return h.run_turn(text,1)[1]
def draft(h):return next(d for d in h.state.drafts.values() if d['status']!='recorded')
def prepare(h,text='กาแฟ 50 บาทวันนี้',item='กาแฟ',amount=50,date_text='วันนี้',date_ref=None):
    def call(body):
        c=json.loads(body['input'][0]['content']);selected=date_ref(c) if callable(date_ref) else date_ref
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[
            {'entry_ref':None,'item':item,'amount':amount,'date_text':date_text,'date_ref':selected,'note':None,'source_refs':[c['current_source_ref']]}]})
    return turn(h,text,call)
def change(h,text,**fields):
    d=deepcopy(draft(h))
    def call(body):
        c=json.loads(body['input'][0]['content'])
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
            {'entry_ref':d['entries'][0]['entry_ref'],'source_refs':[c['current_source_ref']],**fields}]})
    return turn(h,text,call)
def read(h,text='ขอดูร่าง',ref=None,legacy=False):
    d=draft(h)
    args={'draft_ref':ref or d['draft_ref']}
    if legacy:args.update(operation='review',draft_version=d['version'])
    return turn(h,text,response(name='control_expense_draft' if legacy else 'read_expense_drafts',args=args))
def review(h):
    d=draft(h)
    return turn(h,'ขอตรวจรายการก่อนบันทึก',response(name='prepare_expense_review',args={'draft_ref':d['draft_ref'],'draft_version':d['version']}))

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Local-only interface proofs')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

@pytest.mark.parametrize('phrase,expected',[('เมื่อวาน','2026-10-13'),('13 ตุลาคม 2026','2026-10-13'),('วันที่ 13','2026-10-13')])
def test_literal_date_expression_resolves_on_server_and_keeps_amount(phrase,expected):
    h=harness();prepare(h);result=change(h,'เปลี่ยนวันที่เป็น '+phrase,date_text=phrase,date_ref=None)
    assert result.status=='preview' and draft(h)['entries'][0]['date']==expected and draft(h)['entries'][0]['amount']=='50'
    schema=next(t for t in h.port.records[-1]['request']['tools'] if t['name']=='prepare_expenses')['parameters']['properties']['entries']['items']
    assert 'date' not in schema['properties'] and 'date_context' not in schema['properties'] and schema['properties']['date_ref']['enum']==[None]
    assert not h.sheets.entries

def test_null_date_on_amount_edit_does_not_clear_certified_date():
    h=harness();prepare(h);before=deepcopy(draft(h)['entries'][0]);result=change(h,'เปลี่ยนยอดเป็น 75 บาท',amount=75,date_text=None,date_ref=None)
    assert result.status=='preview' and draft(h)['entries'][0]['amount']=='75' and draft(h)['entries'][0]['date']==before['date']
    assert before['source_refs'][0] in draft(h)['entries'][0]['source_refs'] and not h.sheets.entries


@pytest.mark.parametrize('fields,text',[({'date_ref':'date_forged'},'ใช้วันที่ของรายการก่อน'),
                                      ({'date_text':'เมื่อวาน','date_ref':'date_forged'},'เปลี่ยนวันที่เป็นเมื่อวาน'),
                                      ({'date_text':'วันที่ 31 กุมภาพันธ์ 2026'},'เปลี่ยนวันที่เป็นวันที่ 31 กุมภาพันธ์ 2026'),
                                      ({'date_text':'เมื่อวาน'},'เปลี่ยนวันที่เป็นวันนี้')])
def test_invalid_date_or_reference_does_not_replace_existing_facts(fields,text):
    h=harness();prepare(h);before=deepcopy(draft(h));result=change(h,text,**fields)
    assert result.status!='recorded' and draft(h)=={**before,'review':None} and not h.sheets.entries

def test_forged_source_cannot_supply_a_literal_date():
    h=harness();prepare(h);d=deepcopy(draft(h))
    result=turn(h,'เปลี่ยนวันที่เป็นเมื่อวาน',response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],
        'entries':[{'entry_ref':d['entries'][0]['entry_ref'],'date_text':'เมื่อวาน','source_refs':['msg_forged']}]}))
    assert draft(h)=={**d,'review':None} and not h.sheets.entries

@pytest.mark.parametrize('phrase',['วันสะดวก','วันใดก็ได้'])
def test_unresolved_date_expression_cannot_silently_default_or_replace_a_date(phrase):
    h=harness();prepare(h);before=deepcopy(draft(h));result=change(h,'เปลี่ยนวันที่เป็น '+phrase,date_text=phrase,date_ref=None)
    assert result.status!='preview' and draft(h)=={**before,'review':None} and not h.sheets.entries

def test_only_a_verified_confirmed_date_is_offered_and_selectable():
    h=harness();prepare(h,'กาแฟ 50 บาทเมื่อวาน',date_text='เมื่อวาน')
    assert h.run_turn('โอเค บันทึกเลย',0)[1].status=='recorded'
    refs=semantic.date_targets(h.service);assert len(refs)==1 and refs[0]['date']=='2026-10-13'
    result=prepare(h,'ค่ากระดาษ 75 บาทใช้วันที่เดียวกับรายการก่อน',item='ค่ากระดาษ',amount=75,date_text=None,date_ref=lambda c:c['date_references'][0]['date_ref'])
    assert result.status=='preview' and draft(h)['entries'][0]['date']=='2026-10-13'
    assert draft(h)['entries'][0]['date_basis']['confirmed_event']==refs[0]['confirmed_event'] and len(h.sheets.entries)==1

def test_literal_date_and_valid_reference_are_mutually_exclusive():
    h=harness();prepare(h,'กาแฟ 50 บาทเมื่อวาน',date_text='เมื่อวาน');h.run_turn('โอเค บันทึกเลย',0)
    result=prepare(h,'ค่ากระดาษ 75 บาทวันนี้',item='ค่ากระดาษ',amount=75,date_text='วันนี้',date_ref=lambda c:c['date_references'][0]['date_ref'])
    assert result.status!='preview' and len(h.sheets.entries)==1 and not [d for d in h.state.drafts.values() if d['status']!='recorded']

def test_a_recorded_receipt_without_completed_effect_cannot_offer_a_date_target():
    h=harness();prepare(h,'กาแฟ 50 บาทเมื่อวาน',date_text='เมื่อวาน');h.run_turn('โอเค บันทึกเลย',0)
    assert semantic.date_targets(h.service)
    next(iter(h.state.actions.values())).status='uncertain'
    assert semantic.date_targets(h.service)==[]

def test_date_target_from_another_workbook_is_rejected_after_context_creation():
    h=harness();prepare(h,'กาแฟ 50 บาทเมื่อวาน',date_text='เมื่อวาน');h.run_turn('โอเค บันทึกเลย',0)
    def foreign(c):
        ref=c['date_references'][0]['date_ref'];h.sheets.target.spreadsheet_id='synthetic-other-book';return ref
    result=prepare(h,'ค่ากระดาษ 75 บาทใช้วันที่ของรายการก่อน',item='ค่ากระดาษ',amount=75,date_text=None,date_ref=foreign)
    assert result.status!='preview' and len(h.sheets.entries)==1
    assert not [d for d in h.state.drafts.values() if d['status']!='recorded']

@pytest.mark.parametrize('legacy',[False,True])
def test_read_keeps_full_draft_version_nonce_proof_and_has_no_save_buttons(legacy):
    h=harness();prepare(h);before=deepcopy(draft(h));proof=deepcopy(h.state.draft_proofs);result=read(h,legacy=legacy)
    assert result.status=='answer' and draft(h)==before and h.state.draft_proofs==proof
    assert result.data['tool_result']['read_only'] and 'line_message' not in result.data and 'confirm_command' not in result.data
    assert not h.sheets.entries and not h.state.actions


def test_read_after_cold_restore_preserves_confirmation_state():
    h=harness();prepare(h);before=deepcopy(draft(h));state=HistoryState();state.clock=lambda:h.now;state.enable_raw_history()
    for row in h.state.raw_journal.rows.values():state.raw_journal.put(deepcopy(row))
    restore_history(state,history_snapshot(h.state),'synthetic-workflow-owner')
    h.state=state;h.service=HistoryFirstAssistant(Settings(owner_id='synthetic-workflow-owner'),h.sheets,port=h.port,state=state,clock=lambda:h.now,
        history_first_enabled=True,item_enrollment_enabled=True,semantic_enabled=True,raw_history_enabled=True)
    result=read(h);assert result.status=='answer' and draft(h)==before and not h.sheets.entries

def test_explicit_review_is_fresh_and_edit_invalidates_its_old_save_action():
    h=harness();prepare(h);before=deepcopy(draft(h));result=review(h)
    assert result.status=='preview' and draft(h)['version']==before['version']+1 and draft(h)['review']['nonce']!=before['review']['nonce']
    old_command=result.data['confirm_command'];change(h,'เปลี่ยนยอดเป็น 75 บาท',amount=75,date_text=None,date_ref=None)
    assert h.run_turn(old_command,0)[1].status=='stale' and not h.sheets.entries

def test_read_does_not_turn_short_acknowledgement_into_confirmation():
    h=harness();prepare(h);read(h);result=turn(h,'ใช่',response(text='ต้องตรวจรายการก่อนบันทึกครับ'))
    assert result.status!='recorded' and not h.sheets.entries

def test_confirmed_write_after_explicit_review_and_duplicate_are_once_only():
    h=harness();prepare(h);read(h);result=review(h);event=h.event('โอเค บันทึกเลย');h.port.limit=0
    first=h.service.handle(event);calls=len(h.port.records);again=h.service.handle(deepcopy(event))
    assert first.status=='recorded' and again==first and len(h.sheets.entries)==1 and len(h.port.records)==calls

@pytest.mark.parametrize('name,amount',[('ค่ากระดาษ',75),('กาแฟ',50)])
def test_known_database_items_keep_canonical_names_without_stripping_prefixes(name,amount):
    h=harness();result=prepare(h,name+' '+str(amount)+' บาทวันนี้',item=name,amount=amount)
    assert result.status=='preview' and draft(h)['entries'][0]['item']==name and draft(h)['entries'][0]['mapping'] is None
    assert h.sheets.atomic_batches==0

def test_new_name_is_an_owner_visible_proposal_not_an_exact_string_gate():
    h=harness();result=prepare(h,'จ่ายค่าวาฟเฟิลทดสอบไป 60 บาทวันนี้',item='ค่าวาฟเฟิลทดสอบ',amount=60)
    assert result.status=='clarification' and 'ค่าวาฟเฟิลทดสอบ' in result.message and draft(h)['entries'][0]['amount']=='60'
    assert not h.sheets.entries and not h.state.actions and h.sheets.atomic_batches==0


def test_amount_correction_retains_original_literal_date_and_full_new_name():
 h=harness();prepare(h,'ค่าวาฟเฟิลทดสอบ 60 บาทเมื่อวาน',item='ค่าวาฟเฟิลทดสอบ',amount=60,date_text='เมื่อวาน')
 original=deepcopy(draft(h)['entries'][0])
 result=change(h,'เปลี่ยนยอดเป็น 75 บาท',amount=75,date_text=None,date_ref=None)
 entry=draft(h)['entries'][0]
 assert entry['item']=='ค่าวาฟเฟิลทดสอบ' and 'ค่าวาฟเฟิลทดสอบ' in result.message
 assert entry['amount']=='75' and entry['date']==original['date']=='2026-10-13'
 assert entry['date_basis']==original['date_basis'] and not h.sheets.entries


def test_read_after_history_summary_preserves_review_nonce_and_draft_version():
 from test_raw_history import summary_output
 h=harness();prepare(h,'กาแฟ 50 บาทวันนี้ หมายเหตุ ใบเสร็จทดสอบ')
 before=deepcopy(draft(h))
 for n in range(9):h.seed_pair(n,'วันที่ 13 ตุลาคม 2026 ทดสอบจัดแฟ้ม '+('แฟ้มและกระดาษทดสอบ '*20),'คุยเรื่องแฟ้มต่อ',ev.timedelta(minutes=60-n))
 # Current schemas and explicit owner-category instructions need about38k
 # after summary (49k before).
 # This still forces compaction; negative context probes stay unchanged.
 h.service.raw_context_tokens=38000
 h.port.values=[summary_output,response(name='read_expense_drafts',args={'draft_ref':before['draft_ref']})]
 _,result,out=h.run_turn('ขอดูร่างกาแฟ',2)
 assert result.status=='answer' and result.data['tool_result']['read_only']
 assert h.state.rolling_summary is not None and out['calls']==2
 assert draft(h)==before and 'line_message' not in result.data and not h.sheets.entries


def test_insufficient_context_cannot_cancel_or_discard_pending_facts():
 h=harness();prepare(h);before=deepcopy(h.state.drafts);originals=deepcopy(h.state.raw_journal.rows)
 h.service.raw_context_tokens=1000
 _,result,out=h.run_turn('ยกเลิกร่างกาแฟนี้ครับ',0)
 assert result.status=='history_context_overflow' and out['calls']==0
 assert h.state.drafts==before and not h.sheets.entries and not h.state.actions
 assert all(h.state.raw_journal.rows[k]==v for k,v in originals.items())
