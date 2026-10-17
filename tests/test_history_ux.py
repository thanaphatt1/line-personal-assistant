"""Offline UX regressions: synthetic owners, providers and financial entries only."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest
from fastapi.testclient import TestClient
from test_history_first import Harness,response,data
from test_production_history import Hosted


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('UX regressions must remain offline')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)


@pytest.mark.parametrize('text',['ยืนยัน','ยืนยันครับ','ยืนยัน ค่ะ'])
def test_plain_confirmation_binds_presented_version_without_model_and_replay_is_once(text):
    h=Harness();preview=h.prepare('ค่ากระดาษ 228')
    assert 'พิมพ์ ยืนยัน' in preview.message and 'draft_' not in preview.message
    event=h.event(text);calls=len(h.port.requests)
    result=h.service.handle(event)
    assert result.status=='recorded' and len(h.sheets.entries)==1 and len(h.port.requests)==calls
    assert h.service.handle(deepcopy(event))==result and len(h.sheets.entries)==1
    again=h.send(text)
    assert again.status=='clarification' and len(h.sheets.entries)==1 and len(h.port.requests)==calls


def test_plain_confirmation_needs_disambiguation_with_multiple_pending_drafts():
    h=Harness();h.prepare('ค่ากระดาษ 228');second=h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    calls=len(h.port.requests);r=h.send('ยืนยัน')
    assert r.status=='clarification' and 'หลายร่าง' in r.message and not h.sheets.entries
    assert len(h.port.requests)==calls
    assert h.send(second.data['confirm_command']).status=='recorded' and len(h.sheets.entries)==1


def test_plain_confirmation_cannot_select_an_old_preview_after_other_topic():
    h=Harness();h.prepare('ค่ากระดาษ 228');h.send('สวัสดี',response(text='สวัสดีครับ'))
    calls=len(h.port.requests);r=h.send('ยืนยัน')
    assert r.status=='clarification' and not h.sheets.entries and len(h.port.requests)==calls


def test_plain_confirmation_refuses_tampered_draft_and_expired_review():
    h=Harness();h.prepare('ค่ากระดาษ 228');h.draft()['entries'][0]['amount']='999'
    assert h.send('ยืนยัน').status=='stale' and not h.sheets.entries
    h=Harness();h.prepare('ค่ากระดาษ 228');h.now+=timedelta(hours=24)
    assert h.send('ยืนยัน').status=='clarification' and not h.sheets.entries


def test_plain_confirmation_cannot_predate_review_or_select_an_undelivered_production_preview():
    h=Harness();h.prepare('ค่ากระดาษ 228');event=h.event('ยืนยัน')
    event['timestamp']=int((h.now-timedelta(seconds=1)).timestamp()*1000)
    assert h.service.handle(event).status=='clarification' and not h.sheets.entries
    h=Harness();h.prepare('ค่ากระดาษ 228');h.service.production=True
    assert h.send('ยืนยัน').status=='clarification' and not h.sheets.entries


def test_mixed_confirmation_and_correction_requires_new_review_and_stale_command_stays_invalid():
    h=Harness();old=h.prepare('ค่ากระดาษ 228')
    changed=h.update('ยืนยัน แต่แก้ยอดค่ากระดาษเป็น 250',amount=250)
    assert changed.status=='preview' and not h.sheets.entries
    assert h.send(old.data['confirm_command']).status=='stale' and not h.sheets.entries
    # Re-display the corrected review through a fresh authenticated turn;
    # the stale-command reply itself is not a presented preview.
    changed=h.update('แก้ยอดค่ากระดาษเป็น 250',amount=250)
    assert h.send('ยืนยัน').status=='recorded' and len(h.sheets.entries)==1


def test_empty_lookup_after_incomplete_expense_keeps_draft_and_returns_human_clarification():
    h=Harness()
    def prepare(body):
        c=json.loads(body['input'][0]['content'])
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(item='ขนมทดลอง',amount=60,source_refs=[c['current_source_ref']])]})
    result=h.send('ขนมทดลอง 60',prepare,response(name='lookup_catalog',args={'kind':'item','query':'ขนมทดลอง'}))
    assert result.status=='clarification' and 'ขนมทดลอง 60' in result.message and 'รายการใหม่' in result.message
    assert not result.message.startswith('{') and 'item_resolution' not in result.message
    assert h.draft()['status']=='needs_information' and not h.sheets.entries
    assert result.data['tool_result']['catalog_matches']==[]


@pytest.mark.parametrize('query',['ค่ากระดาษ','ไม่มีชื่อดังกล่าว'])
def test_lookup_only_tool_loop_has_a_meaningful_reply_without_raw_json(query):
    h=Harness();lookup=response(name='lookup_catalog',args={'kind':'item','query':query})
    result=h.send('ค้นหารายการ',lookup,lookup)
    assert result.status=='answer' and 'Database' in result.message
    assert not result.message.startswith('{') and not h.sheets.entries


@pytest.mark.parametrize('text',['{"ok": true, "matches": []}','```json\n{"ok": true, "matches": []}\n```'])
def test_model_copy_of_tool_json_is_rendered_as_a_human_reply(text):
    h=Harness()
    result=h.send('ค้นหารายการ',response(name='lookup_catalog',args={'kind':'item','query':'ไม่มีชื่อดังกล่าว'}),response(text=text))
    assert result.status=='answer' and 'ไม่พบชื่อ' in result.message and not result.message.startswith(('{','```'))


def saved_yesterday(h):
    preview=h.prepare('เมื่อวานค่ากระดาษ 228')
    assert preview.status=='preview'
    assert h.send('ยืนยัน').status=='recorded'


def test_explicit_add_on_reuses_single_immediately_confirmed_date_and_previews_it():
    h=Harness();saved_yesterday(h)
    result=h.prepare('กาแฟ 50 ด้วย',item='กาแฟ',amount=50)
    d=[d for d in h.state.drafts.values() if d['status']!='recorded'][0]
    assert result.status=='preview' and d['entries'][0]['date']=='2026-10-12'
    assert 'วันที่เดียว' in result.message and '2026-10-12' in result.message
    assert h.send('ยืนยัน').status=='recorded' and len(h.sheets.entries)==2


def test_explicit_add_on_date_overrides_previous_date_and_normal_new_entry_defaults_today():
    h=Harness();saved_yesterday(h)
    result=h.prepare('วันนี้กาแฟ 50 ด้วย',item='กาแฟ',amount=50)
    assert result.status=='preview' and result.data['tool_result']['entries'][0]['date']=='2026-10-13'
    h=Harness();saved_yesterday(h)
    result=h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
    assert result.status=='preview' and result.data['tool_result']['entries'][0]['date']=='2026-10-13'


def test_continuation_keeps_confirmed_date_across_bangkok_midnight_within_history_window():
    h=Harness();saved_yesterday(h);h.now+=timedelta(hours=15)
    result=h.prepare('กาแฟ 50 ด้วย',item='กาแฟ',amount=50)
    assert result.status=='preview' and result.data['tool_result']['entries'][0]['date']=='2026-10-12'
    assert '2026-10-12' in result.message and len(h.sheets.entries)==1


def test_continuation_word_inside_a_literal_note_does_not_change_the_date():
    h=Harness();saved_yesterday(h)
    result=h.prepare('กาแฟ 50 หมายเหตุ ซื้อเผื่อเพื่อนด้วย',item='กาแฟ',amount=50,note='ซื้อเผื่อเพื่อนด้วย')
    assert result.status=='preview'
    entry=result.data['tool_result']['entries'][0]
    assert entry['date']=='2026-10-13' and entry['note']=='ซื้อเผื่อเพื่อนด้วย'


def test_expired_context_and_model_suggested_date_cannot_choose_continuation_date():
    h=Harness();saved_yesterday(h);h.now+=timedelta(hours=24)
    result=h.prepare('กาแฟ 50 ด้วย',item='กาแฟ',amount=50)
    assert result.status=='clarification' and 'วันที่เท่าไร' in result.message and len(h.sheets.entries)==1
    h=Harness();saved_yesterday(h)
    result=h.prepare('กาแฟ 50 ด้วย',item='กาแฟ',amount=50,date='2026-10-13')
    assert result.status=='clarification' and result.data['tool_result']['invalid_fields']==['entries[0].date_source']
    assert len(h.sheets.entries)==1


def test_add_on_after_confirmed_batch_with_different_dates_asks_which_date():
    h=Harness()
    def prepare(body):
        ref=json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(source_refs=[ref]),data(item='กาแฟ',amount=50,source_refs=[ref])]})
    assert h.send('เมื่อวานค่ากระดาษ 228\nวันนี้กาแฟ 50',prepare).status=='preview'
    assert h.send('ยืนยัน').status=='recorded' and len(h.sheets.entries)==2
    result=h.prepare('รถเมล์ 1 ด้วย',item='รถเมล์',amount=1)
    assert result.status=='clarification' and 'วันที่เท่าไร' in result.message and len(h.sheets.entries)==2


def test_signed_unknown_add_on_keeps_previous_date_and_explains_new_name_fallback():
    api=Hosted()
    def prepare(body):
        ref=json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(item='ขนมทดลอง',amount=60,source_refs=[ref])]})
    with TestClient(api.app()) as client:
        assert api.create(client,'เมื่อวานค่ากระดาษ 228').status=='preview'
        assert api.send_text(client,'ยืนยัน')[0].status=='recorded'
        result,_=api.send_text(client,'ขนมทดลอง 60 ด้วย',prepare,response(name='lookup_catalog',args={'kind':'item','query':'ขนมทดลอง'}))
        assert result.status=='clarification' and '2026-10-12' in result.message and 'ร่างเดิม' in result.message and 'เพิ่มรายการใหม่' not in result.message
        assert 'ขนมทดลอง 60' in result.message and not result.message.startswith('{')
        assert api.google.sheet_writes==1


@pytest.mark.parametrize('has_saved',[False,True])
def test_add_on_without_adjacent_confirmed_context_asks_date(has_saved):
    h=Harness()
    if has_saved:
        saved_yesterday(h);h.send('สวัสดี',response(text='สวัสดีครับ'))
    result=h.prepare('กาแฟ 50 ด้วย',item='กาแฟ',amount=50)
    assert result.status=='clarification' and 'วันที่เท่าไร' in result.message
    assert result.data['tool_result']['entries'][0]['date'] is None
    assert len(h.sheets.entries)==int(has_saved)


def test_signed_production_recreation_preserves_plain_confirmation_binding_and_continuation():
    api=Hosted()
    with TestClient(api.app()) as client:
        result=api.create(client,'เมื่อวานค่ากระดาษ 228')
        assert result.status=='preview' and 'draft_' not in result.message
    with TestClient(api.app()) as client:
        calls=api.models;result,event=api.send_text(client,'ยืนยัน')
        assert result.status=='recorded' and api.models==calls
    with TestClient(api.app()) as client:
        assert api.send(client,event).status_code==200 and api.google.sheet_writes==1
        result=api.create(client,'ค่ากระดาษ 50 ด้วย',amount=50)
        assert result.status=='preview' and '2026-10-12' in result.message
        assert 'วันที่เดียว' in result.message and api.google.sheet_writes==1


@pytest.mark.parametrize('text',['ค่ากระดาษ 228กาแฟ 50','ค่ากระดาษ 228 กาแฟ 50'])
def test_single_line_multiple_expenses_now_have_individually_grounded_amounts(text):
    h=Harness()
    def prepare(body):
        ref=json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(source_refs=[ref]),data(item='กาแฟ',amount=50,source_refs=[ref])]})
    result=h.send(text,prepare)
    assert result.status=='preview' and not h.sheets.entries
    assert [(e['item'],e['amount']) for e in result.data['tool_result']['entries']]==[('ค่ากระดาษ','228'),('กาแฟ','50')]
