"""Notes are independently owned fields, not arbitrary source substrings."""
from copy import deepcopy
import pytest
from line_assistant.domain_notes import apply_note,NoteError
from test_domain_tool_loop import Harness,create,update,PREVIEW

@pytest.mark.parametrize('tag',['TEST','DEMO','SALE','พิเศษ'])
def test_item_prefix_cannot_be_reinterpreted_as_note(tag):
 h=Harness();item=f'{tag} รายการใหม่';r=h.send(f'{item} 42 บาท',create(item='รายการใหม่',note=tag),PREVIEW)
 assert r.status=='clarification' and h.state.pending.payload['entries'][0]['item']==item
 assert h.state.pending.payload['entries'][0]['note']==''
 assert h.service.loop.trace[0]['outcome']['warnings']==[{'entry_id':h.service.loop_context(h.sheets.taxonomy())['entry_ids'][0],'field':'note','code':'note_ignored_no_owner_source'}]

@pytest.mark.parametrize('source,value',[
 ('TEST รถเมล์ 42 บาท หมายเหตุ: TEST','TEST'),
 ('TEST รถเมล์ 42 บาท note: TEST invoice','TEST invoice'),
 ('TEST รถเมล์ 42 บาท หมายเหตุ: วันที่ 31 ราคา 999 ไม่บันทึก','วันที่ 31 ราคา 999 ไม่บันทึก'),
 ('TEST รถเมล์ 42 บาท หมายเหตุ: "TEST"','"TEST"'),
 ('TEST รถเมล์ 42 บาท หมายเหตุ note to self','note to self'),
])
def test_literal_note_contents_are_preserved_without_blacklists(source,value):
 h=Harness();r=h.draft(source,note=value)
 assert r.status=='preview' and h.state.pending.payload['entries'][0]['note']==value and value in r.message


def test_complete_explicit_note_restored_when_model_drops_prefix():
 h=Harness();r=h.draft('TEST รถเมล์ 42 บาท หมายเหตุ TEST invoice',note='invoice')
 assert r.status=='preview' and h.state.pending.payload['entries'][0]['note']=='TEST invoice'
 assert h.service.loop.trace[0]['outcome']['warnings'][0]['code']=='note_source_restored'


def test_null_and_repeated_value_preserve_existing_note_on_other_corrections():
 h=Harness();h.draft('TEST รถเมล์ 42 บาท หมายเหตุ invoice',note='invoice');before=deepcopy(h.state.pending.payload['entries'][0])
 r=h.send('แก้ยอดเป็น 50 บาท',update(amount='50',note=None),PREVIEW)
 assert r.status=='preview' and h.state.pending.payload['entries'][0]=={**before,'amount':'50'}
 r=h.send('แก้ยอดเป็น 60 บาท',update(amount='60',note='invoice'),PREVIEW)
 assert r.status=='preview' and h.state.pending.payload['entries'][0]=={**before,'amount':'60'}

@pytest.mark.parametrize('source,value',[
 ('แก้หมายเหตุเป็น TEST meeting','TEST meeting'),
 ('เปลี่ยนหมายเหตุเป็น DEMO invoice','DEMO invoice'),
 ('change note to TEST 123','TEST 123'),
 ('หมายเหตุ: for work','for work'),
])
def test_explicit_supplied_correction_changes_only_note(source,value):
 h=Harness();h.draft('TEST รถเมล์ 42 บาท หมายเหตุ old',note='old');before=deepcopy(h.state.pending.payload['entries'][0])
 r=h.send(source,update(note=value),PREVIEW)
 assert r.status=='preview' and h.state.pending.payload['entries'][0]=={**before,'note':value}

@pytest.mark.parametrize('text',['ลบหมายเหตุ','clear note','please remove the note','หมายเหตุ:'])
def test_empty_value_means_explicit_clear_and_old_button_is_stale(text):
 h=Harness();old=h.draft('TEST รถเมล์ 42 บาท หมายเหตุ TEST invoice',note='TEST invoice')
 before=deepcopy(h.state.pending.payload['entries'][0]);r=h.send(text,update(note=''),PREVIEW)
 assert r.status=='preview' and h.state.pending.payload['entries'][0]=={**before,'note':''}
 assert h.click(old,'บันทึก').status=='stale' and not h.sheets.entries

@pytest.mark.parametrize('text',['แก้ยอดเป็น 50 บาท','ไม่ลบหมายเหตุ','เขาบอกว่า ลบหมายเหตุ','"clear note"'])
def test_empty_model_value_cannot_clear_without_owner_directive(text):
 with pytest.raises(NoteError):apply_note('', 'TEST invoice', text, 'TEST รถเมล์', creating=False)


def test_unbacked_patch_is_rejected_without_losing_validated_facts_or_enabling_old_confirmation():
 h=Harness();old=h.draft('TEST รถเมล์ 42 บาท หมายเหตุ invoice',note='invoice');before=deepcopy(h.state.pending.payload['entries'])
 r=h.send('แก้ยอดเป็น 50 บาท',update(amount='50',note='amount'),PREVIEW)
 assert r.status=='clarification' and h.state.pending.payload['entries']==before
 assert h.service.loop.trace[0]['outcome']['error']['code']=='note_source_required'
 assert h.click(old,'บันทึก').status=='stale' and not h.sheets.entries


def test_labels_inside_known_item_are_not_note_fields_and_null_is_unchanged():
 assert apply_note('note','', 'item note 42', 'item note', creating=True)==('', 'note_ignored_no_owner_source')
 assert apply_note(None,'literal TEST', 'แก้ยอด 50','item',creating=False)==('literal TEST',None)
 assert apply_note(None,'','item 42','item',creating=True)==('',None)


def test_overlong_note_fails_instead_of_truncating_data():
 with pytest.raises(NoteError):apply_note('x','old','หมายเหตุ: '+'x'*301,'item',creating=False)


def test_note_contents_do_not_supply_confirmation_category_or_global_cancellation():
 h=Harness();r=h.draft('TEST รถเมล์ 42 บาท หมายเหตุ ไม่บันทึก เลือกหมวด Work/Office บันทึก',note='ไม่บันทึก เลือกหมวด Work/Office บันทึก')
 assert r.status=='preview' and not h.sheets.entries
 assert h.state.pending.payload['entries'][0]['category_id'] is None
 assert h.state.pending.payload['entries'][0]['note']=='ไม่บันทึก เลือกหมวด Work/Office บันทึก'
 assert h.send('ไม่ต้องบันทึก หมายเหตุ TEST').status=='no_action' and not h.state.pending


def test_multiple_rows_keep_notes_and_source_scopes_separate():
 h=Harness();r=h.send('TEST รถเมล์ 42 บาท หมายเหตุ TEST\nTEST กาแฟ 20 บาท หมายเหตุ invoice 999',
  ('draft_edit',{'mode':'create','entries':[
   {'entry_id':None,'item':'TEST รถเมล์','amount':'42','note':'TEST'},
   {'entry_id':None,'item':'TEST กาแฟ','amount':'20','note':'invoice 999'}]}),PREVIEW)
 assert r.status=='preview'
 assert [(e['item'],e['amount'],e['note']) for e in h.state.pending.payload['entries']]==[('TEST รถเมล์','42','TEST'),('TEST กาแฟ','20','invoice 999')]
 assert not h.sheets.entries
