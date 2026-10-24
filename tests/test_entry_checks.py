"""Expense reviews show verified duplicates/date sources; conditional adds and alias offers never write by themselves."""
import json,socket
import pytest
from test_history_first import response,data
from test_raw_history import Harness
from test_read_queries import plan
from owner_features_hosted import OwnerHosted,turn,alias


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def add(h,text,flags=None,**entry):
    def call(body):
        c=json.loads(body['input'][0]['content']);e=data(**entry)
        if not e['source_refs']:e['source_refs']=[c['current_source_ref']]
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[e],**(flags or {})})
    return h.send(text,call,response(text='ขอข้อมูลเพิ่มเติมครับ'))


def saved(h,text='ค่ากระดาษ 228 บาท',**entry):
    r=add(h,text,**entry);assert r.status=='preview',r
    assert h.send(r.data['confirm_command']).status=='recorded';return r


def test_review_shows_same_day_duplicate_and_same_name_with_other_amount_before_consent():
    h=Harness();saved(h)
    again=add(h,'ค่ากระดาษ 228 บาท')
    assert again.status=='preview' and 'บันทึกไว้แล้ว: ค่ากระดาษ 228 บาท วันที่ 13 ต.ค. 2026 (1 รายการ)' in again.message
    assert again.data['review_notices'] and len(h.sheets.entries)==1
    h.send('ยกเลิก '+again.data['tool_result']['draft_ref'])
    other=add(h,'ค่ากระดาษ 100 บาท',amount=100)
    assert other.status=='preview' and 'วันเดียวกันมี ค่ากระดาษ บันทึกไว้แล้ว 1 รายการ' in other.message and len(h.sheets.entries)==1


def test_review_states_when_the_date_was_defaulted_to_today():
    r=add(Harness(),'ค่ากระดาษ 228 บาท')
    assert 'ไม่ได้ระบุวัน จึงใช้วันนี้ (13 ต.ค. 2026)' in r.message
    explicit=add(Harness(),'ค่ากระดาษ 228 บาท เมื่อวาน',date_text='เมื่อวาน')
    assert explicit.status=='preview' and 'จึงใช้วันนี้' not in explicit.message


def test_add_only_if_absent_reports_existing_payment_without_any_draft_or_write():
    h=Harness();saved(h);before=dict(h.state.drafts)
    r=add(h,'วันนี้มีค่ากระดาษ 228 บาทหรือยัง ถ้าไม่มีเพิ่มให้',flags={'only_if_absent':True},date_text='วันนี้')
    assert r.status=='answer' and 'มีอยู่แล้วครับ เลยยังไม่ได้เพิ่ม' in r.message and 'บันทึกไว้แล้ว: ค่ากระดาษ 228 บาท' in r.message
    assert 'confirm_command' not in r.data and h.state.drafts.keys()==before.keys() and len(h.sheets.entries)==1


def test_add_only_if_absent_prepares_a_normal_review_when_nothing_matches():
    h=Harness()
    r=add(h,'วันนี้มีค่ากระดาษ 228 บาทหรือยัง ถ้าไม่มีเพิ่มให้',flags={'only_if_absent':True},date_text='วันนี้')
    assert r.status=='preview' and 'ตรวจแล้ว ยังไม่มี ค่ากระดาษ 228 บาท วันที่ 13 ต.ค. 2026' in r.message and not h.sheets.entries
    assert h.send(r.data['confirm_command']).status=='recorded' and len(h.sheets.entries)==1


def test_add_only_if_absent_fails_closed_when_saved_records_cannot_be_read(monkeypatch):
    h=Harness()
    def unavailable(*a,**k):raise RuntimeError('synthetic outage')
    monkeypatch.setattr(h.sheets,'saved_entries',unavailable)
    r=add(h,'ถ้ายังไม่มีค่ากระดาษ 228 บาทวันนี้ เพิ่มให้หน่อย',flags={'only_if_absent':True},date_text='วันนี้')
    assert r.status=='clarification' and 'ยังตรวจไม่ได้' in r.message and not h.state.drafts and not h.sheets.entries


def test_another_payment_can_use_the_day_just_asked_about_and_the_review_says_so():
    h=Harness();saved(h,'ค่ากระดาษ 228 บาท เมื่อวาน',date_text='เมื่อวาน')
    h.send('เมื่อวานใช้อะไรไปบ้าง',plan('expense','2026-10-12',listing=True))
    seen={}
    def call(body):
        c=json.loads(body['input'][0]['content']);ref=next(v['date_ref'] for v in c['date_references'] if v.get('previous_question'))
        seen['ref']=ref
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(item='กาแฟ',amount=50,date_ref=ref,source_refs=[c['current_source_ref']])]})
    r=h.send('เพิ่มอีกอันนึงเป็นกาแฟ 50 บาท',call,response(text='ขอข้อมูลเพิ่มครับ'))
    assert seen and r.status=='preview' and r.data['tool_result']['entries'][0]['date']=='2026-10-12'
    assert 'ใช้วันที่ 12 ต.ค. 2026 ตามที่ถามก่อนหน้า' in r.message and 'จึงใช้วันนี้' not in r.message


def test_another_payment_after_an_already_recorded_check_uses_that_date():
    h=Harness();saved(h,'ค่ากระดาษ 228 บาท เมื่อวาน',date_text='เมื่อวาน')
    found=add(h,'เมื่อวานมีค่ากระดาษ 228 บาทหรือยัง ถ้าไม่มีเพิ่มให้',flags={'only_if_absent':True},date_text='เมื่อวาน')
    assert found.status=='answer' and 'มีอยู่แล้ว' in found.message
    def call(body):
        c=json.loads(body['input'][0]['content']);ref=next(v['date_ref'] for v in c['date_references'] if v.get('previous_question'))
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(amount=100,date_ref=ref,source_refs=[c['current_source_ref']])]})
    r=h.send('เพิ่มอีกอันนึงเป็นค่ากระดาษ 100 บาท',call,response(text='ขอข้อมูลเพิ่มครับ'))
    assert r.status=='preview' and r.data['tool_result']['entries'][0]['date']=='2026-10-12'
    assert 'ตามที่ถามก่อนหน้า' in r.message and 'วันเดียวกันมี ค่ากระดาษ บันทึกไว้แล้ว 1 รายการ' in r.message


def test_question_date_is_offered_only_right_after_that_read():
    h=Harness();h.send('เมื่อวานใช้อะไรไปบ้าง',plan('expense','2026-10-12',listing=True))
    h.send('ขอบคุณครับ',response(text='ยินดีครับ'))
    seen=[]
    def call(body):
        seen.extend(json.loads(body['input'][0]['content'])['date_references']);return response(text='ได้ครับ')
    h.send('เพิ่มกาแฟ 50 บาท',call)
    assert not any(v.get('previous_question') for v in seen)


def resolved(item,literal,amount=88):
    def answer(body):
        c=json.loads(body['input'][0]['content'])
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[{'entry_ref':None,'item':item,'item_text':literal,
            'amount':amount,'date_text':None,'date_ref':None,'note':None,'source_refs':[c['current_source_ref']],'field_sources':None,'previous_price_ref':None}]})
    return answer


def existing(item):
    def answer(body):
        d=json.loads(body['input'][0]['content'])['available_drafts'][0]
        return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':d['entries'][0]['entry_ref'],
            'category':None,'subcategory':None,'existing_item':item,'resolution_step':'use_existing_item'})
    return answer


def test_receipt_offers_to_remember_a_chosen_spelling_and_the_alias_review_still_decides():
    api=OwnerHosted()
    asked,_=turn(api,'สปอติฟาย 88 บาท',resolved('Spotify','สปอติฟาย'))
    assert asked.status=='clarification' and 'สปอติฟาย หมายถึง Spotify' in asked.message
    review,_=turn(api,'ใช่',existing('Spotify'))
    assert review.status=='preview' and 'Spotify' in review.message and api.expense_writes==0
    receipt,_=turn(api,review.data['confirm_command'])
    actions=[b['action'] for b in receipt.data['line_message']['contents']['footer']['contents']]
    offer=[a for a in actions if a['type']=='message']
    assert receipt.status=='recorded' and offer==[{'type':'message','label':'จำชื่อ สปอติฟาย','text':'จำว่า สปอติฟาย คือ Spotify'}]
    assert not api.snapshot()['history24']['current'].get('owner_features_v1',{}).get('aliases')
    proposal,_=turn(api,offer[0]['text'],alias('set',label='สปอติฟาย',item='Spotify'))
    assert proposal.status=='preview' and not api.snapshot()['history24']['current'].get('owner_features_v1',{}).get('aliases')
    turn(api,'ยืนยัน')
    assert api.snapshot()['history24']['current']['owner_features_v1']['aliases']['สปอติฟาย']['item']=='Spotify'
    # Once remembered, the same spelling resolves directly and no offer is repeated.
    again,_=turn(api,'สปอติฟาย 88 บาท',resolved('สปอติฟาย',None))
    saved_again,_=turn(api,again.data['confirm_command'])
    assert saved_again.status=='recorded' and not [b for b in saved_again.data['line_message']['contents']['footer']['contents'] if b['action']['type']=='message']


def test_record_date_button_question_uses_a_readable_date():
    from test_owner_features import record
    from owner_features_hosted import button
    api=OwnerHosted();saved_record=record(api)
    result,_=turn(api,button(saved_record,'date'),postback=True)
    assert result.status=='clarification' and 'ต้องการเปลี่ยนเป็นวันที่เท่าไรครับ?' in result.message
    assert not __import__('re').search(r'\d{4}-\d{2}-\d{2}',result.message)
