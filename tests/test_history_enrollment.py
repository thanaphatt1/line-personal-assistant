"""Natural owner phrases are model semantics; all providers/owners are synthetic."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json,socket
import pytest
from fastapi.testclient import TestClient
from line_assistant.taxonomy import identifier
from test_history_first import Harness as BaseHarness,response,data
from test_production_history import Hosted

class Harness(BaseHarness):
    def __init__(self):super().__init__(item_enrollment_enabled=True)

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('No live expense replay or paid provider call')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def enrollment(ref=None,entry_ref=None,category=None,subcategory=None,version=None):
    def call(body):
        c=json.loads(body['input'][0]['content']);d=next(x for x in c['available_drafts'] if ref is None or x['draft_ref']==ref)
        return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'] if version is None else version,
                        'entry_ref':entry_ref,'category':category,'subcategory':subcategory})
    return call

def unresolved(h,item='เครป'):
    r=h.prepare(f'{item} 60 วันที่ 2026-10-13',item=item,amount=60)
    assert r.status=='clarification' and not h.sheets.entries
    return h.draft()

@pytest.mark.parametrize('text',['เพิ่ม','เพิ่มชื่อนี้เลย','เอาเป็นรายการใหม่ครับ','สร้างชื่อเครปให้ด้วย','ไม่ใช่ชื่อเดิม เป็นชื่อใหม่'])
def test_model_semantic_enrollment_does_not_require_a_prescribed_phrase(text):
    h=Harness();d=unresolved(h);before=deepcopy(d['entries'][0])
    r=h.send(text,enrollment())
    assert r.status=='clarification' and 'หมวดไหน' in r.message
    assert 'YYYY' not in r.message and 'เพิ่มรายการใหม่' not in r.message
    e=h.draft()['entries'][0]
    assert all(e[k]==before[k] for k in ['item','amount','date','note']) and not h.sheets.entries
    r=h.send('อาหารก็ได้ครับ',enrollment(category=identifier('cat','Food')))
    assert r.status=='preview' and 'เพิ่มชื่อใหม่ใน Database: Food' in r.message and not h.sheets.entries
    event=h.event('ยืนยัน');calls=len(h.port.requests);saved=h.service.handle(event)
    assert saved.status=='recorded' and h.sheets.atomic_batches==1 and len(h.port.requests)==calls
    assert ('เครป','Food','') in h.sheets.rows and len(h.sheets.entries)==1
    assert h.service.handle(deepcopy(event))==saved and h.sheets.atomic_batches==1

def test_compact_date_correction_and_short_add_reuse_parent_style_with_only_mock_data():
    h=Harness();h.now=datetime(2026,10,14,0,18,tzinfo=timezone.utc)
    h.prepare('เครป 60 วันนี้',item='เครป',amount=60)
    r=h.update('เพิ่มรายการ เครป 13ตค 60 บาท',date='2026-10-13',date_text='13ตค')
    assert r.status=='clarification' and h.draft()['entries'][0]['date']=='2026-10-13'
    r=h.send('เพิ่ม',enrollment())
    assert r.status=='clarification' and 'หมวดไหน' in r.message
    assert (h.draft()['entries'][0]['item'],h.draft()['entries'][0]['amount'],h.draft()['entries'][0]['date'])==('เครป','60','2026-10-13')

def test_initial_natural_request_can_prepare_and_start_enrollment_in_one_turn():
    h=Harness()
    def prepare(body):
        ref=json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(item='เครป',amount=60,source_refs=[ref])]})
    r=h.send('ช่วยเพิ่มค่าเครปไปหน่อย 60 บาท',prepare,enrollment())
    assert r.status=='clarification' and 'เครป จะใช้หมวดไหน' in r.message
    assert h.draft()['entries'][0]['item']=='เครป' and not h.sheets.entries

def test_only_necessary_subcategory_is_asked_and_interruption_keeps_all_facts():
    h=Harness();h.sheets.rows.extend([('เค้ก','Food','Bakery'),('ไอศกรีม','Food','Dessert')]);unresolved(h)
    h.send('เพิ่มเลย',enrollment());before=deepcopy(h.draft()['entries'])
    h.send('ดาวฤกษ์คืออะไร',response(text='ดาวฤกษ์เปล่งแสงได้เอง'))
    assert h.draft()['entries']==before
    r=h.send('กลับเรื่องเครป ขออยู่หมวดอาหาร',enrollment(category=identifier('cat','Food')))
    assert r.status=='clarification' and 'หมวดย่อยไหน' in r.message
    r=h.send('ของหวานเหมาะสุด',enrollment(subcategory=identifier('sub','Food\0Dessert')))
    assert r.status=='preview' and 'Food / Dessert' in r.message and not h.sheets.entries

def test_amount_edit_keeps_enrollment_choices_but_invalidates_old_review():
    h=Harness();unresolved(h);h.send('เพิ่ม',enrollment())
    old=h.send('อาหาร',enrollment(category=identifier('cat','Food')))
    updated=h.update('แก้ยอดเครปเป็น 75 บาท',amount=75)
    assert updated.status=='preview' and h.draft()['entries'][0]['mapping']['category']=='Food'
    assert h.send(old.data['confirm_command']).status=='stale' and not h.sheets.entries
    assert h.send(updated.data['confirm_command']).status=='recorded' and h.sheets.entries[0].amount=='75'

@pytest.mark.parametrize('kwargs',[{'category':'cat_fake'},{'subcategory':'sub_fake'},{'version':0}])
def test_invalid_taxonomy_or_version_cannot_mutate_owned_draft(kwargs):
    h=Harness();unresolved(h);before=deepcopy(h.state.drafts)
    r=h.send('เพิ่ม',enrollment(**kwargs),response(text='ขอเลือกหมวดจากข้อมูลที่มี'))
    assert r.status=='clarification' and h.state.drafts==before and not h.sheets.entries

def test_multiple_unknown_names_need_a_target_then_focus_can_bind_category_answer():
    h=Harness();first=unresolved(h);h.prepare('ขนมทดลอง 80',item='ขนมทดลอง',amount=80)
    before=deepcopy(h.state.drafts)
    r=h.send('เพิ่ม',enrollment(ref=first['draft_ref']),response(text='หมายถึงชื่อไหนครับ'))
    assert r.status=='clarification' and h.state.drafts==before
    r=h.send('เพิ่มชื่อเครป',enrollment(ref=first['draft_ref']));assert 'หมวดไหน' in r.message
    r=h.send('อาหาร',enrollment(ref=first['draft_ref'],category=identifier('cat','Food')))
    assert r.status=='preview' and not h.sheets.entries

def test_possible_typo_is_enrolled_with_its_literal_name_and_never_silently_merged():
    h=Harness();unresolved(h,'กาเฟ');h.send('เป็นชื่อใหม่ เพิ่มได้เลย',enrollment())
    r=h.send('อาหาร',enrollment(category=identifier('cat','Food')))
    assert r.status=='preview' and 'กาเฟ 60' in r.message
    h.send(r.data['confirm_command']);assert ('กาเฟ','Food','') in h.sheets.rows
    assert h.sheets.entries[0].item=='กาเฟ'

def test_negated_enrollment_and_wrong_owner_do_not_change_draft():
    h=Harness();unresolved(h);before=deepcopy(h.state.drafts)
    r=h.send('ไม่ต้องเพิ่ม',enrollment(),response(text='พักรายการไว้'))
    assert r.status=='clarification' and h.state.drafts==before
    event=h.event('เพิ่ม');event['source']['userId']='someone-else';calls=len(h.port.requests)
    assert h.service.handle(event).status=='denied' and h.state.drafts==before and len(h.port.requests)==calls

def test_enrollment_expiry_and_wrong_category_child_cannot_change_draft():
    h=Harness();unresolved(h);h.send('เพิ่ม',enrollment())
    before=deepcopy(h.state.drafts)
    r=h.send('เลือกอาหาร',enrollment(category=identifier('cat','Food'),subcategory=identifier('sub','Work/Office\0')),response(text='ขอเลือกหมวดย่อยใหม่'))
    assert r.status=='clarification' and h.state.drafts==before and not h.sheets.entries
    ref=h.draft()['draft_ref'];version=h.draft()['version'];h.now+=timedelta(hours=24)
    stale=response(name='prepare_item_enrollment',args={'draft_ref':ref,'draft_version':version,'entry_ref':None,'category':None,'subcategory':None})
    r=h.send('เพิ่ม',stale,response(text='ร่างหมดอายุแล้ว'))
    assert r.status=='clarification' and not h.state.drafts and not h.sheets.entries

def test_enrollment_tool_is_advertised_only_when_relevant_and_keeps_request_bounds():
    h=Harness();h.send('สวัสดี',response(text='สวัสดีครับ'))
    assert 'prepare_item_enrollment' not in {t['name'] for t in h.port.requests[-1]['tools']}
    unresolved(h);h.send('เอาเป็นชื่อใหม่',enrollment())
    assert 'prepare_item_enrollment' in {t['name'] for t in h.port.requests[-1]['tools']}
    assert all(len(json.dumps(b,ensure_ascii=False,separators=(',',':')).encode())<=6144 for b in h.port.requests)

def test_category_already_supplied_naturally_does_not_require_a_choice_id_or_repeat_question():
    h=Harness();h.sheets.rows.append(('ไอศกรีม','Food','Dessert'))
    def prepare(body):
        ref=json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(item='เครป',amount=60,source_refs=[ref])]})
    r=h.send('ช่วยเพิ่มเครป 60 บาท อยู่หมวดอาหาร ของหวาน',prepare,enrollment(category='Food',subcategory='Dessert'))
    assert r.status=='preview' and 'Food / Dessert' in r.message and 'หมวดไหน' not in r.message
    assert not h.sheets.entries and len(h.port.requests)==2

def test_signed_factory_enrollment_recreation_plain_confirm_and_atomic_mock_write():
    api=Hosted()
    with TestClient(api.app()) as client:
        r=api.create(client,'เครป 60 วันที่ 2026-10-13',item='เครป',amount=60);assert r.status=='clarification'
        r,_=api.send_text(client,'เพิ่ม',enrollment());assert 'หมวดไหน' in r.message and not api.snapshot()['pending']
    with TestClient(api.app()) as client:
        r,_=api.send_text(client,'อาหาร',enrollment(category=identifier('cat','Food')))
        assert r.status=='preview' and api.google.sheet_writes==0
        calls=api.models;r,event=api.send_text(client,'ยืนยัน')
        assert r.status=='recorded' and api.models==calls and api.google.sheet_writes==1
    with TestClient(api.app()) as client:assert api.send(client,event).status_code==200
    assert api.google.sheet_writes==1 and any(k[0]=='Database' and v=='เครป' for k,v in api.google.cells.items())
