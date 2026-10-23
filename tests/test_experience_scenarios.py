"""Multi-turn acceptance scenarios. Scripted planning, not language accuracy."""
from copy import deepcopy
import json
import pytest
from test_raw_history import Harness
from test_history_first import response
from test_pending_records import patch
from test_weekday_enrollment_v11 import harness,active
from test_catalog_names_v09 import prepare,entry


def test_one_missing_amount_then_interruption_then_short_answer_and_single_save():
    h=Harness()
    ask=h.prepare('ค่ากระดาษ',amount=None)
    assert ask.status=='clarification' and ask.message=='ค่ากระดาษ จ่ายเท่าไรครับ?'
    before=deepcopy(h.draft())
    unrelated=h.send('พักก่อน คุยเรื่องอื่น',response(text='ได้ครับ'))
    assert unrelated.status=='answer' and h.draft()==before
    review=h.send('ค่ากระดาษ 47 บาท',patch({0:[('amount','47','47')]}))
    assert review.status=='preview' and h.draft()['entries'][0]['item']=='ค่ากระดาษ',review
    assert h.draft()['entries'][0]['date']==before['entries'][0]['date'] and not h.sheets.entries
    event=h.event(review.data['confirm_command']);saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==1
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==1


@pytest.mark.parametrize('answer',['Food','ของกิน','หมวดอาหาร'])
def test_rename_compound_category_then_short_sibling_answer_reaches_review(answer):
    h=harness();first='ขนมทดสอบออโรรา';second='เครื่องดื่มทดสอบเวกา'
    prepare(h,first+' 47 บาท '+second+' 53 บาท',[entry(item=first,amount=47),entry(item=second,amount=53)])
    result=h.send('เปลี่ยนชื่ออันแรกเป็น ร้านทดสอบเนบิวลา แล้วจัดเป็นอาหาร',
        patch({0:[('item','ร้านทดสอบเนบิวลา','ร้านทดสอบเนบิวลา'),('category','Food/Grocery','อาหาร')]}))
    assert result.status=='clarification'
    result=h.send(answer,patch({1:[('category','Food/Grocery',answer)]}))
    assert result.status=='preview',result
    entries=active(h)['entries']
    assert [e['item'] for e in entries]==['ร้านทดสอบเนบิวลา',second]
    assert [e['amount'] for e in entries]==['47','53']
    assert all(e['mapping']['category']=='Food/Grocery' for e in entries) and not h.sheets.entries


def test_clarification_asks_one_field_and_keeps_all_remaining_draft_facts():
    h=Harness()
    result=h.prepare('ค่ากระดาษ',amount=None)
    context=json.loads(h.port.requests[0]['input'][0]['content'])
    assert context['message']=='ค่ากระดาษ' and result.message.count('?')==1
    assert 'วันที่' not in result.message and 'ยังอยู่' not in result.message
    assert h.draft()['entries'][0]['date'] and not h.sheets.entries
