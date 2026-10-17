"""Synthetic, offline inline pairing through tools and signed production factory."""
from copy import deepcopy
import json,socket
import pytest
from fastapi.testclient import TestClient
from test_history_first import Harness,response,data
from test_production_history import Hosted

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('No real provider/event replay in inline tests')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def proposal(entries):
    def prepare(body):
        ref=json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[data(source_refs=[ref],**e) for e in entries]})
    return prepare

ENTRIES=[{'item':'ค่ากระดาษ','amount':228},{'item':'กาแฟ','amount':50}]

@pytest.mark.parametrize('text',['ค่ากระดาษ 228 กาแฟ 50','ค่ากระดาษ 228กาแฟ 50','ค่ากระดาษ 228 บาท และกาแฟ 50 บาท','ค่ากระดาษ 228\nกาแฟ 50'])
def test_individual_amount_grounding_preview_and_plain_confirmation_write_once(text):
    h=Harness();r=h.send(text,proposal(ENTRIES))
    assert r.status=='preview' and not h.sheets.entries
    assert [(e['item'],e['amount']) for e in r.data['tool_result']['entries']]==[('ค่ากระดาษ','228'),('กาแฟ','50')]
    event=h.event('ยืนยัน');calls=len(h.port.requests);saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==2 and len(h.port.requests)==calls
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==2

@pytest.mark.parametrize('text',['เมื่อวานค่ากระดาษ 228กาแฟ 50','วันที่ 10 ค่ากระดาษ 228 กาแฟ 50','2026-10-10 ค่ากระดาษ 228กาแฟ 50'])
def test_shared_prefix_date_is_grounded_for_both_pairs(text):
    h=Harness();r=h.send(text,proposal(ENTRIES))
    expected='2026-10-12' if 'เมื่อวาน' in text else '2026-10-10'
    assert r.status=='preview' and [e['date'] for e in r.data['tool_result']['entries']]==[expected,expected]

def test_pair_dates_before_the_amount_and_note_numbers_are_not_money():
    h=Harness();entries=[*ENTRIES];entries[1]={**entries[1],'note':'ซื้อให้เพื่อน 2 คน วันที่ 20'}
    r=h.send('ค่ากระดาษ วันที่ 10 228 กาแฟ วันที่ 11 50 หมายเหตุ ซื้อให้เพื่อน 2 คน วันที่ 20',proposal(entries))
    assert r.status=='preview'
    rows=r.data['tool_result']['entries']
    assert [(e['date'],e['note']) for e in rows]==[('2026-10-10',''),('2026-10-11','ซื้อให้เพื่อน 2 คน วันที่ 20')]

def test_model_entry_order_does_not_change_literal_item_amount_binding():
    h=Harness();r=h.send('ค่ากระดาษ 228กาแฟ 50',proposal(list(reversed(ENTRIES))))
    assert r.status=='preview' and [(e['item'],e['amount']) for e in r.data['tool_result']['entries']]==[('กาแฟ','50'),('ค่ากระดาษ','228')]

@pytest.mark.parametrize('text,entries',[
    ('ค่ากระดาษ 228กาแฟ 50',[{'item':'ค่ากระดาษ','amount':50},{'item':'กาแฟ','amount':228}]),
    ('ค่ากระดาษ 2 ชิ้น 228 กาแฟ 50',ENTRIES),
    ('ค่ากระดาษ 228กาแฟ 50 รวม 278',ENTRIES),
    ('ค่ากระดาษ กาแฟ 228 50',ENTRIES),
    ('ค่ากระดาษ 228 เมื่อวานกาแฟ 50',ENTRIES),
    ('ค่ากระดาษ 228กาแฟ 50 เมื่อวาน',ENTRIES),
    ('ร้านลับค่ากระดาษ 228กาแฟ 50',ENTRIES),
    ('ค่ากระดาษ 228กาแฟ 50',[{'item':'ค่ากระดาษ','amount':228}]),
    ('ค่ากระดาษ 228กาแฟ 50รถเมล์ 12',ENTRIES),
    ('ค่ากระดาษ 228กาแฟ -50',ENTRIES),
    ('ค่ากระดาษ 228กาแฟ 50.005',ENTRIES),
    ('ค่ากระดาษ 228 ค่ากระดาษ 250',[{'item':'ค่ากระดาษ','amount':228},{'item':'ค่ากระดาษ','amount':250}]),
    ('ค่ากระดาษ 228กาแฟ 50',[{'item':'ค่ากระดาษ','amount':228},{'item':'ค่ากระดาษ','amount':228}]),
])
def test_ambiguous_or_unmatched_pairs_cannot_make_a_review_or_write(text,entries):
    h=Harness();r=h.send(text,proposal(entries),response(text='กรุณาระบุชื่อ ยอด และวันที่ของแต่ละรายการให้ชัดเจน'))
    assert r.status=='clarification' and not h.state.drafts and not h.sheets.entries

@pytest.mark.parametrize('text',['ตัดผม 250 เครป 60','ตัดผม 250เครป 60'])
def test_exact_requested_shape_uses_synthetic_catalog_and_signed_production_factory(text):
    api=Hosted()
    for row,values in [(7,['ตัดผม','Personal care','Hair']),(8,['เครป','Food','Dessert'])]:
        for col,value in enumerate(values,1):api.google.cells[('Database',row,col)]=value
    entries=[{'item':'ตัดผม','amount':250},{'item':'เครป','amount':60}]
    with TestClient(api.app()) as client:
        r,_=api.send_text(client,text,proposal(entries))
        assert r.status=='preview' and api.google.sheet_writes==0
        assert 'ตัดผม 250' in r.message and 'เครป 60' in r.message
    with TestClient(api.app()) as client:
        calls=api.models;r,event=api.send_text(client,'ยืนยัน')
        assert r.status=='recorded' and len(r.data['refs'])==2 and api.models==calls and api.google.sheet_writes==1
    with TestClient(api.app()) as client:
        assert api.send(client,event).status_code==200 and api.google.sheet_writes==1
    assert [api.google.cells[('All Expenses',row,3)] for row in [2,3]]==[250,60]

def test_negative_control_unscoped_amount_acceptance_is_detected(monkeypatch):
    import line_assistant.history_first as module
    def check():
        h=Harness();r=h.send('ค่ากระดาษ 228กาแฟ 50',proposal([{'item':'ค่ากระดาษ','amount':50},{'item':'กาแฟ','amount':228}]),response(text='ขอข้อมูลให้ชัดเจน'))
        assert r.status=='clarification' and not h.state.drafts
    check()
    original=module.HistoryFirstAssistant._units
    def unsafe(self,sources,item,old):
        units=original(self,sources,item,old)
        # Mutation falsely assigns the other item's amount to this name.
        return [(s.replace('228','TEMP').replace('50','228').replace('TEMP','50'),at,ref) for s,at,ref in units]
    monkeypatch.setattr(module.HistoryFirstAssistant,'_units',unsafe)
    with pytest.raises(AssertionError):check()
