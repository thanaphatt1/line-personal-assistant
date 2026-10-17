"""LLM responses are scripted synthetic proposals; no paid model or LINE traffic."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import socket

import pytest
from line_assistant.adapters import SheetLayout, MockCalendar
from line_assistant.catalog import ItemCatalog
from line_assistant.config import Settings
from line_assistant.conversation import ConversationAssistant
from line_assistant.conversation_dates import resolve
from line_assistant.conversation_mocks import ConversationSheets
from line_assistant.intent_api import Intent, IntentPlanner, body, parse, tools
from line_assistant.language import dates
from line_assistant.model_api import DurableModelGuard, MODEL_ID, RESERVATION_MICRO_USD
from line_assistant.persistence import SQLiteState, state_snapshot
from line_assistant.service import ProviderFailure
from line_assistant.state import MemoryState, Pending
from line_assistant.taxonomy import identifier
from test_model_api import ModelPort, response

NOW = datetime(2026, 10, 12, 2, tzinfo=timezone.utc)
ROWS = [('รถเมล์', 'Transportation', ''), ('วิตามิน', 'Work/Office', 'Supplies'),
        ('สบู่', 'Work/Office', 'Hygiene'), ('กาแฟ', 'Food', '')]

@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def fail(*args, **kwargs): raise AssertionError('Local conversation tests cannot use sockets')
    monkeypatch.setattr(socket.socket, 'connect', fail)
    monkeypatch.setattr(socket.socket, 'connect_ex', fail)

def decision(intent, **kwargs): return Intent(intent=intent, **kwargs).model_dump(mode='json')

class Harness:
    def __init__(self, state=None, sheets=None, limit=500000):
        self.now = NOW; self.counter = 0
        self.sheets = sheets or ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'}, ('F','G')), ROWS)
        self.state = state or MemoryState()
        self.state.workbook_id = self.sheets.target.spreadsheet_id
        self.service = ConversationAssistant(Settings(owner_id='mock-owner', channel_secret='synthetic-secret'),
            self.sheets, MockCalendar(), ItemCatalog([], {}), state=self.state, clock=lambda:self.now)
        self.service.production = False
        self.port = ModelPort()
        self.guard = DurableModelGuard(self.state, monthly_micro_usd=limit, max_calls_day=100, clock=lambda:self.now)
        self.service.planner = IntentPlanner(self.port, self.guard, lambda:self.service._active_event_id)

    def event(self, text='', postback=None):
        self.counter += 1; self.now += timedelta(seconds=1)
        value = {'webhookEventId':f'conv-{self.counter}', 'timestamp':int(self.now.timestamp()*1000),
                 'source':{'type':'user','userId':'mock-owner'}, 'replyToken':'synthetic-reply'}
        if postback is None: value.update(type='message', message={'id':str(self.counter),'type':'text','text':text})
        else: value.update(type='postback', postback={'data':postback})
        return value

    def send(self, text, intent, **kwargs):
        self.port.value = response('interpret_message', decision(intent, **kwargs))
        return self.service.handle(self.event(text))

    def click(self, result, label):
        buttons = result.data['line_message']['contents']['footer']['contents']
        value = next(b['action']['data'] for b in buttons if b['action']['label'] == label)
        return self.service.handle(self.event(postback=value))

    def expense(self, item='รถเมล์', amount='20', text=None, date=None):
        return self.send(text or f'{item} {amount} บาท', 'expense', entries=[{'item':item,'amount':amount,'date':date}])

def test_medicine_transcript_preserves_day_amount_and_resolves_health_before_single_confirmation():
    h = Harness()
    r = h.send('วันที่ 10 มีค่ากระดาษ 228 บาท', 'expense', entries=[{'item':'ค่ากระดาษ','amount':'228'}])
    assert r.status == 'clarification' and 'วันที่ 10 เป็นเดือนใด' in r.message
    assert h.state.pending.payload['entries'][0]['amount'] == '228'
    r = h.send('ตุลาคม', 'edit_draft', entries=[{'date':'2026-10-10'}])
    assert r.data['stage'] == 'resolution' and not h.sheets.entries
    r = h.send('เพิ่มในหมวด office', 'enroll_item', category_text='office')
    assert r.data['stage'] == 'subcategory' and 'Work/Office' in r.message
    r = h.send('Supplies', 'enroll_item', subcategory_text='Supplies')
    assert r.status == 'preview' and 'ค่ากระดาษ 228 บาท วันที่ 2026-10-10' in r.message
    old = deepcopy(r)
    r = h.send('แก้ยอดเป็น 250 บาท', 'edit_draft', entries=[{'amount':'250'}])
    assert r.status == 'preview' and '250' in r.message
    assert h.click(old, 'บันทึก').status == 'stale' and h.port.calls == 5
    r = h.send('บันทึกเลย', 'confirm')
    assert r.status == 'recorded' and h.sheets.atomic_batches == 1
    assert [(e.item,e.amount,e.date.isoformat()) for e in h.sheets.entries] == [('ค่ากระดาษ','250','2026-10-10')]
    assert ('ค่ากระดาษ','Work/Office','Supplies') in h.sheets.rows
    assert h.port.calls == 6 and len(h.state.recent_turns) == 6
    assert not h.state.pending
    for request in h.port.bodies:
        payload = json.loads(request['input'][0]['content'])
        assert len(payload['context']['recent']) <= 6
        assert 'mock-owner' not in json.dumps(request) and 'expires_at' not in json.dumps(request)
        assert 'origin_event_id' not in json.dumps(request) and 'allowed' not in json.dumps(request)

def test_known_item_preview_and_button_are_one_owner_confirmed_write_without_second_model_call():
    h=Harness(); r=h.expense()
    assert r.status == 'preview' and 'Database:' not in r.message and not h.sheets.entries
    result=h.click(r,'บันทึก')
    assert result.status == 'recorded' and h.port.calls == 1 and len(h.sheets.entries) == 1
    assert h.click(r,'บันทึก').status == 'stale'

def test_typo_selection_keeps_amount_and_requires_final_preview():
    h=Harness(); r=h.expense('รถเมล','35')
    assert r.status == 'clarification' and not h.sheets.entries
    chosen=identifier('item','รถเมล์')
    r=h.send('หมายถึงรถเมล์ครับ','select_item',item_id=chosen)
    assert r.status == 'preview' and '35' in r.message and h.port.calls == 2
    assert h.click(r,'บันทึก').status == 'recorded' and h.sheets.entries[0].item == 'รถเมล์'
    assert h.sheets.atomic_batches == 0

def test_new_item_buttons_use_existing_pair_and_atomic_mock():
    h=Harness(); r=h.expense('ขนม','50')
    r=h.click(r,'เพิ่มรายการใหม่'); r=h.click(r,'Food')
    assert r.status == 'preview' and 'Food' in r.message
    assert h.click(r,'บันทึก').status == 'recorded'
    assert h.port.calls == 1 and h.sheets.atomic_batches == 1

def test_ambiguous_health_component_requires_category_choice():
    sheets=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),
                             ROWS+[('แว่น','Office/Other','Vision')])
    h=Harness(sheets=sheets); h.expense('ค่ากระดาษ','228')
    r=h.send('เพิ่มในหมวด office','enroll_item',category_text='office')
    assert r.data['stage'] == 'category' and h.state.pending.payload['entries'][0]['category_id'] is None
    assert not h.sheets.entries

@pytest.mark.parametrize('text', ['ไม่ต้องบันทึก','ยังไม่บันทึก','ถ้าบันทึกเลย','บันทึกไหม','"บันทึก"','yes?','ตกลง 250 บาท'])
def test_model_confirm_cannot_override_negation_question_or_unanchored_owner_text(text):
    h=Harness(); h.expense()
    assert h.send(text,'confirm').status in {'no_action','preview'}
    assert not h.sheets.entries and h.port.calls == 2

def test_correction_cancellation_reads_all_reach_model_and_keep_short_context():
    h=Harness(); h.expense()
    assert h.send('สรุปวันนี้','summary').status == 'summary'
    assert h.send('เมื่อวานวันที่เท่าไหร่','date_question').status == 'date_answer'
    assert h.send('ยกเลิก','cancel').status == 'cancelled'
    assert h.port.calls == 4 and h.state.pending is None and not h.sheets.entries
    assert len(h.state.recent_turns) == 6

def test_recorded_edit_has_no_new_expense_and_reaches_model():
    h=Harness(); h.click(h.expense(),'บันทึก')
    assert h.send('แก้รายการล่าสุดเป็น 40 บาท','edit_recorded').status == 'unsupported'
    assert len(h.sheets.entries) == 1 and h.port.calls == 2

@pytest.mark.parametrize('text,status',[('รายรับ รถเมล์ 20 บาท','income_unconfigured'),
    ('รับเงิน รถเมล์ 20 บาท','income_unconfigured'),('income รถเมล์ 20 บาท','income_unconfigured'),
    ('นัด รถเมล์ 20','unsupported'),('เตือน รถเมล์ 20','unsupported'),
    ('ช่วยนัด รถเมล์ 20','unsupported'),('ช่วยเตือน รถเมล์ 20','unsupported'),
    ('ขอนัด รถเมล์ 20','unsupported'),('please เตือน รถเมล์ 20','unsupported')])
def test_out_of_scope_source_cannot_become_expense_by_model_misclassification(text,status):
    h=Harness()
    assert h.send(text,'expense',entries=[{'item':'รถเมล์','amount':'20'}]).status==status
    assert h.port.calls==1 and h.state.pending is None and not h.sheets.entries

def test_note_delimiter_does_not_hide_income_on_later_batch_line():
    h=Harness();text='รถเมล์ 20 บาท หมายเหตุ ไปตลาด\nรายรับ กาแฟ 50 บาท'
    result=h.send(text,'expense',entries=[{'item':'รถเมล์','amount':'20','note':'ไปตลาด'},{'item':'กาแฟ','amount':'50'}])
    assert result.status=='income_unconfigured' and h.port.calls==1 and not h.sheets.entries

@pytest.mark.parametrize('fields', [
    {'item':'invented','amount':'20'}, {'item':'รถเมล์','amount':'99'},
    {'item':'รถเมล์','amount':'20','date':'2025-01-01'}, {'item':'รถเมล์','amount':'20','note':'invented'},
])
def test_model_fields_without_current_evidence_do_not_become_ready(fields):
    h=Harness(); assert h.send('รถเมล์ 20 บาท','expense',entries=[fields]).status == 'invalid'
    assert not h.sheets.entries and h.state.pending is None

@pytest.mark.parametrize('text, amount', [('รถเมล์ -20 บาท','20'),('รถเมล์ 1,2 บาท','12'),
                                       ('รถเมล์ 20.001 บาท','20'),('รถเมล์ 0 บาท','0')])
def test_invalid_source_money_is_not_accepted(text, amount):
    h=Harness(); assert h.send(text,'expense',entries=[{'item':'รถเมล์','amount':amount}]).status == 'invalid'

def test_ids_outside_supplied_choices_are_rejected_without_taxonomy_write():
    h=Harness(); h.expense('ค่ากระดาษ','228')
    assert h.send('health','enroll_item',category_id='cat_forged').status == 'invalid'
    assert len(h.sheets.rows) == len(ROWS) and not h.sheets.entries

def test_cancel_button_still_works_when_free_text_budget_is_exhausted():
    h=Harness(limit=RESERVATION_MICRO_USD); r=h.expense()
    assert h.send('ยกเลิก','cancel').status == 'model_budget_exhausted'
    assert h.state.pending and h.port.calls == 1
    assert h.click(r,'ยกเลิก').status == 'cancelled'

def test_duplicate_owner_event_has_one_model_call_and_postback_one_write():
    h=Harness(); h.port.value=response('interpret_message',decision('expense',entries=[{'item':'รถเมล์','amount':'20'}]))
    event=h.event('รถเมล์ 20 บาท'); r=h.service.handle(event)
    assert h.service.handle(event)==r and h.port.calls==1
    data=r.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    ev=h.event(postback=data); result=h.service.handle(ev)
    assert h.service.handle(ev)==result and len(h.sheets.entries)==1 and h.port.calls==1
    ev['postback']['data']=data+'changed'
    assert h.service.handle(ev).status=='conflict'

def test_owner_security_stale_event_and_text_token_cannot_authorize_a_button():
    h=Harness(); r=h.expense(); data=r.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    ev=h.event(postback=data); ev['source']['userId']='other'
    assert h.service.handle(ev).status=='denied'
    old=h.event('รถเมล์ 20 บาท'); old['timestamp']=int((NOW-timedelta(days=2)).timestamp()*1000)
    assert h.service.handle(old).status=='stale' and h.port.calls==1
    assert h.send(data,'confirm').status=='preview' and not h.sheets.entries

def test_cold_start_restores_draft_recent_context_and_button(tmp_path):
    state=SQLiteState(tmp_path/'conversation.sqlite','mock-owner'); h=Harness(state=state)
    r=h.expense(); h2=Harness(state=SQLiteState(tmp_path/'conversation.sqlite','mock-owner'),sheets=h.sheets)
    h2.now=h.now; h2.counter=h.counter
    assert h2.click(r,'บันทึก').status=='recorded' and len(h.sheets.entries)==1
    assert len(h2.state.recent_turns)==2 and h2.port.calls==0

def test_expiration_cancels_buttons_and_recent_context_without_extending_draft_ttl():
    h=Harness(); r=h.expense(); expiry=h.state.pending.expires_at
    h.send('แก้ยอด 25 บาท','edit_draft',entries=[{'amount':'25'}])
    assert h.state.pending.expires_at==expiry
    h.now=expiry+timedelta(seconds=1)
    assert h.click(r,'บันทึก').status=='stale' and not h.state.pending
    assert not h.state.recent_turns

def test_migrates_legacy_pending_only_on_new_owner_input_and_reconfirms():
    h=Harness(); h.state.pending=Pending('item','12345678','legacy',int(NOW.timestamp()*1000),NOW+timedelta(minutes=20),
        {'entries':[{'item':'ค่ากระดาษ','amount':'228','date':'2026-10-10','note':'','kind':'expense'}], 'index':0,'raw_items':['ค่ากระดาษ']})
    r=h.send('เพิ่มในหมวด office','enroll_item',category_text='office')
    assert r.data['stage']=='subcategory' and not h.sheets.entries
    assert h.state.pending.origin_event_id=='legacy'
    assert h.state.pending.expires_at==NOW+timedelta(minutes=20)

@pytest.mark.parametrize('phase', ['prepared','executing','effect_returned','action_completed','receipt_completed'])
def test_crash_and_replay_preserve_one_atomic_master_expense_pair(phase):
    h=Harness(); r=h.expense('ขนม','50'); r=h.click(r,'เพิ่มรายการใหม่'); r=h.click(r,'Food')
    data=r.data['line_message']['contents']['footer']['contents'][0]['action']['data']; ev=h.event(postback=data)
    class Crash(BaseException): pass
    def hook(current,key):
        if current==phase: raise Crash()
    h.service.phase_hook=hook
    with pytest.raises(Crash):h.service.handle(ev)
    h.service.phase_hook=lambda *args:None
    assert h.service.handle(ev).status=='recorded'
    assert h.sheets.atomic_batches==1 and len(h.sheets.entries)==1
    assert h.sheets.rows.count(('ขนม','Food',''))==1

def test_conflicting_existing_new_item_mapping_is_not_overwritten():
    h=Harness(); r=h.expense('ขนม','50'); r=h.click(r,'เพิ่มรายการใหม่'); r=h.click(r,'Food')
    h.sheets.rows.append(('ขนม','Transportation',''))
    assert h.click(r,'บันทึก').status=='conflict'
    assert not h.sheets.entries and ('ขนม','Transportation','') in h.sheets.rows

@pytest.mark.parametrize('text, expected', [('วันที่ 10 มีค่ากระดาษ 228 บาท',None), ('วันที่ 10 มีค 228 บาท','2026-03-10'),
                                         ('วันที่ 10 ตค มีค่ากระดาษ 228 บาท','2026-10-10')])
def test_month_abbreviation_requires_a_boundary(text,expected):
    value=dates(text,NOW).value
    assert (value.isoformat() if value else None)==expected

def test_partial_month_and_invalid_date_evidence():
    value,parts,_=resolve('วันที่ 10 มีค่ากระดาษ 228 บาท',NOW)
    assert value is None and parts=={'day':10}
    assert resolve('ตุลาคม',NOW,parts)[0]=='2026-10-10'
    assert resolve('วันที่ 31 กพ',NOW)[0] is None
    assert resolve('วันที่ 20 ตค',NOW)[0] is None
    assert resolve('ตุลาคม 2025',NOW,{'day':10})[0]=='2025-10-10'
    assert resolve('ตค 2569',NOW,{'day':10})[0]=='2026-10-10'

def test_context_is_bounded_no_authorization_or_history_api_fields():
    h=Harness(); h.expense(); request=h.port.bodies[0]
    assert request['tools']==tools() and request['store'] is False
    assert not {'conversation','previous_response_id'} & set(request)
    assert len(json.dumps(request,ensure_ascii=False,separators=(',',':')).encode())<=6144
    payload=json.loads(request['input'][0]['content'])
    assert set(payload['context'])=={'pending','recent','choices'}
    with pytest.raises(ValueError):body('a',NOW,{'pending':None,'recent':[],'choices':{'items':[],'categories':[]},'owner_id':'bad'})

@pytest.mark.parametrize('change',['confirmed','owner','extra_call','output_text','wrong_model','usage','duplicate'])
def test_intent_response_cannot_introduce_permissions_or_free_text_claims(change):
    args=decision('confirm'); value=response('interpret_message',args)
    if change in {'confirmed','owner'}:
        args[change]=True; value=response('interpret_message',args)
    elif change=='extra_call':value['output']*=2
    elif change=='output_text':value['output']=[{'type':'message','content':'saved'}]
    elif change=='wrong_model':value['model']='other'
    elif change=='usage':value['usage']['input_tokens']=8193
    else:value['output'][0]['arguments']='{"intent":"confirm","intent":"expense"}'
    with pytest.raises((ValueError,OverflowError)):parse(value)

def test_context_reuses_unchanged_fields_but_still_revalidates_new_source():
    h=Harness(); h.expense(date='2026-10-12')
    r=h.send('แก้ยอดเป็น 25 บาท','edit_draft',entries=[{'item':'รถเมล์','amount':'25','date':'2026-10-12','note':''}])
    assert r.status=='preview' and '25' in r.message

def test_model_march_guess_on_day_only_text_still_gets_targeted_month_question():
    h=Harness()
    r=h.send('วันที่ 10 มีค่ากระดาษ 228 บาท','expense',entries=[{'item':'ค่ากระดาษ','amount':'228','date':'2026-03-10'}])
    assert r.status=='clarification' and 'เป็นเดือนใด' in r.message
    assert h.state.pending.payload['entries'][0]['date'] is None

@pytest.mark.parametrize('text', ['วันที่ 10 ตค หรือวันที่ 11 ตค', 'วันที่ 10 ตค ปีที่แล้ว', 'วันที่ 10 ตค และ 2026-10-09'])
def test_conflicting_complete_dates_are_not_rebuilt_from_first_day_month(text):
    assert dates(text,NOW).value is None and resolve(text,NOW)[0] is None
    h=Harness(); r=h.send(text+' รถเมล์ 20 บาท','expense',entries=[{'item':'รถเมล์','amount':'20','date':'2026-10-10'}])
    assert r.status in {'invalid','clarification'} and not h.sheets.entries

def test_recorded_edit_source_guard_handles_model_misclassification():
    h=Harness(); text='แก้รายจ่ายรถเมล์ที่บันทึกแล้วเป็น 30 บาท'
    assert h.send(text,'expense',entries=[{'item':'รถเมล์','amount':'30'}]).status=='unsupported'
    assert h.send('บันทึก','confirm').status=='no_pending' and not h.sheets.entries

def test_note_is_in_full_review_before_confirmation():
    h=Harness(); r=h.send('รถเมล์ 20 บาท หมายเหตุ ไปตลาด','expense',entries=[{'item':'รถเมล์','amount':'20','note':'ไปตลาด'}])
    assert 'หมายเหตุ: ไปตลาด' in r.message
    assert h.click(r,'บันทึก').status=='recorded' and h.sheets.entries[0].note=='ไปตลาด'

def test_edit_button_and_incomplete_correction_suspend_old_confirmation():
    h=Harness(); original=h.expense(); r=h.click(original,'แก้ไข')
    assert r.data['stage']=='correction'
    assert h.click(original,'บันทึก').status=='stale'
    assert h.send('บันทึก','confirm').status=='clarification' and not h.sheets.entries
    r=h.send('แก้ยอดเป็น 25 บาท','edit_draft',entries=[{'amount':'25'}])
    assert r.status=='preview' and h.click(r,'บันทึก').status=='recorded'

def test_incomplete_correction_text_requires_a_new_valid_preview():
    h=Harness(); original=h.expense()
    assert h.send('แก้ยอดเป็น','edit_draft').data['stage']=='correction'
    assert h.click(original,'บันทึก').status=='stale'
    assert h.send('บันทึก','confirm').status=='clarification' and not h.sheets.entries

def test_invalid_date_cannot_default_to_today_on_later_amount_answer():
    h=Harness(); r=h.send('วันที่ 31 กพ รถเมล์ 20 บาท','expense',entries=[{'item':'รถเมล์','amount':'20'}])
    assert r.status=='clarification'
    r=h.send('แก้ยอด 25 บาท','edit_draft',entries=[{'amount':'25'}])
    assert r.data['stage']=='date' and h.state.pending.payload['entries'][0]['date'] is None

def test_ambiguous_multi_entry_correction_suspends_confirmation():
    h=Harness(); r=h.send('รถเมล์ 20 บาท\nกาแฟ 50 บาท','expense',entries=[{'item':'รถเมล์','amount':'20'},{'item':'กาแฟ','amount':'50'}])
    assert r.status=='preview'
    result=h.send('แก้ยอดเป็น 30 บาท','edit_draft',entries=[{'amount':'30'}])
    assert result.status=='clarification' and not h.sheets.entries
    assert h.click(r,'บันทึก').status=='stale'

def test_large_taxonomy_is_compacted_to_prompt_byte_bound():
    h=Harness(); context=h.service._context(h.sheets.taxonomy(),'hello')
    context['choices']['categories']=[{'id':f'cat{i}', 'label':f'Category{i}',
        'subcategories':[{'id':f's{i}_{j}','label':f'Subcategory {j}'} for j in range(8)]} for i in range(8)]
    request=body('hello',NOW,context)
    assert len(json.dumps(request,ensure_ascii=False,separators=(',',':')).encode())<=6144
    assert len(json.loads(request['input'][0]['content'])['context']['choices']['categories'])<=8

def test_nested_context_cannot_disclose_unreviewed_fields():
    h=Harness(); h.expense(); context=h.service._context(h.sheets.taxonomy(),'hello')
    context['pending']['entries'][0]['owner_id']='not-allowed'
    with pytest.raises(ValueError):body('hello',NOW,context)
