"""Offline natural-context tools and independently authorized owner actions."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json,socket
import pytest
from fastapi.testclient import TestClient
from line_assistant.history_context import source_ref
from line_assistant.taxonomy import identifier
from line_assistant.state import Action
from test_history_first import Harness as Base,response,data
from test_history_enrollment import enrollment
from test_production_history import Hosted

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Semantic tests cannot access real providers')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

class Harness(Base):
    def __init__(self):super().__init__(semantic_enabled=True,item_enrollment_enabled=True)

def control(op='confirm',ref=None,version=None):
    return response(name='control_expense_draft',args={'operation':op,'draft_ref':ref,'draft_version':version})

def button(h,result,label='บันทึก'):
    ev=h.event('owner button');ev.pop('message');ev['type']='postback'
    ev['postback']={'data':next(b['action']['data'] for b in result.data['line_message']['contents']['footer']['contents'] if b['action']['label']==label)}
    return ev

def summarize(start='2026-10-12',end=None,refs=None):
    return response(name='spending_summary',args={'start_date':start,'end_date':end or start,'source_refs':refs})

@pytest.mark.parametrize('text',['สรุปยอดเมื่อวาน','เมื่อวานใช้เงินไปทั้งหมดเท่าไร','ขอดูยอดค่าใช้จ่ายของวันก่อนหน่อย','สรุปรายจ่ายเมื่อวาน'])
def test_read_only_summary_runs_directly_with_actual_mock_totals(text):
    h=Harness();h.prepare('เมื่อวานค่ากระดาษ 228');h.send('ยืนยัน')
    calls=len(h.port.requests);r=h.send(text,summarize())
    assert r.status=='summary' and r.data['tool_result']['expense']=='228.00'
    assert len(h.port.requests)==calls+1 and len(h.sheets.entries)==1 and '228.00' in r.message

def test_short_yes_after_date_clarification_uses_prior_owner_scope_without_write_authority():
    h=Harness();h.now=datetime(2026,10,14,0,18,tzinfo=timezone.utc)
    h.send('สรุปยอดเมื่อวาน',response(text='หมายถึงวันที่ 13 ตุลาคมใช่ไหมครับ'))
    r=h.send('ใช่',summarize('2026-10-13',refs=[source_ref('history-1')]))
    assert r.status=='summary' and r.data['tool_result']['range']=={'start_date':'2026-10-13','end_date':'2026-10-13'}
    assert not h.sheets.entries and not h.state.actions

@pytest.mark.parametrize('start,end',[('2026-10-13','2026-10-12'),('2025-01-01','2026-10-13'),('0001-01-01','0001-01-01'),('invalid','2026-10-13')])
def test_malformed_or_unbounded_summary_never_calls_business_reader(start,end):
    h=Harness();calls=[];h.sheets.summary=lambda *a:calls.append(a)
    r=h.send('สรุปยอด',summarize(start,end),response(text='ขอช่วงวันที่ที่ชัดเจน'))
    assert r.status=='clarification' and not calls and not h.sheets.entries

def test_summary_evidence_and_untrusted_numeric_results_are_validated():
    h=Harness();r=h.send('สรุปยอดเมื่อวาน',summarize('2026-10-13'),response(text='วันที่ไม่ตรงกับที่ขอ'))
    assert r.status=='clarification' and r.data['tool_result']['error']=='summary_date_evidence_mismatch'
    h=Harness();h.sheets.summary=lambda *a:{'income':'NaN','expense':'999'}
    r=h.send('สรุปยอด',summarize(),response(text='อ่านผลไม่ได้'))
    assert r.status=='clarification' and r.data['tool_result']['error']=='invalid_summary_result'
    h=Harness();r=h.send('สรุปยอด',summarize(refs=['tool_result_fake']),response(text='ต้องมีบริบทเจ้าของ'))
    assert r.status=='clarification' and not h.sheets.entries

@pytest.mark.parametrize('text',['เอาตามนี้เลย','โอเค บันทึกได้','จัดการตามที่แสดงครับ','ยืนยันข้อมูลนี้'])
def test_semantic_confirmation_only_offers_bound_owner_action_and_never_commits(text):
    h=Harness();h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    r=h.send(text,control())
    assert r.status=='preview' and not h.sheets.entries and 'draft_' not in r.message
    ev=button(h,r);calls=len(h.port.requests);saved=h.service.handle(ev)
    assert saved.status=='recorded' and len(h.sheets.entries)==1 and len(h.port.requests)==calls
    assert h.service.handle(deepcopy(ev))==saved and len(h.sheets.entries)==1

@pytest.mark.parametrize('text',['ยืนยัน','ใช่','ใช่ครับ','โอเค บันทึกเลย','โอเคบันทึกเลยค่ะ'])
def test_supported_short_text_confirmation_commits_without_button_or_model(text):
    h=Harness();h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    event=h.event(text);calls=len(h.port.requests);saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==1 and len(h.port.requests)==calls
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==1

@pytest.mark.parametrize('text',['"ใช่"','ไม่ใช่','โอเค บันทึกเลย แต่เปลี่ยนยอดเป็น 75 บาท','ถ้าตอบว่าใช่จะเกิดอะไรขึ้น'])
def test_negative_quoted_hypothetical_or_mixed_text_is_not_write_authority(text):
    h=Harness();h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    r=h.send(text,control(),response(text='ตรวจรายละเอียดให้ชัดเจนก่อน'))
    assert r.status in {'preview','clarification'} and not h.sheets.entries

def test_broader_model_phrase_can_finish_through_text_with_buttons_optional():
    h=Harness();h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    r=h.send('จัดการตามนี้ได้เลยครับ',control())
    assert r.status=='preview' and 'ตอบทางข้อความ' in r.message and not h.sheets.entries
    calls=len(h.port.requests);saved=h.send('ใช่')
    assert saved.status=='recorded' and len(h.sheets.entries)==1 and len(h.port.requests)==calls

def test_mixed_correction_confirm_revokes_old_button_and_requires_changed_review():
    h=Harness();first=h.prepare('กาแฟ 50',item='กาแฟ',amount=50);old=button(h,first)
    r=h.send('เอาเลย แต่เปลี่ยนยอดกาแฟเป็น 75 บาท',control(),response(text='ต้องตรวจยอดใหม่ก่อน'))
    assert r.status=='clarification' and not h.sheets.entries
    assert h.service.handle(old).status=='stale' and not h.sheets.entries
    updated=h.update('ยอดกาแฟเป็น 75 บาท',amount=75)
    assert updated.status=='preview' and h.service.handle(button(h,updated)).status=='recorded'
    assert h.sheets.entries[0].amount=='75'

def test_unrelated_topic_keeps_facts_but_revokes_old_save_capability():
    h=Harness();first=h.prepare('กาแฟ 50',item='กาแฟ',amount=50);old=button(h,first);facts=deepcopy(h.draft()['entries'])
    h.send('ทำไมดาวสว่าง',response(text='ดาวเปล่งแสงเอง'))
    assert h.draft()['entries']==facts and h.service.handle(old).status=='stale'
    reviewed=h.send('กลับเรื่องกาแฟ ทำต่อได้เลย',control('review'))
    assert reviewed.status=='preview' and not h.sheets.entries

@pytest.mark.parametrize('text',['ไม่เอาร่างนี้แล้ว','ยกเลิกอันนี้ให้หน่อย','พักไว้ ไม่ต้องทำรายการนี้'])
def test_semantic_cancellation_needs_no_draft_id_and_keeps_recovery_facts(text):
    h=Harness();h.prepare('กาแฟ 50',item='กาแฟ',amount=50);r=h.send(text,control('cancel'))
    assert r.status=='cancelled' and not h.state.drafts and not h.sheets.entries
    assert r.data['tool_result']['cancelled_draft']['entries'][0]['amount']=='50'
    assert 'draft_' not in r.message

def test_multiple_pending_targets_use_owner_selection_buttons_without_id_templates():
    h=Harness();h.prepare('ค่ากระดาษ 228');h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    r=h.send('เอาอันนั้นเลย',control())
    assert r.status=='clarification' and 'draft_' not in r.message and not h.sheets.entries
    selection=button(h,r,label='2. กาแฟ');review=h.service.handle(selection)
    assert review.status=='preview' and 'กาแฟ 50' in review.message
    assert h.service.handle(button(h,review)).status=='recorded' and len(h.sheets.entries)==1
    assert len(h.state.drafts)==2

def test_uncertain_write_cannot_be_cancelled_by_semantic_tool_or_button():
    h=Harness();first=h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    h.state.actions['uncertain']=Action('add_transactions',{'entries':[]},{},h.now+timedelta(hours=1),h.now+timedelta(days=1),status='uncertain')
    before=deepcopy(h.state.actions)
    r=h.send('ยกเลิก',control('cancel'),response(text='ยังตรวจผลการเขียนไม่เสร็จ'))
    assert r.status=='clarification' and h.state.actions==before and h.state.drafts and not h.sheets.entries

def test_button_tampering_foreign_owner_and_expiry_cannot_write():
    h=Harness();r=h.prepare('กาแฟ 50',item='กาแฟ',amount=50);ev=button(h,r)
    ev['postback']['data']=ev['postback']['data'].replace('|save','|other')
    assert h.service.handle(ev).status=='stale' and not h.sheets.entries
    h=Harness();r=h.prepare('กาแฟ 50',item='กาแฟ',amount=50);ev=button(h,r);ev['source']['userId']='foreign'
    assert h.service.handle(ev).status=='denied' and not h.sheets.entries
    h.now+=timedelta(hours=24);ev=button(h,r)
    assert h.service.handle(ev).status=='stale' and not h.sheets.entries

def test_reference_to_verified_prior_date_needs_no_specific_add_on_word():
    h=Harness();h.prepare('เมื่อวานค่ากระดาษ 228');h.send('ยืนยัน')
    def prepare(body):
        ref=json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(item='กาแฟ',amount=50,source_refs=[ref],date_context='last_confirmed')]})
    r=h.send('ยังมีค่ากาแฟ 50 เป็นวันเดียวกันกับรายการก่อน',prepare)
    assert r.status=='preview' and r.data['tool_result']['entries'][0]['date']=='2026-10-12'

def test_deferred_owned_date_fact_can_update_pending_entry_without_full_restatement():
    h=Harness();h.now=datetime(2026,10,14,0,18,tzinfo=timezone.utc)
    h.prepare('เครป 60 วันนี้',item='เครป',amount=60)
    h.send('เพิ่มรายการ เครป 13ตค 60 บาท',response(name='read_expense_drafts',args={'draft_ref':h.draft()['draft_ref']}),response(name='lookup_catalog',args={'kind':'item','query':'เครป'}))
    assert h.draft()['entries'][0]['date']=='2026-10-14'
    def update(body):
        c=json.loads(body['input'][0]['content']);d=c['available_drafts'][0]
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[data(item=None,amount=None,date='2026-10-13',date_text='13ตค',source_refs=[source_ref('history-2')])]})
    r=h.send('เพิ่มชื่อนี้เลย',update,enrollment())
    assert r.status=='clarification' and 'หมวดไหน' in r.message
    assert h.draft()['entries'][0]['date']=='2026-10-13' and h.draft()['entries'][0]['amount']=='60'

def test_signed_final_factory_save_requires_same_acknowledged_review_message():
    api=Hosted()
    with TestClient(api.app()) as client:
        first=api.create(client);r,_=api.send_text(client,'เอาตามนี้เลย',control())
        assert r.status=='preview' and api.google.sheet_writes==0
        ev=api.button(r,'บันทึก');assert api.send(client,ev).status_code==200
    with TestClient(api.app()) as client:assert api.send(client,ev).status_code==200
    assert api.google.sheet_writes==1
    # Provider acknowledgement of an unrelated/different message is inadequate.
    api=Hosted();app=api.app()
    with TestClient(app) as client:
        r=api.create(client);ev=api.button(r,'บันทึก')
        state=app.state.assistant.state
        from line_assistant.pilot_http import rpc_budget
        with rpc_budget(96),state.session():
            reply=next(iter(state.replies.values()));reply.content_hash='wrong';state.checkpoint()
        assert api.send(client,ev).status_code==200 and api.google.sheet_writes==0

def test_model_cannot_supply_write_consent_or_save_on_an_unrelated_owner_turn():
    h=Harness();h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    r=h.send('สวัสดี',control())
    assert r.status=='preview' and not h.sheets.entries and not h.state.actions
    r=h.send('เอาเลย',response(name='control_expense_draft',args={'operation':'confirm','draft_ref':None,'draft_version':None,'confirmed':True}),response(text='ต้องตรวจร่างก่อน'))
    assert r.status=='clarification' and not h.sheets.entries

def test_cached_older_amount_cannot_overwrite_a_later_owner_correction():
    h=Harness();h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    h.send('กาแฟ 13ตค 50 บาท',response(text='รับข้อความไว้'))
    h.update('แก้ยอดกาแฟเป็น 75 บาท',amount=75)
    def outdated(body):
        d=json.loads(body['input'][0]['content'])['available_drafts'][0]
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[data(item='กาแฟ',amount=50,date='2026-10-13',source_refs=[source_ref('history-2')])]})
    r=h.send('ใช้รายละเอียดก่อนหน้า',outdated,response(text='ยอดเก่าถูกแก้แล้ว'))
    assert r.status=='clarification' and h.draft()['entries'][0]['amount']=='75' and not h.sheets.entries

def test_semantic_name_preparation_accepts_normal_verbs_but_preserves_suffix_modifiers():
    h=Harness();r=h.prepare('ช่วยบันทึกค่ากาแฟ 50 บาทให้หน่อยครับ',item='กาแฟ',amount=50)
    assert r.status=='preview' and 'ต้นทาง:' in r.message and not h.sheets.entries
    h=Harness();r=h.prepare('กาแฟลาเต้ 50 บาท',item='กาแฟ',amount=50)
    assert r.status=='clarification' and not h.state.drafts and not h.sheets.entries

def test_short_catalog_choice_uses_only_offered_existing_names_and_shows_the_change():
    h=Harness()
    def prepare(body):
        ref=json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(item='กาเฟ',amount=60,source_refs=[ref])]})
    r=h.send('กาเฟ 60 บาท',prepare,response(name='lookup_catalog',args={'kind':'item','query':'กาเฟ'}))
    assert r.status=='clarification' and 'กาแฟ' in r.message and h.draft()['entries'][0]['item']=='กาเฟ'
    h.send('ดาวสว่างเพราะอะไร',response(text='ดาวเปล่งแสงเอง'))
    def select(body):
        d=json.loads(body['input'][0]['content'])['available_drafts'][0]
        return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':None,'category':None,'subcategory':None,'existing_item':'กาแฟ'})
    r=h.send('ใช้ชื่อเดิมตัวนั้นครับ',select)
    assert r.status=='preview' and 'กาเฟ → กาแฟ' in r.message and not h.sheets.entries
    assert h.service.handle(button(h,r)).status=='recorded' and h.sheets.entries[0].item=='กาแฟ'

def test_unoffered_catalog_choice_cannot_silently_merge_an_unknown_name():
    h=Harness();h.prepare('ขนมทดลอง 60',item='ขนมทดลอง',amount=60)
    def select(body):
        d=json.loads(body['input'][0]['content'])['available_drafts'][0]
        return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':None,'category':None,'subcategory':None,'existing_item':'กาแฟ'})
    r=h.send('เอาตัวนี้',select,response(text='ขอชื่อที่ต้องการเลือก'))
    assert r.status=='clarification' and h.draft()['entries'][0]['item']=='ขนมทดลอง' and not h.sheets.entries
