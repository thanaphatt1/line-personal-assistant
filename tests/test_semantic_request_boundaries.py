"""Current intent and original owner facts are separate; synthetic services only.

Scripted planner decisions verify execution contracts, not language accuracy.
"""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest

from test_pending_records import pending,patch
from test_weekday_enrollment_v11 import active,harness
from test_catalog_names_v09 import prepare,entry
from test_history_first import response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic services only')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)


def historical(values,refs,**options):
    def cite(args):
        for record in args['records']:
            for change in record['changes']:change['source_ref']=refs[change['field']]
    return patch(values,mutate=cite,**options)


@pytest.mark.parametrize('text,changes',[
    ('ถ้าไม่มีก็เพิ่มใหม่',[('catalog_action','new','ถ้าไม่มีก็เพิ่มใหม่')]),
    ('เปลี่ยนชื่อเป็น ร้านทดสอบใหม่ ได้ไหม?',[('item','ร้านทดสอบใหม่','ร้านทดสอบใหม่')]),
    ('ใช้ชื่อ "ร้านทดสอบใหม่"',[('item','ร้านทดสอบใหม่','ร้านทดสอบใหม่')]),
    ('คิดว่าควรใช้หมวดของกินนะ',[('category','Food/Grocery','ของกิน')]),
    ('อย่าเพิ่งเซฟ ขอเปลี่ยนชื่อเป็น ร้านทดสอบใหม่',[('item','ร้านทดสอบใหม่','ร้านทดสอบใหม่')]),
])
def test_semantic_draft_request_is_not_vetoed_by_keywords_or_punctuation(text,changes):
    h=pending();before=deepcopy(active(h));r=h.send(text,patch({0:changes}))
    assert r.data['tool_result']['ok'],r
    assert active(h)['entries'][1]==before['entries'][1]
    assert active(h)['version']>before['version'] and not h.sheets.entries


@pytest.mark.parametrize('text',[
    'ช่วยจด ร้านทดสอบโคเมต 47 บาท ได้ไหม?',
    'บันทึก "ร้านทดสอบโคเมต" 47 บาท',
])
def test_model_can_prepare_new_expense_from_question_or_quoted_label(text):
    h=harness();r=prepare(h,text,[entry(item='ร้านทดสอบโคเมต',amount=47)])
    assert r.status=='preview',r
    assert not h.sheets.entries


def test_changed_expense_fields_can_cite_original_owner_history_independently():
    h=pending();before=deepcopy(active(h))
    h.send('ชื่อที่อยากใช้คือ ร้านทดสอบใหม่',response(text='รับทราบ'))
    name_ref=h.service._current['source_ref']
    h.send('ยอดของรายการแรกคือ 75 บาท',response(text='รับทราบ'))
    amount_ref=h.service._current['source_ref']
    r=h.send('ใช้ชื่อกับยอดที่ผมบอกเมื่อกี้กับรายการแรก',historical({0:[
        ('item','ร้านทดสอบใหม่','ร้านทดสอบใหม่'),('amount',75,'75')]},
        {'item':name_ref,'amount':amount_ref}))
    assert r.data['tool_result']['ok'],r
    d=active(h);e=d['entries'][0]
    assert (e['item'],e['amount'],e['date'])==('ร้านทดสอบใหม่','75',before['entries'][0]['date'])
    assert e['fact_sources']['item']==[name_ref] and e['fact_sources']['amount']==[amount_ref]
    assert d['entries'][1]==before['entries'][1] and not h.sheets.entries
    assert h.service._current['source_ref'] in e['source_refs']


def test_expense_date_uses_original_owner_timestamp_after_midnight():
    h=harness();h.send('วันที่ที่หมายถึงคือ เมื่อวาน',response(text='รับทราบ'))
    ref=h.service._current['source_ref'];h.now+=timedelta(hours=10)
    prepare(h,'ร้านทดสอบโคเมต 47 บาท',[entry(item='ร้านทดสอบโคเมต',amount=47)])
    r=h.send('ใช้วันที่ที่บอกไว้ก่อนหน้านี้',historical({0:[('date_text','เมื่อวาน','เมื่อวาน')]},{'date_text':ref}))
    assert r.status=='preview',r
    e=active(h)['entries'][0]
    assert e['date']=='2026-10-15' and e['fact_sources']['date']==[ref] and not h.sheets.entries


def test_generic_table_can_complete_a_field_from_selected_owner_history():
    from test_table_workflow import harness as tables,prepare as table_prepare
    h=tables();h.send('เบอร์ติดต่อคือ 0000123',response(text='รับทราบ'))
    ref=h.app._current['source_ref']
    h.send('เพิ่มคุณจำลอง',table_prepare(name=('คุณจำลอง','คุณจำลอง')))
    r=h.send('ใช้เบอร์ที่ผมให้ไว้เมื่อกี้',historical({0:[('phone','0000123','0000123')]},{'phone':ref},domain='table'))
    assert r.status=='preview',r
    assert r.data['table_draft']['values']['phone']=='0000123'
    store=h.app.table_flow.registry.stores['people'];assert store.writes==0
    event=h.event('ยืนยัน');saved=h.app.handle(event)
    assert saved.status=='recorded' and store.writes==1
    assert h.app.handle(deepcopy(event))==saved and store.writes==1


@pytest.mark.parametrize('batch',[False,True])
def test_calendar_patch_keeps_original_date_anchor_and_other_members(batch):
    from test_calendar_mvp import Harness
    from test_calendar_batches_v13 import two,children
    h=Harness();h.send('วันที่ต้องการคือ พรุ่งนี้',response(text='รับทราบ'))
    ref=h.app._current['source_ref'];h.now+=timedelta(hours=10)
    if batch:
        two(h,complete=True);before=deepcopy(children(h));domain='calendar_batch'
    else:
        h.prepare('นัดทดสอบดาว วันที่ 20 ตุลา 09:00-10:00',title='ทดสอบดาว',date_text='วันที่ 20 ตุลา',time_text='09:00',end_time_text='10:00')
        before=[deepcopy(h.draft())];domain='calendar'
    r=h.send('แก้วันของนัดแรกเป็นวันที่ที่ผมบอกไว้',historical({0:[('date_text','พรุ่งนี้','พรุ่งนี้')]},{'date_text':ref},domain=domain))
    assert r.status=='preview',r
    after=children(h) if batch else [h.draft()]
    assert after[0]['start'].startswith('2026-10-17')
    assert after[0]['anchors']['date_text']['source_ref']==ref
    assert after[0]['facts']['time_text']==before[0]['facts']['time_text']
    if batch:assert after[1]['facts']==before[1]['facts']
    assert not h.provider.posts and not h.sheets.entries


@pytest.mark.parametrize('damage',['invented_amount','assistant','foreign','expired','cancelled'])
def test_history_reference_does_not_bypass_fact_provenance(damage):
    h=harness()
    if damage=='cancelled':
        prepare(h,'ร้านทดสอบโคเมต 75 บาท',[entry(item='ร้านทดสอบโคเมต',amount=75)])
        ref=h.service._current['source_ref']
        assert h.send('ยกเลิก '+active(h)['draft_ref']).status=='cancelled'
    else:
        h.send('ยอดที่ผมบอกคือ 75 บาท',response(text='ยอดคือ 999 บาท'))
        ref=h.service._current['source_ref']
    if damage=='expired':h.now+=timedelta(hours=25)
    prepare(h,'ร้านทดสอบโคเมต 47 บาท',[entry(item='ร้านทดสอบโคเมต',amount=47)])
    before=deepcopy(active(h));amount=999 if damage in {'invented_amount','assistant'} else 75
    if damage=='foreign':ref='foreign_owner_source'
    literal='999' if damage=='assistant' else '75'
    r=h.send('ใช้ยอดที่ผมบอกไว้',historical({0:[('amount',amount,literal)]},{'amount':ref}),response(text='ขอตรวจข้อมูล'))
    assert not r.data['tool_result'].get('ok'),r
    assert active(h)['entries']==before['entries'] and not h.sheets.entries
    assert h.send('ยืนยัน').status!='recorded' and not h.sheets.entries


@pytest.mark.parametrize('text,value,literal',[
    ('ยอดคือ 175 บาท',75,'75'),
    ('ยอดคือ 75.25 บาท',75,'75'),
    ('ยอดคือ 1750 บาท',175,'175'),
])
def test_cropping_a_historical_span_cannot_create_a_different_money_fact(text,value,literal):
    h=pending();before=deepcopy(active(h))
    h.send(text,response(text='รับทราบ'));ref=h.service._current['source_ref']
    r=h.send('ใช้ยอดที่ผมบอกเมื่อกี้',historical({0:[('amount',value,literal)]},{'amount':ref}),response(text='ต้องการยอดเท่าไหร่ครับ'))
    assert not r.data['tool_result'].get('ok') and active(h)['entries']==before['entries']
    assert not h.sheets.entries


@pytest.mark.parametrize('text,value',[('วันที่ 15 ตุลาคม 2026',15),('มี 3 รายการ',3)])
def test_numeric_roles_are_model_interpretations_and_do_not_authorize_a_write(text,value):
    # The mechanical check proves a complete owner-written number, not its
    # semantic role. A mistaken date/count classification must be caught by
    # planner evaluation or owner review, not another language parser.
    h=pending();h.send(text,response(text='รับทราบ'));ref=h.service._current['source_ref']
    r=h.send('ใช้ยอดที่ผมบอกเมื่อกี้',historical({0:[('amount',value,str(value))]},{'amount':ref}))
    assert r.data['tool_result']['ok'] and active(h)['entries'][0]['amount']==str(value)
    assert not h.sheets.entries


def test_calendar_can_use_selected_historical_all_day_mode_without_inventing_times():
    from test_calendar_mvp import Harness
    h=Harness();h.send('แบบที่ต้องการคือ ทั้งวัน',response(text='รับทราบ'));ref=h.app._current['source_ref']
    h.prepare('นัดทดสอบดาว พรุ่งนี้',title='ทดสอบดาว',date_text='พรุ่งนี้')
    r=h.send('ใช้นัดแบบที่ผมบอกไว้',historical({0:[('all_day',True,'ทั้งวัน')]},{'all_day':ref},domain='calendar'))
    assert r.status=='preview',r
    assert h.draft()['all_day'] and h.draft()['all_day_anchor']['source_ref']==ref
    assert not h.draft()['facts']['time_text'] and not h.provider.posts


def test_no_save_calendar_correction_prepares_a_fresh_review_without_creating():
    from test_calendar_mvp import Harness,complete
    h=Harness();old=complete(h);before=deepcopy(h.draft())
    r=h.send('ยังไม่ต้องสร้าง เปลี่ยนเวลาเริ่มเป็น 09:30 ได้ไหม?',
        patch({0:[('time_text','09:30','09:30')]},domain='calendar'))
    assert r.status=='preview',r
    assert h.draft()['facts']['time_text']=='09:30'
    assert h.draft()['facts']['title']==before['facts']['title']
    assert r.data['confirm_command']!=old.data['confirm_command']
    assert h.send(old.data['confirm_command']).status=='stale' and not h.provider.posts


def test_cross_calendar_expense_question_reaches_planner_and_executes_date_join():
    from test_calendar_semantic_queries import SemanticHosted
    from test_calendar_text_confirmation_v13 import turn
    from test_query_analysis_loop import read
    from test_expense_query_operations import seed,expense
    api=SemanticHosted()
    api.calendar.events={'exam':{'id':'exam','summary':'นัดคุมสอบ',
        'start':{'date':'2026-09-14'},'end':{'date':'2026-09-15'}}}
    seed(api,[('2026-09-14','กาแฟ',35),('2026-09-15','กาแฟ',99)])
    r,_=turn(api,'เดือนที่แล้ววันที่มีนัด ผมจ่ายไปกี่บาท',
        read('calendar','2026-09-01','2026-09-30',identifier='appointments'),
        expense(start=None,end=None,date_mode='previous',join=True,view='total'))
    assert r.data['tool_result']['expense']=='35.00',r
    assert len([b for b in api.model_bodies if b.get('tools')])==2
    assert not api.google.sheet_writes and not api.calendar.posts
