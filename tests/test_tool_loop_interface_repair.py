"""Local regressions from saved failures; sockets denied, no new model evidence."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json
import socket
import pytest
from line_assistant.tool_loop import parse, LoopBlocked
from line_assistant.domain_tools import Patch
from line_assistant.persistence import SQLiteState
from line_assistant.taxonomy import identifier
from test_domain_tool_loop import Harness, Port, create, update, response, PREVIEW

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'วันที่ 4 TEST รถเมล์ 35 บาท หมายเหตุ ไปส่งพัสดุหน้าตลาด'

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k): raise AssertionError('No network allowed for interface repair')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)

class DynamicPort(Port):
    def request(self, method, path, *, body):
        if callable(self.scripts[0]):
            value = self.scripts.pop(0)(deepcopy(body))
            self.scripts.insert(0, value)
        return super().request(method, path, body=body)

def harness(**kwargs):
    h = Harness(**kwargs); h.port = DynamicPort(); h.service.loop.port = h.port
    return h

def proposal(body, *, mode='update', **patch):
    ctx = json.loads(body['input'][0]['content'])['context']
    if 'entry_id' not in patch:
        patch['entry_id'] = None if mode == 'create' else ctx['entry_ids'][0]
    return response('draft_edit', {'mode': mode, 'entries': [patch]})


@pytest.mark.parametrize('count', [0, 2])
def test_mixed_output_still_requires_one_serial_function(count):
    value = response('preview_draft', {})
    value['output'] = [{'type':'message','status':'completed','content':[]}] + value['output'] * count
    with pytest.raises(LoopBlocked): parse(value)

@pytest.mark.parametrize('kind', ['message_incomplete', 'unknown_output', 'unknown_tool'])
def test_mixed_output_does_not_relax_terminal_or_allowlist_checks(kind):
    value = response('preview_draft', {})
    if kind == 'message_incomplete': value['output'].append({'type':'message','status':'in_progress'})
    elif kind == 'unknown_output': value['output'].append({'type':'computer_call','status':'completed'})
    else: value['output'][0]['name'] = 'commit_expense'
    with pytest.raises(LoopBlocked): parse(value)

@pytest.mark.parametrize('old', [0, 1, '1'])
def test_legacy_numeric_target_is_never_coerced(old):
    h = harness()
    result = h.send('TEST รถเมล์ 35 บาท', response('draft_edit', {'mode':'create','entries':[{'index':old,'item':'TEST รถเมล์','amount':'35'}]}), PREVIEW)
    assert h.service.loop.trace[0]['outcome']['error']['code'] == 'invalid_arguments'
    assert result.status == 'no_pending' and h.state.pending.kind == 'tool_source' and not h.sheets.entries

@pytest.mark.parametrize('target', [None, 'entry_unknown', '1'])
def test_unknown_entry_ids_reject_and_invalidate_previous_confirmation(target):
    h = harness(); old = h.draft(); before = deepcopy(h.state.pending.payload['entries'])
    result = h.send('แก้ยอดเป็น 50 บาท', lambda b: proposal(b, entry_id=target, amount='50'), PREVIEW)
    assert h.service.loop.trace[0]['outcome']['error']['code'] == 'invalid_target'
    assert h.state.pending.payload['entries'] == before
    assert h.click(old, 'บันทึก').status == 'stale' and not h.sheets.entries


def test_runtime_returns_stable_ids_and_finishes_edit_without_paid_preview():
    h = harness(); result = h.draft()
    ctx = h.service.loop_context(h.sheets.taxonomy()); ids = ctx['entry_ids']
    assert h.service.loop.trace[0]['outcome']['entry_ids'] == ids
    assert len(h.port.requests) == 1 and result.status == 'preview'
    result = h.send('แก้ยอดเป็น 50 บาท', lambda b: proposal(b, amount='50'))
    assert result.status == 'preview' and h.service.loop_context(h.sheets.taxonomy())['entry_ids'] == ids
    assert len(h.port.requests) == 2 and not h.sheets.entries
    h.send('ยกเลิก'); h.draft()
    assert h.service.loop_context(h.sheets.taxonomy())['entry_ids'] != ids


def selected(body, *, item_id=None, result_id=None):
    feedback = json.loads(body['input'][2]['output'])
    row = feedback['matches'][0]
    return proposal(body, mode='create', result_set_id=result_id or feedback['result_set_id'], item_id=item_id or row['id'], amount='35')


def test_one_search_then_valid_id_creates_review_in_two_calls():
    h = harness()
    result = h.send('TEST รถเมล์ 35 บาท', ('catalog_search',{'query':'รถเมล์','offset':0}), selected)
    assert result.status == 'preview' and len(h.port.requests) == 2 and not h.sheets.entries
    assert h.state.pending.payload['entries'][0]['item'] == 'TEST รถเมล์'

@pytest.mark.parametrize('fault', ['unknown_id', 'wrong_set', 'old_page', 'changed_mapping'])
def test_search_selection_is_bound_to_current_returned_set_and_live_snapshot(fault):
    h = harness()
    def choose(body):
        if fault == 'changed_mapping':
            h.sheets.rows[0] = ('TEST รถเมล์','Work/Education','')
        return selected(body, item_id='item_unknown' if fault=='unknown_id' else None,
                        result_id='result_unknown' if fault=='wrong_set' else None)
    calls = [('catalog_search',{'query':'รถเมล์','offset':0})]
    if fault == 'old_page':
        calls.append(('catalog_search',{'query':'กาแฟ','offset':0}))
        def choose(body): return selected(body, item_id=identifier('item','TEST รถเมล์'))
    calls += [choose]
    if fault != 'old_page': calls.append(PREVIEW)
    result = h.send('TEST รถเมล์ 35 บาท', *calls)
    codes = [v['outcome'].get('error',{}).get('code') for v in h.service.loop.trace]
    assert ('search_results_changed' if fault=='changed_mapping' else 'unknown_result_set' if fault=='wrong_set' else 'unknown_result_id') in codes
    assert h.state.pending.kind == 'tool_source' and not h.sheets.entries


def test_returned_category_id_is_not_owner_consent():
    h = harness(); h.draft('TEST รายการใหม่ 35 บาท', item='TEST รายการใหม่', amount='35')
    before = deepcopy(h.state.pending.payload['entries'])
    def choose(body):
        data = json.loads(body['input'][2]['output'])
        return proposal(body, category_id=data['matches'][0]['id'], result_set_id=data['result_set_id'])
    h.send('อืม', ('category_search',{'query':'Work/Office','offset':0}), choose, PREVIEW)
    assert h.service.loop.trace[1]['outcome']['error']['code'] == 'category_not_owner_choice'
    assert h.state.pending.payload['entries'] == before and not h.sheets.entries


def test_failed_create_recovery_grounds_original_facts_and_current_month():
    h = harness(); result = h.send(SOURCE, RuntimeError('offline format failure'))
    retained = deepcopy(h.state.pending)
    assert result.status == 'model_unavailable' and retained.kind == 'tool_source'
    def recover(body):
        ctx = json.loads(body['input'][0]['content'])['context']
        assert ctx['source_context']['text'] == SOURCE and ctx['pending'] is None
        return proposal(body, mode='create', item='TEST รถเมล์', amount='35', day=4, month=9, date_source='เดือนก่อน')
    result = h.send('เดือนก่อนจ้า', recover)
    e = h.state.pending.payload['entries'][0]
    assert result.status == 'preview' and e['date'] == '2026-09-04' and e['amount'] == '35'
    assert e['note'] == 'ไปส่งพัสดุหน้าตลาด' and h.state.pending.expires_at == retained.expires_at
    assert h.state.pending.origin_event_id == retained.origin_event_id and not h.sheets.entries


def test_source_recovery_survives_cold_start_without_extending_ttl(tmp_path):
    db = tmp_path/'recovery.sqlite'; h = harness(state=SQLiteState(db,'mock-owner'))
    event = h.event(SOURCE); h.port.scripts = [RuntimeError('offline')]
    result = h.service.handle(event); expiry = h.state.pending.expires_at
    assert h.service.handle(deepcopy(event)) == result and len(h.port.requests) == 1
    h2 = harness(state=SQLiteState(db,'mock-owner'), sheets=h.sheets); h2.now=h.now; h2.counter=h.counter
    result = h2.send('เดือนก่อนจ้า', lambda b: proposal(b, mode='create',item='TEST รถเมล์',amount='35',month=9,date_source='เดือนก่อน'))
    assert result.status == 'preview' and h2.state.pending.expires_at == expiry and not h.sheets.entries

@pytest.mark.parametrize('control', ['cancel', 'expire'])
def test_failed_source_cannot_be_resurrected_after_cancel_or_expiry(control):
    h = harness(); h.send(SOURCE, RuntimeError('offline'))
    if control == 'cancel': h.send('ยกเลิก')
    else: h.now += timedelta(seconds=h.service.settings.pending_ttl_seconds+1)
    result = h.send('เดือนก่อนจ้า', lambda b: proposal(b,mode='create',item='TEST รถเมล์',amount='35',month=9,date_source='เดือนก่อน'), PREVIEW)
    assert result.status == 'no_pending' and h.state.pending is None and not h.sheets.entries
    assert h.service.loop.trace[0]['outcome']['error']['code'] == 'unbacked_item'

@pytest.mark.parametrize('text', ['บันทึก','แก้วันที่เป็น 6 กันยายน','ล้างหมายเหตุเดิม'])
def test_no_draft_controls_are_clear_and_make_no_calls(text):
    h = harness(); result = h.send(text)
    assert result.status == 'no_pending' and not h.port.requests and not h.sheets.entries
    assert 'ที่บันทึกแล้ว' not in result.message and h.service.loop.trace == []


def test_recent_history_is_not_a_recovery_source():
    h = harness(); h.draft(SOURCE,amount='35',note='ไปส่งพัสดุหน้าตลาด',day=4,date_source='วันที่ 4'); h.send('ยกเลิก')
    assert h.state.recent_turns
    result = h.send('กันยายน', lambda b: proposal(b, mode='create',item='TEST รถเมล์',amount='35',month=9,date_source='กันยายน'), PREVIEW)
    assert result.status == 'no_pending' and h.state.pending is None and not h.sheets.entries


def test_source_only_is_not_a_confirmable_draft_and_rejects_foreign_owner():
    h = harness(); event = h.event(SOURCE); event['source']['userId']='foreign-owner'
    assert h.service.handle(event).status == 'denied' and h.state.pending is None and not h.port.requests
    h.send(SOURCE, RuntimeError('offline')); retained = deepcopy(h.state.pending)
    assert h.send('บันทึก').status == 'no_pending' and not h.sheets.entries
    assert h.state.pending.payload == retained.payload and h.state.pending.expires_at == retained.expires_at


def test_recovery_does_not_accept_changed_amount_without_owner_source():
    h = harness(); h.send(SOURCE, RuntimeError('offline'))
    h.send('เดือนก่อนจ้า', lambda b: proposal(b,mode='create',item='TEST รถเมล์',amount='999',month=9,date_source='เดือนก่อน'), PREVIEW)
    assert h.service.loop.trace[0]['outcome']['error']['code'] == 'unbacked_amount'
    assert h.state.pending.kind == 'tool_source' and not h.sheets.entries


def test_currency_suffix_is_a_structured_argument_error():
    h = harness(); h.send('TEST รถเมล์ 35 บาท', lambda b: proposal(b,mode='create',item='TEST รถเมล์',amount='35 บาท'), PREVIEW)
    assert h.service.loop.trace[0]['outcome']['error']['code'] == 'invalid_arguments'
    assert h.state.pending.kind == 'tool_source' and not h.sheets.entries




def test_recovered_draft_needs_fresh_owner_review_and_commit_replay_is_once():
    h = harness(); event = h.event(SOURCE); h.port.scripts=[RuntimeError('offline')]
    h.service.handle(event)
    review = h.send('เดือนก่อนจ้า',lambda b: proposal(b,mode='create',item='TEST รถเมล์',amount='35',month=9,date_source='เดือนก่อน'))
    assert review.status=='preview' and not h.sheets.entries
    done=h.click(review,'บันทึก'); assert done.status=='recorded' and len(h.sheets.entries)==1
    calls=len(h.port.requests)
    assert h.click(review,'บันทึก').status=='stale'
    h.service.handle(deepcopy(event))
    assert len(h.sheets.entries)==1 and len(h.port.requests)==calls and h.state.pending is None


def test_current_turn_search_bindings_are_retired_at_receipt():
    h = harness(); h.send('TEST รถเมล์ 35 บาท',('catalog_search',{'query':'รถเมล์','offset':0}),selected)
    assert h.service._search_sets=={}
