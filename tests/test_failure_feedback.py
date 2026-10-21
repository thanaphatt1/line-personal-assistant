"""Synthetic reproduction of a traced rejected enrollment, never owner state."""
from copy import deepcopy
import json
import pytest

from line_assistant import response_realization as voice
from line_assistant.action_trace import outcome
from line_assistant.history_context import HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.validation_explanations import message
from test_catalog_followup_focus import pending,enroll
from test_catalog_names_v09 import harness
from test_history_first import response
from test_response_realization import generated


def rejected(h,text='เพิ่มเป็นรายการใหม่',**changes):
    return enroll(h,text,entry_ref=h.draft()['entries'][0]['entry_ref'],category='Food',**changes)


@pytest.mark.parametrize('text',['เพิ่มเป็นรายการใหม่','ค่าหอยทอด 50 บาท ค่าหมูย่าง 50 บาท','ค่าหอยทอด 50 บาท'])
def test_rejected_inferred_category_explains_actual_failure_and_asks_only_missing_choice(text):
    h=pending();before=deepcopy(h.draft())
    r=rejected(h,text);output=r.data['tool_result'];feedback=output['validation_feedback']
    assert output['error']=='new_item_requires_category_clarification'
    assert feedback['kind']=='model_arguments' and feedback['next_action']=='ask_category'
    assert feedback['required_fields']==['entries[0].category']
    assert 'หมวด' in r.message and 'หอยทอด' in r.message and 'Food' in r.message
    assert 'หมูย่าง' not in r.message and '50' not in r.message
    assert 'ยังจับคู่หลักฐาน' not in r.message and 'ส่งประโยคเดิม' not in r.message and 'ยังอยู่' not in r.message
    assert [v['amount'] for v in feedback['known_entries']]==['50','50']
    assert feedback['target']['item']=='หอยทอด'
    assert h.draft()==before and not h.sheets.entries
    assert r.data['validation_receipt']['next_action']=='ask_category'
    assert 'confirm_command' not in r.data


def test_category_answer_after_failure_survives_restart_and_resolves_only_the_asked_row():
    h=pending();rejected(h);original=deepcopy(h.draft()['entries'])
    fresh=HistoryState();fresh.clock=lambda:h.now;fresh.enable_raw_history(journal=deepcopy(h.state.raw_journal))
    restore_history(fresh,history_snapshot(h.state),'mock-owner')
    restored=harness(state=fresh);restored.now=h.now;restored.counter=h.counter
    r=enroll(restored,'หมวด Food',resolution_step='choose_category',category='Food')
    assert restored.draft()['entries'][0]['mapping']['category']=='Food'
    assert 'enrollment' not in restored.draft()['entries'][1]
    for e,old in zip(restored.draft()['entries'],original):
        for field in ['item','amount','date','fact_sources','amount_occurrence','source_quote']:assert e.get(field)==old.get(field)
    assert r.status=='clarification' and not restored.sheets.entries


def test_repeated_reported_messages_keep_the_same_question_then_allow_full_review_and_single_save():
    h=pending();original=deepcopy(h.draft()['entries'])
    for text in ['เพิ่มเป็นรายการใหม่','ค่าหอยทอด 50 บาท ค่าหมูย่าง 50 บาท','ค่าหอยทอด 50 บาท']:
        r=rejected(h,text)
        assert r.data['tool_result']['validation_feedback']['next_action']=='ask_category'
        assert h.draft()['version']==1 and h.draft()['entries']==original
    enroll(h,'หมวด Food',resolution_step='choose_category',category='Food')
    enroll(h,'เพิ่มหมูย่างเป็นรายการใหม่',entry_ref=h.draft()['entries'][1]['entry_ref'])
    r=enroll(h,'หมวด Food',entry_ref=h.draft()['entries'][1]['entry_ref'],resolution_step='choose_category',category='Food')
    assert r.status=='preview' and not h.sheets.entries
    event=h.event(r.data['confirm_command']);saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==2
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==2


def test_reason_question_uses_the_delivered_diagnosis_instead_of_inventing_a_missing_date():
    h=pending();r=rejected(h);before=len(h.port.requests)
    why=h.send('ผิดตรงไหน',response(text='เพราะไม่ได้ระบุยอด'))
    assert why.data['explains_server_receipt'] and len(h.port.requests)==before
    assert r.message in why.message and 'ยังยืนยันจากคำตอบ' in why.message
    assert not h.sheets.entries


def test_unknown_failure_does_not_invent_evidence_mismatch_or_an_input_correction():
    text=message({'ok':False,'error':'unexpected_internal_condition'})
    assert 'ยังบอกไม่ได้ว่าคุณต้องแก้ข้อมูลส่วนไหน' in text
    assert 'จับคู่หลักฐาน' not in text


def test_schema_error_retains_typed_fields_without_echoing_raw_input_or_exception_prose():
    h=pending();d=h.draft()
    args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'category':{'private_value':'never-echo-this'},'entry_ref':None}
    r=h.send('เพิ่มเป็นรายการใหม่',response(name='prepare_item_enrollment',args=args))
    out=r.data['tool_result'];assert out['error']=='invalid_tool_arguments'
    assert out['invalid_fields']==['category']
    assert out['validation_feedback']['codes']==['invalid_tool_arguments']
    assert out['validation_feedback']['kind']=='model_arguments'
    assert 'never-echo-this' not in json.dumps(out) and 'validation error' not in r.message
    assert not h.sheets.entries


@pytest.mark.parametrize('error',[TypeError('internal-private-location'),KeyError('private-coordinate'),ValueError('private exception prose')])
def test_internal_exception_does_not_claim_that_owner_or_model_arguments_are_wrong(monkeypatch,error):
    h=pending()
    def fail(args):raise error
    monkeypatch.setattr(h.service,'_enroll_item',fail)
    r=enroll(h,'เพิ่มเป็นรายการใหม่');out=r.data['tool_result']
    assert out['error']=='tool_failed' and out['validation_feedback']['kind']=='unclassified'
    assert 'ยังบอกไม่ได้ว่าคุณต้องแก้ข้อมูลส่วนไหน' in r.message
    assert 'private' not in json.dumps(out) and 'ผิดรูปแบบ' not in r.message
    assert not h.sheets.entries


def body():
    h=pending();r=rejected(h);return h,r,voice.body_for(h.service,r)


@pytest.mark.parametrize('wording',[
    'ผมข้ามขั้นตอนไปเสนอหมวดก่อนที่คุณจะเลือก เลยยังใช้หมวดนั้นไม่ได้ครับ',
    'ผมยังยืนยันหมวดของหอยทอดจากคำตอบไม่ได้ จึงยังไม่ใช้หมวดที่เสนอไปครับ',
])
def test_reply_model_sees_diagnosis_and_can_paraphrase_it_with_locked_status_facts_and_question(wording):
    h,r,b=body();assert voice.validate_body(b)
    p=json.loads(b['input'][0]['content'])
    assert p['validation_reply'] and p['validation_feedback']['codes']==['new_item_requires_category_clarification']
    assert p['validation_feedback']['required_fields']==['entries[0].category']
    assert [e['item'] for e in p['validation_feedback']['known_entries']]==['หอยทอด','หมูย่าง']
    value=generated(b)
    doc=json.loads(value['output'][0]['content'][0]['text']);doc['explanation']=wording
    value['output'][0]['content'][0]['text']=json.dumps(doc,ensure_ascii=False)
    text=voice.parse(value,b)
    assert wording in text and p['facts']['reason'] not in text
    for key in ['status','ask']:assert text.count(p['facts'][key])==1
    assert 'ยังอยู่' not in text and 'ไม่ต้องส่ง' not in text and 'หมูย่าง' not in text
    assert h.draft()['version']==1 and not h.sheets.entries


@pytest.mark.parametrize('change',[
    {'reason_codes':['tool_unavailable']},
    {'explanation':'บันทึกแล้ว เลือกหมวดแล้วครับ'},
    {'explanation':'คุณพิมพ์หมวดผิดครับ'},
    {'explanation':'ไม่มีสิทธิ์เข้าถึงหมวดในฐานข้อมูลครับ'},
    {'explanation':'หมวดนี้ยอดเงินต้องเป็น 100 บาทครับ'},
    {'explanation':'ส่งชื่อหมวดมาใหม่ครับ'},
    {'explanation':'ผมเลือกหมวด Food ให้ครับ'},
    {'explanation':'เลือกหมวดไหนครับ?'},
    {'explanation':'ผมจะซ่อมระบบให้ครับ'},
])
def test_unsupported_diagnostic_prose_is_rejected_without_changing_facts_or_status(change):
    h,r,b=body();value=generated(b);doc=json.loads(value['output'][0]['content'][0]['text']);doc.update(change)
    value['output'][0]['content'][0]['text']=json.dumps(doc,ensure_ascii=False)
    with pytest.raises(ValueError):voice.parse(value,b)
    assert h.draft()['version']==1 and not h.sheets.entries


def test_failure_trace_keeps_classification_and_next_step_but_no_names_amounts_or_choices():
    _,r,_=body();logged=outcome(r.data['tool_result'])
    assert logged['failure_kind']=='model_arguments' and logged['next_action']=='ask_category'
    serialized=json.dumps(logged,ensure_ascii=False)
    assert all(label not in serialized for label in ['หอยทอด','หมูย่าง','Food','choices','50 บาท'])


def test_hosted_diagnostic_generation_failure_returns_actionable_fallback_and_replays_without_a_model_call():
    from fastapi.testclient import TestClient
    from test_response_realization import VoiceHosted
    from test_calendar_text_confirmation_v13 import turn
    api=VoiceHosted()
    def prepare(b):
        c=json.loads(b['input'][0]['content'])
        props=next(t for t in b['tools'] if t['name']=='prepare_expenses')['parameters']['properties']['entries']['items']['properties']
        rows=[]
        for name in ['หอยทอด','หมูย่าง']:
            row={k:None for k in props};row.update(item=name,amount=50,source_refs=[c['current_source_ref']]);rows.append(row)
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':rows})
    turn(api,'หอยทอด 50 บาท กับ หมูย่าง 50 บาท',prepare)
    def invalid_choice(b):
        d=json.loads(b['input'][0]['content'])['available_drafts'][0]
        return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],
            'entry_ref':d['entries'][0]['entry_ref'],'resolution_step':'begin_new_item',
            'category':'Food/Drink','subcategory':None,'existing_item':None})
    def bad_prose(b):
        p=json.loads(b['input'][0]['content']);assert p['validation_reply']
        value=generated(b);value['output'][0]['content'][0]['text']=json.dumps({
            'reason_codes':p['validation_feedback']['codes'],'explanation':'ไม่มีสิทธิ์เลือกหมวดในฐานข้อมูลครับ'},ensure_ascii=False)
        return value
    api.voice=bad_prose;r,event=turn(api,'เพิ่มเป็นรายการใหม่',invalid_choice)
    assert r.data['response_realization']['reason']=='invalid_output'
    assert 'หมวด' in r.message and 'หอยทอด' in r.message and 'หมูย่าง' not in r.message
    assert 'ยังอยู่' not in r.message and 'ไม่ต้องส่ง' not in r.message
    assert 'สิทธิ์' not in r.message and not api.google.sheet_writes and not api.calendar.posts
    calls=len(api.model_bodies)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert len(api.model_bodies)==calls and not api.google.sheet_writes
