"""Synthetic multi-item regressions for delivered catalog questions."""
from copy import deepcopy
import pytest

from line_assistant.history_context import HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history
from test_weekday_enrollment_v11 import harness,active,enroll
from test_catalog_names_v09 import prepare,entry
from test_history_first import response
from test_pending_records import patch

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    def deny(*args,**kwargs):raise AssertionError('Synthetic adapters only')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)

NOODLE='ก๋วยเตี๋ยวต้มยำทดสอบ'
RENAMED='ก๋วยเตี๋ยวต้มยำโรงพยาบาลทดสอบ'
KEBAB='เคบับทดสอบ'


def pending():
    h=harness()
    prepare(h,f'{NOODLE} 47 บาท {KEBAB} 53 บาท',
        [entry(item=NOODLE,amount=47),entry(item=KEBAB,amount=53)])
    return h


@pytest.mark.parametrize('text',[f'{KEBAB}เพิ่มใหม่ เป็น Food',f'เพิ่ม{KEBAB}ไปในหมวด Food ไง'])
def test_compound_new_name_and_category_uses_offered_alias_without_an_extra_turn(text):
    h=pending()
    enroll(h,'เพิ่มใหม่',entry_ref=active(h)['entries'][0]['entry_ref'])
    enroll(h,'Food',step='choose_category',category='Food/Grocery')
    before=deepcopy(active(h))
    r=enroll(h,text,entry_ref=before['entries'][1]['entry_ref'],category='Food/Grocery')
    assert r.status=='preview',r
    assert [e['mapping']['category'] for e in active(h)['entries']]==['Food/Grocery']*2
    assert [e['amount'] for e in active(h)['entries']]==['47','53']
    assert not h.sheets.entries


def rejected(h):
    # Model invents an unselected category. This deliberately keeps the old
    # pre-enrollment state while displaying the server's category question.
    r=enroll(h,'เพิ่มใหม่',category='Food/Grocery')
    assert r.data['tool_result']['error']=='new_item_requires_category_clarification'
    return r


@pytest.mark.parametrize('text',['Food','หมวด Food',f'เพิ่ม{NOODLE}ไปในหมวด Food ไง'])
def test_failed_question_accepts_literal_alias_through_the_shared_pending_schema(text):
    h=pending();rejected(h);before=deepcopy(active(h));requests=len(h.port.requests)
    r=h.send(text,patch({0:[('category','Food/Grocery','Food')]}))
    assert len(h.port.requests)==requests+1
    assert r.status=='clarification'
    assert active(h)['entries'][0]['mapping']['category']=='Food/Grocery'
    assert 'enrollment' not in active(h)['entries'][1]
    for original,current in zip(before['entries'],active(h)['entries']):
        for key in ['item','amount','date','note','source_quote','fact_sources','amount_occurrence']:
            assert original.get(key)==current.get(key)
    assert not h.sheets.entries and active(h)['review'] is None


def test_full_dialogue_applies_literal_rename_and_saves_two_rows_once_after_review():
    h=pending();original=deepcopy(active(h));calls=len(h.port.requests)
    r=h.send(f'รายการใหม่ เพิ่มเป็น {RENAMED} ได้มั้ย',patch({0:[('item',RENAMED,RENAMED),('catalog_action','new','รายการใหม่')]}))
    assert len(h.port.requests)==calls+1
    assert r.data['tool_result']['item_enrollment']['field']=='category'
    assert r.data['tool_result']['item_enrollment']['item']==RENAMED
    assert active(h)['entries'][0]['item']==RENAMED
    for before,after in zip(original['entries'],active(h)['entries']):
        for key in ['entry_ref','amount','date','note','amount_occurrence']:
            assert before.get(key)==after.get(key)
    enroll(h,'Food',step='choose_category',category='Food/Grocery')
    r=enroll(h,f'{KEBAB}เพิ่มใหม่ เป็น Food',entry_ref=active(h)['entries'][1]['entry_ref'],category='Food/Grocery')
    assert r.status=='preview',r
    assert [e['item'] for e in active(h)['entries']]==[RENAMED,KEBAB]
    assert not h.sheets.entries
    event=h.event(r.data['confirm_command']);saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==2
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==2
    assert (RENAMED,'Food/Grocery','') in h.sheets.rows
    assert (NOODLE,'Food/Grocery','') not in h.sheets.rows


def test_failed_question_recovery_survives_cold_restore():
    h=pending();rejected(h)
    state=HistoryState();state.clock=lambda:h.now
    state.enable_raw_history(journal=deepcopy(h.state.raw_journal))
    restore_history(state,history_snapshot(h.state),'mock-owner')
    fresh=type(h)(state=state)
    fresh.sheets.rows=deepcopy(h.sheets.rows)
    fresh.now=h.now;fresh.counter=h.counter
    result=fresh.send('Food',patch({0:[('category','Food/Grocery','Food')]}))
    assert result.status=='clarification'
    assert active(fresh)['entries'][0]['mapping']['category']=='Food/Grocery'
    assert not fresh.sheets.entries


@pytest.mark.parametrize('category',[None,'Food','Food/Grocery'])
def test_repeated_begin_with_literal_category_answer_continues_the_existing_question(category):
    h=pending();enroll(h,'เพิ่มใหม่')
    before=deepcopy(active(h))
    r=enroll(h,'Food',category=category)
    assert r.status=='clarification'
    assert active(h)['version']==before['version']+1
    assert active(h)['entries'][0]['mapping']['category']=='Food/Grocery'
    assert 'enrollment' not in active(h)['entries'][1] and not h.sheets.entries


def test_ambiguous_alias_after_failure_keeps_the_category_question_open():
    h=pending();h.sheets.rows.append(('ชื่อหมวดสมมติ','Food/Drink',''))
    rejected(h);before=deepcopy(active(h))
    h.send('Food',response(text='synthetic needs a unique category'))
    assert active(h)==before and not h.sheets.entries


def test_alias_cannot_overwrite_a_different_explicit_model_choice():
    h=pending();enroll(h,'เพิ่มใหม่');before=deepcopy(active(h))
    r=enroll(h,'Food',category='Gift')
    assert r.status!='preview' and active(h)==before and not h.sheets.entries


@pytest.mark.parametrize('text',[f'อย่าเพิ่มเป็น {RENAMED}',f'สมมติเพิ่มเป็น {RENAMED}',
    f'"เพิ่มเป็น {RENAMED}"',f'ใช้ชื่อ {RENAMED} หมายเหตุ ทดสอบ'])
def test_non_action_or_mixed_field_rename_is_left_for_the_existing_planner(text):
    h=pending();before=deepcopy(active(h));calls=len(h.port.requests)
    h.send(text,response(text='synthetic clarification'))
    assert len(h.port.requests)==calls+1 and active(h)==before and not h.sheets.entries


def test_signed_cold_factory_renames_and_recovers_failed_category_before_confirm_once():
    from datetime import datetime
    import json
    from fastapi.testclient import TestClient
    from test_response_realization import VoiceHosted
    from test_calendar_text_confirmation_v13 import turn
    api=VoiceHosted();api.now=datetime.fromisoformat('2026-10-16T10:00:00+00:00')
    api.google.cells={k:v for k,v in api.google.cells.items() if k[0]!='Database' or k[1]==1}
    for row,values in enumerate(harness().sheets.rows,2):
        for col,value in enumerate(values,1):api.google.cells[('Database',row,col)]=value
    def current():return next(d for d in api.snapshot()['history24']['current']['drafts'].values() if d['status']!='recorded')
    def create(body):
        c=json.loads(body['input'][0]['content'])
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,
            'entries':[{'item':name,'amount':amount,'source_refs':[c['current_source_ref']]} for name,amount in [(NOODLE,47),(KEBAB,53)]]})
    turn(api,f'{NOODLE} 47 บาท {KEBAB} 53 บาท',create)
    r,_=turn(api,f'รายการใหม่ เพิ่มเป็น {RENAMED} ได้มั้ย',patch({0:[('item',RENAMED,RENAMED),('catalog_action','new','รายการใหม่')]}))
    assert current()['entries'][0]['item']==RENAMED
    d=current()
    turn(api,'Food',response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],
        'entry_ref':d['entries'][0]['entry_ref'],'resolution_step':'begin_new_item','category':'Food','subcategory':None,'existing_item':None}))
    d=current()
    failed,_=turn(api,'เพิ่มใหม่',response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],
        'entry_ref':d['entries'][1]['entry_ref'],'resolution_step':'begin_new_item','category':'Food/Grocery','subcategory':None,'existing_item':None}))
    assert failed.data['tool_result']['error']=='new_item_requires_category_clarification'
    calls=len(api.model_bodies)
    review,_=turn(api,'หมวด Food',patch({1:[('category','Food/Grocery','Food')]}))
    assert review.status=='preview' and len(api.model_bodies)==calls+2
    assert [e['item'] for e in current()['entries']]==[RENAMED,KEBAB]
    assert [e['amount'] for e in current()['entries']]==['47','53']
    assert not api.google.sheet_writes and not api.calendar.posts
    saved,event=turn(api,'ยืนยัน')
    assert saved.status=='recorded' and api.google.sheet_writes==1
    calls=len(api.model_bodies)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.google.sheet_writes==1 and len(api.model_bodies)==calls


@pytest.mark.parametrize('text',['ไม่เอาหมวด Food','"Food"','สมมติเลือกหมวด Food','Food หรือ Gift','เดือนที่แล้วใช้ Food เท่าไหร่'])
def test_unconfirmed_or_unrelated_text_does_not_bind_failed_category_question(text):
    h=pending();rejected(h);before=deepcopy(h.state.drafts)
    h.send(text,response(text='synthetic unrelated response'))
    assert h.state.drafts==before and not h.sheets.entries


@pytest.mark.parametrize('damage',['expired','version','undelivered','fingerprint'])
def test_unverified_question_never_binds_an_alias(damage):
    from datetime import timedelta
    from line_assistant.state import Reply
    h=pending();rejected(h)
    last=next(r for r in reversed(h.state.history) if r['role']=='assistant')
    if damage=='expired':
        receipt=h.state.receipts[last['event']]
        h.state.receipts[last['event']]=type(receipt)(receipt.fingerprint,receipt.result,h.now-timedelta(seconds=1))
    elif damage=='version':active(h)['version']+=1;h.service._seal(active(h))
    elif damage=='fingerprint':active(h)['entries'][0]['amount']='999'
    else:
        h.service.production=True
        h.state.replies[last['event']]=Reply(status='failed',content_hash='synthetic',expires_at=h.now+timedelta(hours=1))
    before=deepcopy(h.state.drafts)
    h.send('Food',response(text='synthetic no binding'))
    assert h.state.drafts==before and not h.sheets.entries
