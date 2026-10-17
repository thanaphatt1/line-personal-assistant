"""Local contract proofs; synthetic invalid proposals remain invalid."""
from copy import deepcopy
import json,socket
import pytest
import behavior_support as ev
from test_raw_workflow_v02 import Port
from test_history_first import response,data

TEXTS={'expense':'ค่าวาฟเฟิลทดสอบ 60 บาทเมื่อวาน','begin':'เอาเป็นชื่อใหม่ครับ','topic':'ดาวฤกษ์คืออะไร','food':'อาหารครับ','empty_edit':'เลือกหมวดอาหาร'}
def decision(h,kind):
 if kind in ('prepare','begin'):return ev.scripted(h,'prepare_unknown' if kind=='prepare' else 'begin_new_item')
 if kind=='wildcard_read':return response(name='read_expense_drafts',args={'draft_ref':'*'})
 if kind=='choose_food':
  value=ev.proposal_arguments(h,'begin_new_item');value.update(resolution_step='choose_category',category='Food/Drink',subcategory='Food')
  return response(name='prepare_item_enrollment',args=value)
 if kind=='empty_edit':return response(name='prepare_expenses',args={'draft_ref':current(h)['draft_ref'],'draft_version':current(h)['version'],'entries':[]})
 raise AssertionError(kind)
def harness():return ev.Harness(Port(),'spending_followup')
def turn(h,text,*values,limit=1):
    h.port.values=list(values)
    return h.run_turn(text,limit)[1]
def current(h):return next(iter(h.state.drafts.values()))
def category_pending():
    h=harness();turn(h,TEXTS['expense'],decision(h,'prepare'));turn(h,TEXTS['begin'],decision(h,'begin'))
    return h
def proposed():
    h=category_pending();turn(h,TEXTS['topic'],decision(h,'wildcard_read'));turn(h,TEXTS['food'],decision(h,'choose_food'))
    return h
def multi():
    h=harness()
    def prepare(body):
        c=json.loads(body['input'][0]['content']);ref=c['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[
            data(item='กาแฟ',amount=50,source_refs=[ref],date_context=None),
            data(item='ค่ากระดาษ',amount=100,source_refs=[ref],date_context=None)]})
    result=turn(h,'กาแฟ 50 บาท\nค่ากระดาษ 100 บาท',prepare)
    assert result.status=='preview' and len(current(h)['entries'])==2
    return h
def update(h,text,rows,version=None):
    d=deepcopy(current(h))
    def value(body):
        c=json.loads(body['input'][0]['content']);entries=deepcopy(rows)
        for e in entries:e.setdefault('source_refs',[c['current_source_ref']])
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':version or d['version'],'entries':entries})
    return turn(h,text,value)

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No paid/live access for local interface work')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def test_synthetic_star_read_does_not_mutate_cancel_or_invent_a_category_choice():
    h=category_pending();before=deepcopy(current(h));result=turn(h,TEXTS['topic'],decision(h,'wildcard_read'))
    assert result.status=='clarification' and result.status!='answer' and current(h)==before
    assert not h.sheets.entries and not h.state.actions

@pytest.mark.parametrize('text,answer',[('ดาวฤกษ์คืออะไร','ผมช่วยบันทึกและสรุปรายจ่ายครับ ร่างเดิมยังอยู่')])
def test_expense_scope_reply_keeps_pending_facts_without_tool_or_implicit_consent(text,answer):
    h=category_pending();before=deepcopy(current(h));result=turn(h,text,response(text=answer))
    assert result.status=='answer' and result.message==answer and current(h)==before
    body=h.port.records[-1]['request'];context=json.loads(body['input'][0]['content'])
    assert body['tool_choice']=='auto' and context['message']==text
    assert 'next_tool' not in json.dumps(context['available_drafts']) and 'Advance owned' not in body['instructions']
    assert 'optional background' in body['instructions'] and context['available_drafts'][0]['entries'][0]['amount']=='60'
    assert not h.state.actions and not h.sheets.entries

def test_resume_can_ask_for_missing_category_from_context_without_any_tool_call():
    h=category_pending();before=deepcopy(current(h));turn(h,'คุยเรื่องดาวก่อน',response(text='ผมรองรับงานรายจ่ายครับ'))
    result=turn(h,'กลับมารายการเมื่อกี้',response(text='วาฟเฟิลทดสอบ 60 บาทเมื่อวาน จะใช้หมวดไหนครับ?'))
    assert result.status=='answer' and current(h)==before and not h.sheets.entries

@pytest.mark.parametrize('already_proposed',[False,True])
def test_category_answer_uses_resolution_tool_without_any_expense_entry_array(already_proposed):
    h=proposed() if already_proposed else category_pending();before=deepcopy(current(h))
    def choice(body):
        c=json.loads(body['input'][0]['content']);d=c['available_drafts'][0];item=d['item_resolutions'][0]
        food=next(v for v in item['category_choices'] if v['label']=='Food/Drink')
        return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],
            'entry_ref':item['entry_ref'],'resolution_step':'choose_category','category':food['id'],'subcategory':None,'existing_item':None})
    result=turn(h,'อาหารครับ',choice)
    assert result.status=='preview' and current(h)['version']==before['version']+1
    assert current(h)['entries'][0]['amount']=='60' and current(h)['entries'][0]['date']=='2026-10-13'
    assert 'entries' not in json.loads(h.port.records[-1]['response']['output'][0]['arguments'])
    assert not h.sheets.entries

def test_synthetic_food_proposal_is_preparation_and_cannot_write_without_owner_confirmation():
    h=proposed();d=current(h);receipt=h.state.receipts['synthetic-'+h.case+'-4'].result
    assert receipt.status=='preview' and 'ข้อเสนอ' in receipt.message
    assert d['entries'][0]['mapping']['category']=='Food/Drink'
    assert d['entries'][0]['amount']=='60' and d['entries'][0]['date']=='2026-10-13'
    assert h.service._workflow()[0]['item_resolutions'][0]['classification_status']=='proposal_requires_owner_review'
    assert not h.state.actions and not h.sheets.entries and h.sheets.atomic_batches==0

def test_synthetic_empty_category_update_returns_validation_in_one_call_and_preserves_facts():
    h=proposed();before=deepcopy(current(h));proof=deepcopy(h.state.draft_proofs);calls=len(h.port.records)
    result=turn(h,TEXTS['empty_edit'],decision(h,'empty_edit'))
    assert result.status=='clarification' and result.data['tool_result']['error']=='entries_missing'
    expected={**before,'review':None}  # Existing policy revokes old save controls on a new turn.
    assert len(h.port.records)==calls+1 and current(h)==expected and h.state.draft_proofs==proof
    assert h.service._draft(before['draft_ref'])==expected and not h.sheets.entries
    schema=next(t for t in h.port.records[-1]['request']['tools'] if t['name']=='prepare_expenses')['parameters']['properties']['entries']
    assert schema['minItems']==1

@pytest.mark.parametrize('index,amount,item',[(0,75,'กาแฟ'),(1,110,'ค่ากระดาษ')])
def test_sparse_updates_preserve_omitted_entries_and_all_unchanged_provenance(index,amount,item):
    h=multi();before=deepcopy(current(h));ref=before['entries'][index]['entry_ref']
    result=update(h,'เปลี่ยน'+item+'เป็น '+str(amount)+' บาท',[{'entry_ref':ref,'amount':amount}])
    assert result.status=='preview' and current(h)['version']==before['version']+1
    assert current(h)['entries'][index]['amount']==str(amount) and current(h)['entries'][1-index]==before['entries'][1-index]
    assert current(h)['entries'][index]['date']==before['entries'][index]['date'] and not h.sheets.entries

@pytest.mark.parametrize('case',['duplicate','foreign','stale','forged_source','wrong_amount','invalid_date','invalid_second'])
def test_invalid_sparse_update_is_atomic_but_revokes_old_confirmation(case):
    h=multi();before=deepcopy(current(h));proof=deepcopy(h.state.draft_proofs);ref=before['entries'][0]['entry_ref']
    rows=[{'entry_ref':ref,'amount':75}];text='เปลี่ยนกาแฟเป็น 75 บาท';version=None
    if case=='duplicate':rows*=2
    if case=='foreign':rows[0]['entry_ref']='entry_foreign'
    if case=='stale':version=999
    if case=='forged_source':rows[0]['source_refs']=['msg_forged']
    if case=='wrong_amount':rows[0]['amount']=999
    if case=='invalid_date':rows[0].update(date='2026-10-15',date_text='พรุ่งนี้');text+=' พรุ่งนี้'
    if case=='invalid_second':
        rows.append({'entry_ref':before['entries'][1]['entry_ref'],'amount':999});text+=' และค่ากระดาษเป็น 110 บาท'
    result=update(h,text,rows,version)
    expected=before if case=='stale' else {**before,'review':None}
    assert current(h)==expected and h.state.draft_proofs==proof
    assert h.service._draft(before['draft_ref'])==expected and not h.sheets.entries and not h.state.actions

def test_catalog_changed_cannot_recertify_untouched_rows_under_a_new_catalog_hash():
    h=multi();before=deepcopy(current(h));h.sheets.rows.append(('ขนม','Food/Drink','Food'))
    result=update(h,'เปลี่ยนกาแฟเป็น 75 บาท',[{'entry_ref':before['entries'][0]['entry_ref'],'amount':75}])
    assert result.data['tool_result']['error']=='catalog_changed' and current(h)=={**before,'review':None} and not h.sheets.entries

def test_sparse_date_correction_preserves_amount_and_other_entry():
    h=multi();before=deepcopy(current(h))
    result=update(h,'เปลี่ยนวันที่ของกาแฟเป็นเมื่อวาน',[{'entry_ref':before['entries'][0]['entry_ref'],'date_text':'เมื่อวาน'}])
    assert result.status=='preview' and current(h)['entries'][0]['date']=='2026-10-13'
    assert current(h)['entries'][0]['amount']=='50' and current(h)['entries'][1]==before['entries'][1]
    assert not h.sheets.entries

def test_unknown_category_cannot_change_the_draft_or_catalog():
    h=category_pending();before=deepcopy(current(h))
    result=turn(h,'อาหารครับ',response(name='prepare_item_enrollment',args={'draft_ref':before['draft_ref'],'draft_version':before['version'],
        'entry_ref':before['entries'][0]['entry_ref'],'resolution_step':'choose_category','category':'cat_invented','subcategory':None,'existing_item':None}))
    assert current(h)==before and not h.sheets.entries and not h.state.actions and h.sheets.atomic_batches==0

def test_unrelated_text_and_following_short_acknowledgement_cannot_confirm_a_proposal():
    h=proposed();before=deepcopy(current(h))
    result=turn(h,'โอเค แล้วดาวฤกษ์คืออะไร',response(text='ผมรองรับงานรายจ่ายครับ ร่างเดิมยังอยู่'))
    assert result.status=='answer' and current(h)==before
    result=turn(h,'โอเค บันทึกเลย',response(text='ต้องตรวจร่างอีกครั้งก่อนครับ'))
    assert result.status!='recorded' and not h.sheets.entries and not h.state.actions

def test_a_draft_can_wait_for_a_name_while_ordinary_conversation_continues():
    h=harness()
    def amount(body):
        c=json.loads(body['input'][0]['content'])
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[data(item=None,amount=75,source_refs=[c['current_source_ref']],date_context=None)]})
    turn(h,'จ่ายไป 75 บาท',amount);before=deepcopy(current(h))
    result=turn(h,'คุยเรื่องอวกาศกัน',response(text='ผมรองรับงานรายจ่ายครับ ร่างเดิมยังอยู่'))
    assert result.status=='answer' and current(h)==before and current(h)['entries'][0]['item'] is None
    assert not h.sheets.entries
