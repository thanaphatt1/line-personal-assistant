"""Provider schemas retain strictness and each field's own meaning."""
import json

import pytest
from line_assistant.draft_workflow import definitions
from line_assistant.history_first import tools

def strict_objects(schema):
    kind=schema.get('type')
    if kind=='object' or isinstance(kind,list) and 'object' in kind:
        assert schema.get('additionalProperties') is False
        assert set(schema.get('required',[]))==set(schema.get('properties',{}))
        for field in schema.get('properties',{}).values():
            strict_objects(field)
    if 'items' in schema:
        strict_objects(schema['items'])
    for variant in schema.get('anyOf',[]):
        strict_objects(variant)

@pytest.mark.parametrize('enrollment',[False,True])
@pytest.mark.parametrize('semantic',[False,True])
@pytest.mark.parametrize('controls',[False,True])
def test_advertised_strict_tool_objects_require_all_declared_fields(enrollment,semantic,controls):
    for tool in tools(enrollment=enrollment,semantic=semantic,controls=controls):
        assert tool['strict'] is True
        strict_objects(tool['parameters'])


@pytest.mark.parametrize('enrollment',[False,True])
@pytest.mark.parametrize('semantic',[False,True])
@pytest.mark.parametrize('controls',[False,True])
def test_note_guidance_cannot_change_other_fields_or_tools(enrollment,semantic,controls):
    advertised=tools(enrollment=enrollment,semantic=semantic,controls=controls)
    original=json.loads(json.dumps(advertised))
    expense=next(t for t in advertised if t['name']=='prepare_expenses')
    expense['parameters']['properties']['entries']['items']['properties']['note'].update(
        description='Only the explicit owner note belongs in this field.')
    expected=next(t for t in original if t['name']=='prepare_expenses')
    expected['parameters']['properties']['entries']['items']['properties']['note'].update(
        description='Only the explicit owner note belongs in this field.')
    assert advertised==original


@pytest.mark.parametrize('enrollment',[False,True])
def test_final_workflow_schema_preserves_item_and_reference_meanings(enrollment):
    advertised=definitions(tools(enrollment=enrollment,semantic=True,controls=True),[])
    # Inspect the serialized provider payload, after every workflow decoration.
    serialized=json.loads(json.dumps(advertised))
    expense=next(t for t in serialized if t['name']=='prepare_expenses')
    properties=expense['parameters']['properties']
    entry=properties['entries']['items']['properties']
    assert 'Full original owner item label' in entry['item']['description']
    assert entry['item']['description']!=entry['note']['description']
    for reference in [properties['draft_ref'],entry['entry_ref']]:
        assert 'note' not in reference.get('description','').lower()
    for tool in serialized:
        strict_objects(tool['parameters'])


@pytest.mark.parametrize('pending',[False,True])
def test_actual_planner_request_keeps_item_writable_when_creation_refs_are_restricted(pending):
    from test_raw_history import Harness,context
    from test_history_first import response

    h=Harness()
    if pending:
        h.prepare('ของใช้ทดลอง 29 บาท',item='ของใช้ทดลอง',amount=29)
    h.send('ช่วยบอกขอบเขตงาน',response(text='ช่วยบันทึกและค้นข้อมูลครับ'))
    body=h.port.requests[-1]
    assert bool(context(body)['pending_records']) is pending
    expense=next(t for t in body['tools'] if t['name']=='prepare_expenses')
    properties=expense['parameters']['properties']
    fields=properties['entries']['items']['properties']
    assert 'Full original owner item label' in fields['item']['description']
    assert 'Null unless' in fields['note']['description']
    assert 'enum' not in fields['item']
    assert 'enum' not in fields['note']
    if pending:
        assert properties['draft_ref']['enum']==[None]
        assert 'enum' not in fields['entry_ref']
