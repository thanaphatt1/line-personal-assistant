"""Explicit blank catalog choices, with synthetic records and no provider I/O."""
from copy import deepcopy
import json
import socket

import pytest

from line_assistant.tables import Column,TableError,normalize
from test_pending_records import patch
from test_weekday_enrollment_v11 import harness,active
from test_catalog_names_v09 import prepare,entry
from test_history_first import response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Synthetic subcategory regression only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def pending_category():
    h=harness()
    h.sheets.rows.extend([('บริการทดสอบสมมติ','Entertainment','Recurring'),
        ('อาหารทดสอบสมมติ','Food/Grocery','Dessert')])
    result=prepare(h,'บัตรกิจกรรมทดสอบไลรา 47 บาท',[entry(item='บัตรกิจกรรมทดสอบไลรา',amount=47)])
    assert result.status=='clarification'
    result=h.send('เพิ่มเข้า entertainment',patch({0:[('category','Entertainment','entertainment')]}))
    assert result.status=='clarification' and 'หมวดย่อย' in result.message
    return h


@pytest.mark.parametrize('word',['ไม่มี','ไม่มีหมวดย่อย','ไม่ต้องใส่หมวดย่อย','without a subcategory please'])
@pytest.mark.parametrize('value',['',None])
def test_offered_no_subcategory_choice_can_complete_and_confirm_exactly_once(word,value):
    h=pending_category();before=deepcopy(active(h)['entries'][0])
    def choose(body):
        context=json.loads(body['input'][0]['content']);record=context['pending_records'][0]['records'][0]
        field=next(f for f in record['fields'] if f['name']=='subcategory')
        assert '' in field['choices'] and 'Recurring' in field['choices'] and field['required'] is True
        return patch({0:[('subcategory',value,word)]})(body)
    result=h.send(word,choose)
    assert result.status=='preview',result
    after=active(h)['entries'][0]
    assert after['mapping']=={'item':before['item'],'category':'Entertainment','subcategory':''}
    for key in ['item','amount','date','note','fact_sources','source_quote']:
        assert after.get(key)==before.get(key)
    assert not h.sheets.entries and not h.sheets.atomic_batches
    event=h.event(result.data['confirm_command']);calls=len(h.port.requests)
    saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==1 and h.sheets.atomic_batches==1
    assert (before['item'],'Entertainment','') in h.sheets.rows
    assert len(h.port.requests)==calls
    assert h.service.handle(deepcopy(event))==saved and h.sheets.atomic_batches==1


def test_category_and_explicit_blank_subcategory_can_be_prepared_together():
    h=harness();h.sheets.rows.append(('บริการสมมติ','Entertainment','Recurring'))
    prepare(h,'บัตรกิจกรรมทดสอบไลรา 47 บาท',[entry(item='บัตรกิจกรรมทดสอบไลรา',amount=47)])
    result=h.send('entertainment ไม่มีหมวดย่อย',patch({0:[
        ('category','Entertainment','entertainment'),('subcategory','','ไม่มีหมวดย่อย')]}))
    assert result.status=='preview',result
    assert active(h)['entries'][0]['mapping']['subcategory']=='' and not h.sheets.entries


@pytest.mark.parametrize('value',['ไม่มี',' ','Invented'])
def test_missing_or_unoffered_subcategory_values_do_not_change_the_draft(value):
    h=pending_category();before=deepcopy(h.state.drafts)
    result=h.send('ไม่มีหมวดย่อย',patch({0:[('subcategory',value,'ไม่มีหมวดย่อย')]}),response(text='ขอเลือกใหม่ครับ'))
    assert result.status=='clarification' and result.data['tool_result']['error']=='patch_invalid_value',result
    assert h.state.drafts==before and not h.sheets.entries


def test_a_subcategory_from_another_category_is_still_rejected_atomically():
    h=pending_category();before=deepcopy(h.state.drafts)
    result=h.send('ใช้ Dessert',patch({0:[('subcategory','Dessert','Dessert')]}),response(text='ขอเลือกใหม่ครับ'))
    assert result.status=='clarification' and h.state.drafts==before and not h.sheets.entries
    assert result.data['tool_result']['error']=='subcategory_not_in_selected_category'
    assert 'หมวด' in result.message and 'ยังระบุสาเหตุที่เจาะจงกว่านี้ไม่ได้' not in result.message


@pytest.mark.parametrize('value',['',None])
def test_blank_subcategory_is_not_invented_when_the_chosen_category_has_no_blank_pair(value):
    h=pending_category()
    h.sheets.rows=[r for r in h.sheets.rows if not (r[1]=='Entertainment' and r[2]=='')]
    before=deepcopy(h.state.drafts)
    result=h.send('ไม่มีหมวดย่อย',patch({0:[('subcategory',value,'ไม่มีหมวดย่อย')]}),response(text='ต้องเลือกหมวดย่อยที่มีครับ'))
    assert result.status=='clarification' and h.state.drafts==before and not h.sheets.entries


def test_pending_choice_support_does_not_relax_generic_table_types():
    with pytest.raises(TableError):normalize(Column(name='text',label='ข้อความจำลอง',type='text'),'')
    with pytest.raises(ValueError):Column(name='choice',label='ตัวเลือกจำลอง',type='enum',choices=['Recurring',''])


@pytest.mark.parametrize('value',['',None])
def test_signed_cold_flow_can_resume_after_interruption_and_confirm_blank_mapping_once(value):
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from test_production_history import Hosted
    api=Hosted()
    api.google.cells={k:v for k,v in api.google.cells.items() if k[0]!='Database' or k[1]==1}
    for row,values in enumerate([('ชื่อหมวดทดสอบ','Entertainment',''),('บริการทดสอบ','Entertainment','Recurring')],2):
        for col,cell_value in enumerate(values,1):api.google.cells[('Database',row,col)]=cell_value
    def create(body):
        source=json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,
            'entries':[entry(item='บัตรกิจกรรมทดสอบไลรา',amount=47,source_refs=[source])]})
    def send(text,*answers):
        with TestClient(api.app(raw_history_enabled=True)) as client:return api.send_text(client,text,*answers)
    first,_=send('บัตรกิจกรรมทดสอบไลรา 47 บาท',create,response(text='synthetic fallback'))
    assert first.status=='clarification'
    category,_=send('เพิ่มเข้า entertainment',patch({0:[('category','Entertainment','entertainment')]}))
    assert category.status=='clarification' and 'หมวดย่อย' in category.message
    send('ข้อมูลยังอยู่ไหม',response(text='ยังรอเลือกหมวดย่อยครับ'))
    api.now+=timedelta(minutes=24)
    preview,_=send('ไม่มีหมวดย่อย',patch({0:[('subcategory',value,'ไม่มีหมวดย่อย')]}))
    assert preview.status=='preview' and not api.google.sheet_writes,preview
    assert preview.data['tool_result']['entries'][0]['mapping']['subcategory']==''
    calls=api.models
    saved,event=send(preview.data['confirm_command'])
    assert saved.status=='recorded' and api.models==calls and api.google.sheet_writes==1
    with TestClient(api.app(raw_history_enabled=True)) as client:
        assert api.send(client,deepcopy(event)).status_code==200
    assert api.models==calls and api.google.sheet_writes==1
