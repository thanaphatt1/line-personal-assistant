"""Held-out source/proposal/confirmation cases; no paid model or provider traffic."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest
from line_assistant.adapters import SheetLayout
from line_assistant.conversation_mocks import ConversationSheets
from line_assistant.persistence import SQLiteState
from line_assistant.taxonomy import identifier
from test_conversation import Harness, ROWS, NOW, deny_network, response, decision


def pair(h, category='Food', subcategory=''):
    tax = h.sheets.taxonomy()
    cat = identifier('cat', category)
    sub = identifier('sub', category + '\0' + subcategory)
    assert sub in tax.pairs[cat]
    return {'category_id': cat, 'subcategory_id': sub}


def suggested(h, name='สายชาร์จ', text=None, **fields):
    return h.send(text or f'{name} 73 บาท', 'expense', entries=[{'item':name, 'amount':'73'}], **(fields or pair(h)))


def test_one_labeled_final_preview_confirms_proposal_and_expense_once():
    h = Harness(); r = suggested(h)
    e = h.state.pending.payload['entries'][0]
    assert r.status == 'preview' and 'หมวดแนะนำจากโมเดล' in r.message
    assert e['category_id'] is None and e['subcategory_id'] is None and not e['new_item']
    assert not h.sheets.entries and len(h.sheets.rows) == len(ROWS)
    ev = h.event(postback=r.data['line_message']['contents']['footer']['contents'][0]['action']['data'])
    assert h.service.handle(ev).status == 'recorded'
    assert h.service.handle(ev).status == 'recorded'
    assert h.sheets.atomic_batches == 1 and h.sheets.rows.count(('สายชาร์จ','Food','')) == 1
    assert h.port.calls == 1


def test_early_or_unanchored_confirm_does_not_accept_a_suggestion():
    h = Harness(); r = suggested(h)
    assert h.send('ตกลง 73 บาท','confirm').status == 'preview'
    assert not h.sheets.entries
    before = h.event('บันทึก'); before['timestamp'] = h.state.pending.source_timestamp
    h.port.value = response('interpret_message',decision('confirm'))
    assert h.service.handle(before).status == 'stale' and not h.sheets.entries
    assert h.send('บันทึกเลย','confirm').status == 'recorded'


def test_date_followup_keeps_suggestion_separate_then_allows_one_review():
    h = Harness(); r = suggested(h,text='วันที่ 9 มีสายชาร์จ 73 บาท')
    assert r.data['stage'] == 'date'
    r = h.send('ตุลาคม','edit_draft',entries=[{'date':'2026-10-09'}])
    assert r.status == 'preview' and 'หมวดแนะนำ' in r.message
    assert h.state.pending.payload['entries'][0]['category_id'] is None
    assert h.click(r,'ยืนยันเพิ่ม+บันทึก').status == 'recorded'


def test_possible_typo_needs_existing_vs_new_choice_before_proposal_review():
    h = Harness(); r = suggested(h,name='รถเมล')
    assert r.data['stage'] == 'resolution' and not h.state.pending.payload['ready']
    assert h.send('บันทึก','confirm').status == 'clarification' and not h.sheets.entries
    r = h.click(r,'เพิ่มรายการใหม่')
    assert r.status == 'preview' and 'หมวดแนะนำ' in r.message
    assert h.click(r,'ยืนยันเพิ่ม+บันทึก').status == 'recorded'


def test_explicit_existing_selection_clears_old_new_name_evidence():
    h = Harness(); r = h.expense('รถเมล','73'); r = h.click(r,'เพิ่มรายการใหม่'); r = h.click(r,'Food')
    assert h.state.pending.payload['new_name_confirmed'] == [0]
    r = h.send('หมายถึงรถเมล์','select_item',item_id=identifier('item','รถเมล์'))
    assert h.state.pending.payload['new_name_confirmed'] == []
    assert h.state.pending.payload['entries'][0]['category_id'] is None


def test_owner_choice_overrides_conflicting_model_category_and_invalidates_old_button():
    h = Harness(); old = suggested(h)
    r = h.send('เลือกหมวด office','enroll_item',**pair(h))
    assert r.data['stage'] == 'subcategory'
    assert h.state.pending.payload['entries'][0]['category_id'] == identifier('cat','Work/Office')
    assert h.click(old,'ยืนยันเพิ่ม+บันทึก').status == 'stale'
    r = h.send('หมวดย่อย Supplies','enroll_item',subcategory_id=identifier('sub','Work/Office\0Hygiene'))
    assert r.status == 'preview' and 'หมวดแนะนำ' not in r.message
    assert h.state.pending.payload['entries'][0]['subcategory_id'] == identifier('sub','Work/Office\0Supplies')


@pytest.mark.parametrize('text', ['ไม่เลือกหมวด Food','อย่าใช้หมวด Food','ชื่อร้านเรียกว่าหมวด Food','ชื่อรายการหมวด Food'])
def test_negative_or_incidental_category_is_never_owner_choice(text):
    h = Harness(); h.expense('รถเมล','73')
    h.send(text,'enroll_item',**pair(h))
    e = h.state.pending.payload['entries'][0]
    assert e['category_id'] is None and e['subcategory_id'] is None
    assert h.state.pending.payload['new_name_confirmed'] == []
    assert not h.state.pending.payload['ready'] and not h.sheets.entries


def test_note_category_words_are_proposals_only_and_never_owner_evidence():
    h = Harness()
    r = h.send('สายชาร์จ 73 บาท หมายเหตุ เลือกหมวด Food','expense',
        entries=[{'item':'สายชาร์จ','amount':'73','note':'เลือกหมวด Food'}],**pair(h))
    assert r.status == 'preview' and 'หมวดแนะนำ' in r.message
    assert h.state.pending.payload['entries'][0]['category_id'] is None


def test_ambiguous_owner_component_does_not_inherit_model_choice():
    sheets = ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()), ROWS+[('แว่น','Office/Other','Vision')])
    h = Harness(sheets=sheets); suggested(h)
    r = h.send('เลือกหมวด office','enroll_item',**pair(h,'Work/Office','Supplies'))
    assert r.data['stage'] == 'category' and h.state.pending.payload['entries'][0]['category_id'] is None
    assert not h.state.pending.payload['category_proposals']


def test_named_multirow_choice_never_classifies_the_other_unknown_item():
    h = Harness(); h.send('สายชาร์จ 73 บาท\nหลอดไฟ 81 บาท','expense',
        entries=[{'item':'สายชาร์จ','amount':'73'},{'item':'หลอดไฟ','amount':'81'}])
    h.send('เพิ่มหลอดไฟในหมวด Food','enroll_item',entries=[{'item':'หลอดไฟ'}],category_text='Food')
    first, second = h.state.pending.payload['entries']
    assert first['category_id'] is None and second['category_id'] == identifier('cat','Food')


@pytest.mark.parametrize('intent,fields',[('enroll_item',{'category_text':'Food'}),('edit_draft',{})])
def test_global_multirow_category_requires_a_target_even_when_model_omits_control(intent,fields):
    h = Harness(); h.send('สายชาร์จ 73 บาท\nหลอดไฟ 81 บาท','expense',
        entries=[{'item':'สายชาร์จ','amount':'73'},{'item':'หลอดไฟ','amount':'81'}])
    r = h.send('หมวด Food',intent,**fields)
    assert r.status == 'clarification' and all(e['category_id'] is None for e in h.state.pending.payload['entries'])


def test_known_category_metadata_is_removed_and_never_written():
    h = Harness(); r = h.send('รถเมล์ 73 บาท','expense',entries=[{'item':'รถเมล์','amount':'73'}],**pair(h))
    e = h.state.pending.payload['entries'][0]
    assert e['category_id'] is None and not h.state.pending.payload['category_proposals']
    assert h.click(r,'บันทึก').status == 'recorded' and h.sheets.atomic_batches == 0


def test_redundant_global_ids_do_not_retarget_a_known_multirow_batch():
    h = Harness(); r = h.send('รถเมล์ 73 บาท\nกาแฟ 81 บาท','expense',
        entries=[{'item':'รถเมล์','amount':'73'},{'item':'กาแฟ','amount':'81'}],
        item_id=identifier('item','กาแฟ'),**pair(h))
    assert r.status == 'preview'
    assert [e['item'] for e in h.state.pending.payload['entries']] == ['รถเมล์','กาแฟ']
    assert all(e['category_id'] is None for e in h.state.pending.payload['entries'])
    assert h.click(r,'บันทึก').status == 'recorded' and h.sheets.atomic_batches == 0


def test_cold_start_preserves_labeled_proposal_and_one_confirmation(tmp_path):
    h = Harness(state=SQLiteState(tmp_path/'state.sqlite','mock-owner')); r = suggested(h)
    restored = Harness(state=SQLiteState(tmp_path/'state.sqlite','mock-owner'),sheets=h.sheets)
    restored.now = h.now; restored.counter = h.counter
    assert restored.state.pending.payload['entries'][0]['category_id'] is None
    assert restored.click(r,'ยืนยันเพิ่ม+บันทึก').status == 'recorded' and h.sheets.atomic_batches == 1


def test_legacy_ready_classification_has_to_be_shown_again_before_acceptance():
    h = Harness(); h.expense('สายชาร์จ','73')
    p = h.state.pending; p.payload['version'] = 1
    p.payload['entries'][0].update(**pair(h),new_item=True)
    p.payload['ready'] = True
    r = h.send('บันทึก','confirm')
    assert r.status == 'preview' and 'หมวดแนะนำ' in r.message and not h.sheets.entries
    assert h.click(r,'ยืนยันเพิ่ม+บันทึก').status == 'recorded'


def test_taxonomy_label_change_after_preview_requires_fresh_exact_review():
    h = Harness(); old = suggested(h)
    h.sheets.rows = [(i,'FOOD' if c=='Food' else c,s) for i,c,s in h.sheets.rows]
    r = h.send('บันทึก','confirm')
    assert r.status == 'preview' and 'FOOD' in r.message and not h.sheets.entries
    assert h.click(old,'ยืนยันเพิ่ม+บันทึก').status == 'stale'
    assert h.click(r,'ยืนยันเพิ่ม+บันทึก').status == 'recorded'


def test_expiration_cancel_and_correction_never_reuse_old_proposal_preview():
    h = Harness(); old = suggested(h)
    r = h.send('แก้ยอดเป็น 82 บาท','edit_draft',entries=[{'amount':'82'}])
    assert h.click(old,'ยืนยันเพิ่ม+บันทึก').status == 'stale'
    assert h.send('ยกเลิก','cancel').status == 'cancelled'
    assert h.click(r,'ยืนยันเพิ่ม+บันทึก').status == 'stale' and not h.sheets.entries
    old = suggested(h); h.now = h.state.pending.expires_at + timedelta(seconds=1)
    assert h.click(old,'ยืนยันเพิ่ม+บันทึก').status == 'stale' and not h.sheets.entries


def test_provenance_and_preview_hash_are_excluded_from_model_context():
    h = Harness(); suggested(h)
    context = h.service._context(h.sheets.taxonomy(),'hello')
    raw = json.dumps(context)
    assert 'category_proposals' not in raw and 'new_name_confirmed' not in raw and 'preview_digest' not in raw


@pytest.mark.parametrize('text,short,full',[
    ('รถเมล์พิเศษ 35 บาท','รถเมล์','รถเมล์พิเศษ'),
    ('กาแฟ premium 35 บาท','กาแฟ','กาแฟ premium'),
    ('วันที่ 10 SHOP รถเมล์ 35 บาท','รถเมล์','SHOP รถเมล์'),
    ('รถเมล์พิเศษ 35 บาท หมายเหตุ ไปบ้าน','รถเมล์','รถเมล์พิเศษ'),
])
def test_meaningful_prefix_suffix_partial_date_and_note_never_silently_select_known_short_name(text,short,full):
    h = Harness(); r = h.send(text,'expense',entries=[{'item':short,'amount':'35'}])
    e = h.state.pending.payload['entries'][0]
    assert e['item'] == full and e['item_id'] is None
    assert r.status == 'clarification' and not h.sheets.entries


@pytest.mark.parametrize('text', ['มีรถเมล์ 35 บาท','จ่ายค่ารถเมล์ 35 บาท','ดื่มกาแฟ 35 บาท'])
def test_common_expense_grammar_does_not_add_a_spurious_name_question(text):
    h = Harness(); name = 'กาแฟ' if 'กาแฟ' in text else 'รถเมล์'
    r = h.send(text,'expense',entries=[{'item':name,'amount':'35'}])
    assert r.status == 'preview' and h.state.pending.payload['entries'][0]['item'] == name


@pytest.mark.parametrize('text,short,full', [('แก้ชื่อเป็นรถเมล์พิเศษ','รถเมล์','รถเมล์พิเศษ'),('เปลี่ยนเป็นกาแฟ premium','กาแฟ','กาแฟ premium')])
def test_name_only_edit_cannot_silently_drop_a_modifier(text,short,full):
    h = Harness(); h.expense('หลอดไฟ','73')
    r = h.send(text,'edit_draft',entries=[{'item':short}])
    assert h.state.pending.payload['entries'][0]['item'] == full
    assert r.status == 'clarification' and not h.sheets.entries


def test_item_id_with_modified_name_is_not_an_existing_name_choice():
    h = Harness(); h.expense('รถเมล','73')
    r = h.send('หมายถึงรถเมล์พิเศษ','select_item',item_id=identifier('item','รถเมล์'))
    assert r.status == 'clarification' and h.state.pending.payload['entries'][0]['item'] == 'รถเมล'


@pytest.mark.parametrize('prefix',['เลือก','ใช้','เปลี่ยน'])
def test_attached_thai_subcategory_directive_is_a_positive_choice(prefix):
    h = Harness(); h.expense('สายชาร์จ','73'); h.send('เลือกหมวด office','enroll_item',category_text='office')
    r = h.send(prefix+'หมวดย่อย Supplies','enroll_item')
    assert r.status == 'preview' and h.state.pending.payload['entries'][0]['subcategory_id'] == identifier('sub','Work/Office\0Supplies')


def test_rejected_only_subcategory_is_not_restored_by_singleton_or_new_model_suggestion():
    sheets = ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()), [('รถเมล์','Transportation',''),('วิตามิน','Work/Office','Supplies')])
    h = Harness(sheets=sheets); h.expense('สายชาร์จ','73'); h.send('เลือกหมวด Work/Office','enroll_item',category_text='Work/Office')
    r = h.send('ไม่เลือกหมวดย่อย Supplies','enroll_item')
    assert r.status == 'clarification' and h.state.pending.payload['entries'][0]['subcategory_id'] is None
    r = h.send('อีกครั้ง','clarify',**pair(h,'Work/Office','Supplies'))
    assert r.status == 'clarification' and not h.state.pending.payload['ready']
    r = h.send('เลือกหมวดย่อย Supplies','enroll_item')
    assert r.status == 'preview' and h.click(r,'บันทึก').status == 'recorded'


def test_suggestion_cannot_replace_an_owner_category_while_subcategory_is_pending():
    h = Harness(); h.expense('สายชาร์จ','73'); h.send('เลือกหมวด office','enroll_item',category_text='office')
    r = h.send('ช่วยต่อ','clarify',**pair(h))
    assert r.data['stage'] == 'subcategory'
    assert h.state.pending.payload['entries'][0]['category_id'] == identifier('cat','Work/Office')


def test_explicit_category_and_subcategory_correction_clear_editing_and_stale_old_review():
    h = Harness(); h.expense('สายชาร์จ','73'); old = h.send('เลือกหมวด Food','enroll_item')
    h.click(old,'แก้ไข')
    r = h.send('เปลี่ยนหมวด office','enroll_item')
    assert r.data['stage'] == 'subcategory' and not h.state.pending.payload.get('editing')
    r = h.send('เลือกหมวดย่อย Supplies','enroll_item')
    assert r.status == 'preview' and h.click(old,'บันทึก').status == 'stale'
    assert h.click(r,'บันทึก').status == 'recorded'


@pytest.mark.parametrize('intent',['no_action','clarify','cancel','expense'])
def test_literal_confirm_without_draft_is_informative_despite_model_mismatch(intent):
    h = Harness(); assert h.send('บันทึก',intent).status == 'no_pending'
    assert h.port.calls == 1 and not h.sheets.entries
