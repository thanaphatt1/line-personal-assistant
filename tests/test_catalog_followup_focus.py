"""Synthetic reproduction, not a replay of unavailable production tool calls."""
from copy import deepcopy
from datetime import timedelta

import pytest
from line_assistant.history_context import HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history
from test_catalog_names_v09 import harness,prepare,entry
from test_history_first import response


def pending():
    h=harness()
    r=prepare(h,'หอยทอด 50 บาท กับ หมูย่าง 50 บาท',
              [entry(item='หอยทอด',amount=50),entry(item='หมูย่าง',amount=50)])
    assert r.status=='clarification'
    assert r.data['tool_result']['catalog_entry_ref']==h.draft()['entries'][0]['entry_ref']
    return h


def enroll(h,text,**changes):
    d=h.draft()
    args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':None,
          'resolution_step':'begin_new_item','category':None,'subcategory':None,'existing_item':None,**changes}
    return h.send(text,response(name='prepare_item_enrollment',args=args))


@pytest.mark.parametrize('text',['ไม่แน่ใจว่าในรายการมั้ย ถ้ายังไม่มีก็เพิ่มใหม่','เพิ่มเป็นรายการใหม่'])
def test_missing_model_entry_ref_uses_delivered_question_and_keeps_entire_batch(text):
    h=pending();before=deepcopy(h.draft()['entries'])
    r=enroll(h,text)
    assert r.data['tool_result']['item_enrollment']['entry_ref']==before[0]['entry_ref']
    assert r.data['tool_result']['item_enrollment']['field']=='category'
    for original,current in zip(before,h.draft()['entries']):
        for key in ['item','amount','date','fact_sources','source_quote','amount_occurrence']:
            assert original.get(key)==current.get(key)
    assert 'enrollment' not in h.draft()['entries'][1]
    assert not h.sheets.entries and h.draft()['review'] is None


def test_explicit_other_item_wins_over_last_question_when_model_omits_ref():
    h=pending()
    r=enroll(h,'เพิ่มหมูย่างเป็นรายการใหม่')
    assert r.data['tool_result']['item_enrollment']['entry_ref']==h.draft()['entries'][1]['entry_ref']
    assert 'enrollment' not in h.draft()['entries'][0] and not h.sheets.entries


def test_repeating_begin_reads_current_category_question_without_reset_or_version_bump():
    h=pending();enroll(h,'เพิ่มเป็นรายการใหม่');before=deepcopy(h.draft())
    r=enroll(h,'เพิ่มเป็นรายการใหม่')
    assert r.data['tool_result']['item_enrollment']['field']=='category'
    assert h.draft()==before and not h.sheets.entries


def test_fresh_model_sibling_proposal_resumes_delivered_category_without_retargeting():
    h=pending();enroll(h,'เพิ่มเป็นรายการใหม่');before=deepcopy(h.draft())
    def choose_sibling(body):
        import json
        context=json.loads(body['input'][0]['content'])
        resolutions=context['available_drafts'][0]['item_resolutions']
        assert [r['entry_ref'] for r in resolutions if r['question_focus']]==[before['entries'][0]['entry_ref']]
        return response(name='prepare_item_enrollment',args={'draft_ref':before['draft_ref'],'draft_version':before['version'],
            'entry_ref':before['entries'][1]['entry_ref'],'category':None,'subcategory':None,'existing_item':None,'resolution_step':'begin_new_item'})
    result=h.send('เพิ่มเป็นรายการใหม่',choose_sibling)
    output=result.data['tool_result']
    assert output['name_question_resumed'] is True
    assert output['item_enrollment']['entry_ref']==before['entries'][0]['entry_ref']
    assert output['item_enrollment']['field']=='category' and h.draft()==before
    assert not output['confirmation_required'] and not h.sheets.entries


def test_two_unknown_items_complete_one_at_a_time_then_confirm_once():
    h=pending();enroll(h,'ถ้าไม่มีก็เพิ่มใหม่')
    first=h.draft()['entries'][0]['entry_ref']
    r=enroll(h,'หมวด Food',entry_ref=first,resolution_step='choose_category',category='Food')
    assert r.data['tool_result']['catalog_entry_ref']==h.draft()['entries'][1]['entry_ref']
    enroll(h,'เพิ่มใหม่ครับ')
    second=h.draft()['entries'][1]['entry_ref']
    r=enroll(h,'หมวด Food',entry_ref=second,resolution_step='choose_category',category='Food')
    assert r.status=='preview' and not h.sheets.entries
    event=h.event(r.data['confirm_command']);saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==2
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==2


@pytest.mark.parametrize('damage',['missing','expired','undelivered','changed_version','both_named'])
def test_ambiguous_or_unverified_focus_never_selects_a_sibling(damage):
    from line_assistant.state import Reply
    h=pending();h.service.production=True
    if damage=='missing':h.state.history=[r for r in h.state.history if r['role']!='assistant']
    elif damage=='expired':h.state.receipts['history-1']=type(h.state.receipts['history-1'])(h.state.receipts['history-1'].fingerprint,h.state.receipts['history-1'].result,h.now-timedelta(seconds=1))
    elif damage=='undelivered':h.state.replies['history-1']=Reply(status='failed',content_hash='synthetic',expires_at=h.now+timedelta(hours=1))
    elif damage=='changed_version':
        h.draft()['version']+=1;h.service._seal(h.draft())
    before=deepcopy(h.draft())
    r=enroll(h,'เพิ่มหอยทอดกับหมูย่างเป็นรายการใหม่' if damage=='both_named' else 'เพิ่มใหม่')
    assert r.status=='clarification' and 'ยังจับคู่หลักฐาน' not in r.message
    assert r.data['tool_result']['error']=='ambiguous_unknown_item'
    assert h.draft()==before and not h.sheets.entries


def test_focus_and_certified_facts_survive_a_cold_restart():
    h=pending();snapshot=history_snapshot(h.state)
    fresh=HistoryState();fresh.clock=lambda:h.now;fresh.enable_raw_history(journal=deepcopy(h.state.raw_journal))
    restore_history(fresh,snapshot,'mock-owner')
    restored=harness(state=fresh);restored.now=h.now;restored.counter=h.counter
    r=enroll(restored,'เพิ่มเป็นรายการใหม่')
    assert r.data['tool_result']['item_enrollment']['entry_ref']==restored.draft()['entries'][0]['entry_ref']
    assert [e['amount'] for e in restored.draft()['entries']]==['50','50'] and not restored.sheets.entries


def test_empty_matches_report_the_actual_lookup_instead_of_asking_owner_database_contents():
    h=pending();r=h.state.receipts['history-1'].result
    assert 'ยังไม่พบชื่อ' in r.message and 'หอยทอด' in r.message
    assert 'ตรงกับชื่อเดิมใน Database หรือ' not in r.message


@pytest.mark.parametrize('text',['สมมติว่าถ้าไม่มีก็เพิ่มใหม่','อย่าเพิ่ม ถ้าไม่มีก็เพิ่มใหม่',
                                 'ตัวอย่าง ถ้าไม่มีก็เพิ่มใหม่','"ถ้าไม่มีก็เพิ่มใหม่"','ถ้ามีก็เพิ่มใหม่',
                                 'ถ้าไม่มีให้บันทึกเงิน 500','ถ้าไม่มีก็เพิ่มใหม่?'])
def test_conditional_name_handling_does_not_weaken_financial_source_guards(text):
    h=pending();before=deepcopy(h.draft())
    r=enroll(h,text)
    assert r.status=='clarification' and h.draft()==before and not h.sheets.entries
