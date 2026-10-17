"""Additional financial regressions. Income remains explicitly unsupported."""
from copy import deepcopy
import json,socket
import pytest
from datetime import datetime
from test_calendar_mvp import Harness as CalendarHarness,response
from line_assistant.service import ProviderFailure

class FinancialHarness(CalendarHarness):
    def __init__(self,enabled=False):
        super().__init__(enabled=enabled);self.service=self.app;self.port=self.model
        self.sheets.rows.extend([('รถตู้','Transportation',''),('ค่ากระดาษ','Work/Office','')])
    def draft(self):return next(d for d in self.state.drafts.values() if d['status']!='recorded')
def harness():
    h=FinancialHarness();h.now=datetime.fromisoformat('2026-10-15T16:24:00+00:00');return h
def entry(amount=31,item='รถตู้',**kw):
    return {'entry_ref':None,'item':item,'item_text':None,'amount':amount,'date_text':None,'date_ref':None,'note':None,'source_refs':[],'field_sources':None,**kw}
def prepare(h,text,entries):
    def call(body):
        c=json.loads(body['input'][0]['content']);values=deepcopy(entries)
        for e in values:e['source_refs']=[c['current_source_ref']]
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':values})
    return h.send(text,call,response(text='ขอข้อมูลเพิ่มเติม'))
def names_harness(enabled=False):
    h=FinancialHarness(enabled=enabled);h.now=datetime.fromisoformat('2026-10-16T06:30:00+00:00')
    h.sheets.rows.extend([('grab bike','Transportation',''),('รถรับส่ง','Transportation','')]);return h
def names_entry(item='grab bike',amount=37,**kw):return entry(item=item,amount=amount,**kw)
names_prepare=prepare
def choose(h,item='grab bike',text='ใช่ครับ'):
    d=h.draft()
    def call(body):return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':d['entries'][0]['entry_ref'],'category':None,'subcategory':None,'existing_item':item,'resolution_step':'use_existing_item'})
    return h.send(text,call,response(text='ขอข้อมูลเพิ่มเติม'))

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic financial providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

@pytest.mark.parametrize('text,amounts,date',[
    ('เมื่อวาน รถตู้ 31 บาท และรถตู้ 39 บาท',[31,39],'2026-10-14'),
    ('เมื่อวาน รถตู้ ๓๑ บาท และรถตู้ ๓๙ บาท',[31,39],'2026-10-14'),
    ('วันที่ 14 รถตู้ 31 กับ 39 บาท',[31,39],'2026-10-14'),
    ('รถตู้ 31 บาท รถตู้ 39 บาท ทั้งหมดเมื่อวาน',[31,39],'2026-10-14')])
def test_duplicate_names_distinct_amounts_shared_dates_confirm_and_replay(text,amounts,date):
    h=harness();r=prepare(h,text,[entry(amount=n,date_text='เมื่อวาน' if 'เมื่อวาน' in text else 'วันที่ 14') for n in amounts])
    assert r.status=='preview',r
    d=next(iter(h.state.drafts.values()));assert [(e['item'],e['amount'],e['date']) for e in d['entries']]==[('รถตู้',str(n),date) for n in amounts]
    event=h.event(r.data['confirm_command']);saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==2
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==2

@pytest.mark.parametrize('word',['รายรับ','รับเงิน','เงินเข้า','เงินเดือน','income'])
def test_income_cannot_become_expense_even_if_model_proposes_it(word):
    h=harness();r=prepare(h,word+' รถตู้ 31 บาท',[entry(amount=31)])
    assert r.status=='clarification' and not h.sheets.entries and not h.state.drafts
    assert any(step['result'].get('error')=='income_unconfigured' for step in h.service.trace)

@pytest.mark.parametrize('surface',['grabbike','Grab  Bike','GRABBIKE'])
@pytest.mark.parametrize('enabled',[False,True])
def test_catalog_alias_case_and_spacing_survive_calendar_work(surface,enabled):
    h=names_harness(enabled=enabled)
    if enabled:
        calendar=h.prepare('นัดอ่านหนังสือพรุ่งนี้9:00 30นาที',title='อ่านหนังสือ',date_text='พรุ่งนี้',time_text='9:00',duration_text='30นาที')
        assert calendar.status=='preview'
    r=names_prepare(h,surface+' 37 บาท',[names_entry()])
    assert r.status=='preview' and h.draft()['entries'][0]['item']=='grab bike'
    assert h.draft()['entries'][0]['amount']=='37' and not h.sheets.entries

def test_correction_revokes_old_financial_review_and_keeps_other_occurrence():
    h=harness();old=prepare(h,'รถตู้ 31 กับ 39 บาท',[entry(amount=31),entry(amount=39)])
    d=next(iter(h.state.drafts.values()));second=deepcopy(d['entries'][1])
    def correct(body):
        c=json.loads(body['input'][0]['content']);values=entry(item=None,amount=35,entry_ref=d['entries'][0]['entry_ref'],source_refs=[c['current_source_ref']])
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[values]})
    fresh=h.send('แก้ยอดรายการแรกเป็น 35 บาท',correct)
    assert fresh.status=='preview' and next(iter(h.state.drafts.values()))['entries'][1]==second
    assert h.send(old.data['confirm_command']).status=='stale' and not h.sheets.entries
    assert h.send(fresh.data['confirm_command']).status=='recorded' and len(h.sheets.entries)==2

def test_trailing_unscoped_date_is_not_silently_shared_across_separate_lines():
    h=harness();r=prepare(h,'รถตู้ ๓๑ บาท\nรถตู้ ๓๙ บาท เมื่อวาน',[entry(amount=31,date_text='เมื่อวาน'),entry(amount=39,date_text='เมื่อวาน')])
    assert r.status=='clarification' and not h.sheets.entries

def test_cancelled_financial_draft_cannot_replay_old_confirmation():
    h=harness();r=prepare(h,'รถตู้ 31 บาท',[entry(amount=31)])
    d=next(iter(h.state.drafts.values()))
    def cancel(body):return response(name='control_expense_draft',args={'operation':'cancel','draft_ref':d['draft_ref'],'draft_version':d['version']})
    assert h.send('ยกเลิกร่างรายจ่าย',cancel).status=='cancelled'
    assert h.send(r.data['confirm_command']).status=='stale' and not h.sheets.entries

def test_typo_requires_owner_choice_and_keeps_shared_date_amounts():
    h=names_harness();r=names_prepare(h,'เมื่อวาน gabr bike 37 บาท',[names_entry(item_text='gabr bike',date_text='เมื่อวาน')])
    assert r.status=='clarification' and h.draft()['entries'][0]['amount']=='37' and not h.sheets.entries
    fixed=choose(h,text='หมายถึง grab bike ครับ')
    assert fixed.status=='preview' and h.draft()['entries'][0]['date']=='2026-10-15'

def test_financial_committed_lost_ack_reconciles_duplicate_batch_without_second_append():
    h=harness();r=prepare(h,'เมื่อวาน รถตู้ 31 กับ 39 บาท',[entry(amount=31,date_text='เมื่อวาน'),entry(amount=39,date_text='เมื่อวาน')])
    add=h.sheets.add;calls=[]
    def lost(*args):
        calls.append(1);add(*args);raise RuntimeError('Synthetic acknowledgement lost')
    h.sheets.add=lost;event=h.event(r.data['confirm_command'])
    with pytest.raises(ProviderFailure):h.service.handle(event)
    h.sheets.add=add;got=h.service.handle(deepcopy(event))
    assert got.status=='recorded' and len(calls)==1 and len(h.sheets.entries)==2
    assert h.service.handle(deepcopy(event))==got and len(h.sheets.entries)==2

@pytest.mark.parametrize('count,status',[('สอง','preview'),('สาม','clarification')])
def test_counted_single_name_explicit_distinct_occurrence_prices(count,status):
    h=names_harness(enabled=True)
    text='ช่วยบันทึกค่าใช้จ่ายค่ารถตู้เมื่อวาน'+count+'รายการ รายการหนึ่ง 31 บาท อีกรายการ 39 บาท ทั้งคู่เป็นวันที่ 15 ตุลาคม'
    r=prepare(h,text,[entry(item='ค่ารถตู้',amount=31,date_text='เมื่อวาน'),entry(item='ค่ารถตู้',amount=39,date_text='วันที่ 15 ตุลาคม')])
    assert r.status==status,r
    if status=='preview':
        assert [(e['item'],e['amount'],e['date']) for e in h.draft()['entries']]==[('รถตู้','31','2026-10-15'),('รถตู้','39','2026-10-15')]
    assert not h.sheets.entries

@pytest.mark.parametrize('count,status',[('สอง','preview'),('สาม','clarification')])
def test_repeated_group_count_is_not_an_extra_count_or_monetary_occurrence(count,status):
    h=names_harness(enabled=True)
    r=prepare(h,'ลงค่ารถตู้'+count+'ครั้งให้หน่อยนะ—วันที่ ๑๕ ต.ค. ทั้งสองรายการ รายการแรก ๓๑ บาท อีกรายการ ๓๙ บาท!',[
        entry(item='ค่ารถตู้',amount='๓๑',date_text='วันที่ ๑๕ ต.ค.'),entry(item='ค่ารถตู้',amount='๓๙',date_text='วันที่ ๑๕ ต.ค.')])
    assert r.status==status,r
    if status=='preview':
        assert [(e['item'],e['amount'],e['date']) for e in h.draft()['entries']]==[('รถตู้','31','2026-10-15'),('รถตู้','39','2026-10-15')]
        assert h.send(r.data['confirm_command']).status=='recorded' and len(h.sheets.entries)==2
    else:assert not h.sheets.entries


def test_counted_prices_with_explicit_conversational_save_request():
    h=names_harness(enabled=True)
    r=prepare(h,'เมื่อวานมีค่ารถตู้สองรายการ ช่วยบันทึกเป็น 31 บาทกับ 39 บาท โดยทั้งสองรายการลงวันที่ 15 ตุลาคม',[
        entry(item='ค่ารถตู้',amount=31,date_text='เมื่อวาน'),entry(item='ค่ารถตู้',amount=39,date_text='เมื่อวาน')])
    assert r.status=='preview',r
    assert [e['amount'] for e in h.draft()['entries']]==['31','39'] and not h.sheets.entries

@pytest.mark.parametrize('value,status',[('๓๑ บาท','preview'),('31บาท','preview'),('31 USD','clarification'),('-31 บาท','clarification'),('32 บาท','clarification')])
@pytest.mark.parametrize('enabled',[False,True])
def test_model_amount_unit_never_bypasses_original_occurrence_proof(value,status,enabled):
    h=names_harness(enabled=enabled)
    r=prepare(h,'เมื่อวาน รถตู้ ๓๑ กับ ๓๙ บาท',[entry(amount=value,date_text='เมื่อวาน'),entry(amount='๓๙ บาท',date_text='เมื่อวาน')])
    assert r.status==status,r
    if status=='preview':assert [e['amount'] for e in h.draft()['entries']]==['31','39']
    assert not h.sheets.entries

@pytest.mark.parametrize('count,status',[('สอง','preview'),('สาม','clarification')])
def test_multiple_amount_wording_has_separate_price_proofs_and_shared_date(count,status):
    h=names_harness(enabled=True)
    r=prepare(h,'บันทึกค่ารถตู้'+count+'ยอดให้ที วันที่เดียวกันคือ ๑๕ ต.ค. ยอดหนึ่ง ๓๑ บาท อีกยอด ๓๙ บาท!',[
        entry(item='ค่ารถตู้',amount=31,date_text='๑๕ ต.ค.'),entry(item='ค่ารถตู้',amount=39,date_text='๑๕ ต.ค.')])
    assert r.status==status,r
    if status=='preview':assert [(e['amount'],e['date']) for e in h.draft()['entries']]==[('31','2026-10-15'),('39','2026-10-15')]
    assert not h.sheets.entries


def test_colloquial_count_suffix_and_trailing_shared_date_preserve_price_occurrences():
    h=names_harness(enabled=True)
    r=prepare(h,'เมื่อวานจ่ายค่ารถตู้สองยอดนะ ยอด 31 บาทกับ 39 บาท ลงวันที่ 15 ตุลาคมทั้งคู่',[
        entry(item='ค่ารถตู้',amount=31,date_text='เมื่อวาน'),entry(item='ค่ารถตู้',amount=39,date_text='เมื่อวาน')])
    assert r.status=='preview' and [(e['amount'],e['date']) for e in h.draft()['entries']]==[('31','2026-10-15'),('39','2026-10-15')],r


def test_existential_count_word_does_not_change_full_item_or_separate_prices():
    h=names_harness(enabled=True)
    r=prepare(h,'ค่ารถตู้วันที่ ๑๕ ต.ค. มีสองรายการ: ๓๑ บาท และ ๓๙ บาท ช่วยบันทึกด้วย!',[
        entry(item='ค่ารถตู้',amount=31,date_text='วันที่ ๑๕ ต.ค.'),entry(item='ค่ารถตู้',amount=39,date_text='วันที่ ๑๕ ต.ค.')])
    assert r.status=='preview' and [(e['item'],e['amount'],e['date']) for e in h.draft()['entries']]==[('รถตู้','31','2026-10-15'),('รถตู้','39','2026-10-15')],r
    assert not h.sheets.entries


def test_full_financial_introducer_does_not_absorb_catalog_name_or_prices():
    h=names_harness(enabled=True)
    r=prepare(h,'ช่วยลงค่าใช้จ่ายรถตู้เมื่อวานสองครั้ง ยอดหนึ่ง 31 บาท อีกยอด 39 บาท ทั้งคู่วันที่ 15 ตุลาคม',[
        entry(item='รถตู้',amount=31,date_text='เมื่อวาน'),entry(item='รถตู้',amount=39,date_text='เมื่อวาน')])
    assert r.status=='preview' and [(e['item'],e['amount'],e['date']) for e in h.draft()['entries']]==[('รถตู้','31','2026-10-15'),('รถตู้','39','2026-10-15')],r


def test_following_financial_save_phrase_keeps_original_duplicate_price_proofs():
    h=names_harness(enabled=True)
    r=prepare(h,'เมื่อวานจ่ายค่ารถตู้สองครั้ง ยอด 31 บาทกับ 39 บาท ช่วยลงเป็นค่าใช้จ่ายวันที่ 15 ตุลาคมทั้งคู่',[
        entry(item='ค่ารถตู้',amount=31,date_text='เมื่อวาน'),entry(item='ค่ารถตู้',amount=39,date_text='เมื่อวาน')])
    assert r.status=='preview' and [(e['amount'],e['date']) for e in h.draft()['entries']]==[('31','2026-10-15'),('39','2026-10-15')],r


@pytest.mark.parametrize('count,status',[('สอง','preview'),('สาม','clarification')])
@pytest.mark.parametrize('enabled',[False,True])
def test_explicit_separate_record_request_retains_count_and_original_prices(count,status,enabled):
    h=names_harness(enabled=enabled)
    r=prepare(h,'ค่ารถตู้วันที่ ๑๕ ต.ค. ลงแยก'+count+'ยอดนะ ๓๑ บาท กับ ๓๙ บาท!',[entry(item='ค่ารถตู้',amount=31,date_text='วันที่ ๑๕ ต.ค.'),entry(item='ค่ารถตู้',amount=39,date_text='วันที่ ๑๕ ต.ค.')])
    assert r.status==status,r
    if status=='preview':
        assert [(e['item'],e['amount'],e['date']) for e in h.draft()['entries']]==[('รถตู้','31','2026-10-15'),('รถตู้','39','2026-10-15')]
        assert h.send(r.data['confirm_command']).status=='recorded' and len(h.sheets.entries)==2
    else:assert not h.sheets.entries
