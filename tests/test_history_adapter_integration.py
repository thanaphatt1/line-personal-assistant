"""Actual adapter code with isolated HTTP protocol fakes, not live services."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json, os, socket, subprocess, sys, tempfile, threading

import httpx
import pytest
from fastapi.testclient import TestClient

from line_assistant.config import Settings
from line_assistant.firestore_state import FirestoreState, LeaseBusy, LeaseLost
from line_assistant.history_context import source_ref
from line_assistant.history_integration import FirestoreHistoryState, history_adapter_assistant
from line_assistant.history_persistence import history_snapshot
from line_assistant.main import create_app
from line_assistant.mock_api import MockApiServices
from line_assistant.service import ProviderFailure
from line_assistant.snapshot_codec import decode
from line_assistant.transport import OutcomeUnknown, RestClient
from history_adapter_worker import ApiScriptPort, mock_target, runtime
from persistent_api_mock import PersistentApiMock
from test_history_first import Port, data, response

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k): raise AssertionError('No network in adapter integration tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def event(text, key, n=1, at=NOW, owner='mock-owner'):
    return {'webhookEventId': key, 'timestamp': int(at.timestamp()*1000)+n,
            'source': {'type': 'user', 'userId': owner}, 'type': 'message',
            'message': {'id': key, 'type': 'text', 'text': text}}


def setup(providers=None, now=NOW, **kwargs):
    p = providers or MockApiServices(id_column='E')
    return runtime(p, lambda: now, **kwargs), p


def prepare(p, text='BTS 228 บาท', at=NOW):
    a, _ = setup(p, at); r = a.handle(event(text, 'create', at=at))
    assert r.status == 'preview', r
    return r


def persisted(p, owner='mock-owner'):
    a, _ = setup(p, owner=owner)
    with a.state.session(): return history_snapshot(a.state)


def worker(directory, ev, phase='none', now=NOW):
    with tempfile.NamedTemporaryFile(mode='w', dir=directory.parent, suffix='.json', delete=False) as f:
        json.dump({'event': ev, 'now': now.isoformat()}, f); path = Path(f.name)
    try:
        return subprocess.run([sys.executable, str(ROOT/'tests/history_adapter_worker.py'),
                               str(directory), str(path), phase], text=True, capture_output=True, timeout=30,
                              env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(ROOT/'src'), str(ROOT/'tests')))})
    finally: path.unlink()


def result(process, status):
    assert process.returncode == 0, process.stderr
    value = json.loads(process.stdout); assert value['status'] == status, value
    return value


def test_default_off_and_plain_firestore_schema_unchanged():
    p = MockApiServices(id_column='E')
    with pytest.raises(ValueError, match='defaults off'):
        history_adapter_assistant(firestore_client=RestClient('firestore', p.transport()),
                                 sheets_client=RestClient('sheets', p.transport()), target=mock_target(), port=ApiScriptPort())
    assert not p.calls
    legacy = FirestoreState(RestClient('firestore', p.transport()), 'mock-owner', project_id='synthetic-project')
    with legacy.session(): legacy.checkpoint()
    snapshot = decode(next(iter(p.documents.values()))['fields']['snapshot']['stringValue'], 800_000)
    assert snapshot['version'] == 1 and 'history' not in snapshot and 'core' not in snapshot


@pytest.mark.parametrize('bad', ['firestore_transport', 'sheets_transport', 'model', 'workbook', 'owner', 'collection'])
def test_factory_refuses_live_or_unbound_resources_before_any_rpc(bad):
    p = MockApiServices(id_column='E')
    args = dict(firestore_client=RestClient('firestore', p.transport()), sheets_client=RestClient('sheets', p.transport()),
                target=mock_target(), port=ApiScriptPort(), enabled=True)
    if bad == 'firestore_transport': args['firestore_client'] = RestClient('firestore')
    elif bad == 'sheets_transport': args['sheets_client'] = RestClient('sheets')
    elif bad == 'model': args['port'].mocked = False
    elif bad == 'workbook':
        from dataclasses import replace
        args['target'] = replace(args['target'], spreadsheet_id='production-business-sheet')
    elif bad == 'owner': args['settings'] = Settings(owner_id='real-owner')
    else: args['collection_id'] = 'assistant_states'
    with pytest.raises(ValueError): history_adapter_assistant(**args)
    assert not p.calls and not p.sheet_writes


def test_invalid_signature_and_foreign_owner_cannot_reach_adapters():
    a, p = setup()
    with TestClient(create_app(a)) as client:
        assert client.post('/webhooks/line', json={'events': [event('BTS 228 บาท', 'bad')]}).status_code == 401
    assert a.handle(event('BTS 228 บาท', 'foreign', owner='mock-other')).status == 'denied'
    assert not p.calls and not a.port.requests


def test_restart_keeps_history_provenance_nonce_and_exact_full_field_receipt():
    a, p = setup(); first = prepare(p, 'วันที่ 10 มี BTS 228 บาท')
    for i in range(12): a.handle(event('เรื่องอื่น '+str(i), 'chat'+str(i), i+2))
    before = persisted(p); b, _ = setup(p)
    assert len(before['history']) > 20
    assert before['draft_proofs'] and before['drafts']
    r = b.handle(event(first.data['confirm_command'], 'confirm', 30))
    assert r.status == 'recorded' and len(r.data['refs']) == 1 and not b.port.requests
    assert [p.cells[('All Expenses', 2, c)] for c in range(1, 5)] == [46305, 'BTS', 228.0, '']
    c, _ = setup(p); assert c.handle(event(first.data['confirm_command'], 'confirm', 30)) == r
    saved = persisted(p); d = next(iter(saved['drafts'].values()))
    assert d['status'] == 'recorded' and d['review'] is None and d['entries'][0]['date'] == '2026-10-10'
    assert saved['core']['receipts']['confirm']['result']['status'] == 'recorded'
    assert any(row['role'] == 'assistant' and row['event'] == 'confirm' for row in saved['history'])
    assert p.sheet_writes == 1 and not p.line_replies


def test_date_v03_future_day_question_survives_restart_then_owner_month_reply():
    a, p = setup(); r = a.handle(event('วันที่ 20 มี BTS 228 บาท', 'create'))
    assert r.status == 'clarification' and 'เดือนไหน' in r.message and not p.sheet_writes
    state = persisted(p); e = next(iter(state['drafts'].values()))['entries'][0]
    assert e['date'] is None and e['date_parts']['day'] == 20
    b, _ = setup(p); r = b.handle(event('กันยายน', 'month', 2))
    assert r.status == 'preview' and '2026-09-20' in r.message
    c, _ = setup(p); assert c.handle(event(r.data['confirm_command'], 'confirm', 3)).status == 'recorded'
    assert next(iter(persisted(p)['drafts'].values()))['entries'][0]['date'] == '2026-09-20'


def test_bangkok_midnight_and_rolling_24h_expiration_do_not_reset_unresolved_effects():
    a, p = setup(); at = NOW.replace(hour=16, minute=59)
    r = prepare(p, at=at)
    b, _ = setup(p, at+timedelta(minutes=2)); b.handle(event('เรื่องอื่นข้ามเที่ยงคืน', 'night', at=at+timedelta(minutes=2)))
    assert next(iter(persisted(p)['drafts'].values()))['entries'][0]['date'] == '2026-10-13'
    later = at+timedelta(hours=24, seconds=1); c, _ = setup(p, later)
    assert c.handle(event(r.data['confirm_command'], 'expired', at=later)).status == 'stale'
    # Read without calling expire does not change the logical expiry.
    s = history_snapshot(c.state)
    assert not s['drafts'] and not s['draft_proofs'] and not any(x['event'] == 'create' for x in s['history'])
    assert not p.sheet_writes


def test_owner_document_namespace_and_copied_foreign_envelope_rejected():
    a, p = setup(); r = prepare(p); b, _ = setup(p, owner='mock-other')
    assert b.handle(event(r.data['confirm_command'], 'foreign-confirm', 2, owner='mock-other')).status == 'stale'
    assert len(p.documents) == 2 and not p.sheet_writes
    foreign = p.documents[b.state.name]
    original = next(v for k, v in p.documents.items() if k != b.state.name)
    foreign['fields']['snapshot'] = deepcopy(original['fields']['snapshot'])
    with pytest.raises(ValueError, match='owner'): b.handle(event('เรื่องอื่น', 'foreign-read', 3, owner='mock-other'))
    assert not p.sheet_writes


@pytest.mark.parametrize('part', ['proof', 'version', 'review', 'history_source'])
def test_corrupt_persisted_history_is_rejected_before_confirmation(part):
    a, p = setup(); r = prepare(p)
    document = next(iter(p.documents.values())); v = json.loads(document['fields']['snapshot']['stringValue'])
    ref = next(iter(v['drafts'])); d = v['drafts'][ref]
    if part == 'proof': v['draft_proofs'][ref]['fingerprint'] = 'wrong'
    elif part == 'version': d['version'] += 1
    elif part == 'review': d['review']['payload_hash'] = 'wrong'
    else: v['history'][0]['source_ref'] = 'invented'
    document['fields']['snapshot']['stringValue'] = json.dumps(v)
    b, _ = setup(p)
    with pytest.raises(ValueError): b.handle(event(r.data['confirm_command'], 'confirm', 2))
    assert not p.sheet_writes and not b.port.requests


def test_stale_and_out_of_order_updates_cannot_replace_latest_draft_or_confirmation():
    a, p = setup(); first = prepare(p)
    new = a.handle(event('แก้ยอด BTS เป็น 250 บาท', 'edit', 20)); assert new.status == 'preview'
    b, _ = setup(p); older = b.handle(event('แก้ยอด BTS เป็น 260 บาท', 'older', 10))
    assert older.status == 'clarification' and older.data['tool_result']['error'] == 'older_draft_update'
    assert b.handle(event(first.data['confirm_command'], 'old-confirm', 30)).status == 'stale'
    assert b.handle(event(new.data['confirm_command'], 'early-confirm', 19)).status == 'stale'
    assert not p.sheet_writes
    c, _ = setup(p); assert c.handle(event(new.data['confirm_command'], 'latest-confirm', 40)).status == 'recorded'
    assert p.cells[('All Expenses', 2, 3)] == 250.0 and p.sheet_writes == 1


def test_cancel_consumes_provenance_and_restart_does_not_resurrect_nonce():
    a, p = setup(); r = prepare(p); ref = r.data['confirm_command'].split()[1]
    assert a.handle(event('ยกเลิก '+ref, 'cancel', 2)).status == 'cancelled'
    saved = persisted(p); assert not saved['drafts'] and not saved['draft_proofs']
    b, _ = setup(p); assert b.handle(event(r.data['confirm_command'], 'old-confirm', 3)).status == 'stale'
    assert not p.sheet_writes


def test_changed_duplicate_webhook_is_conflict_and_wrong_nonce_cannot_write():
    a, p = setup(); r = prepare(p)
    assert a.handle(event('BTS 250 บาท', 'create')).status == 'conflict'
    command = r.data['confirm_command']; wrong = command.rsplit(' ', 1)[0]+' '+'0'*16
    assert a.handle(event(wrong, 'wrong-confirm', 2)).status == 'stale' and not p.sheet_writes
    assert a.handle(event(command, 'confirm', 3)).status == 'recorded'


def test_lost_state_commit_acknowledgement_is_verified_by_nonce_and_fields():
    a, p = setup(); p.fail_after_firestore_commit = True
    r = a.handle(event('BTS 228 บาท', 'create')); assert r.status == 'preview'
    p.fail_after_firestore_commit = True
    assert a.handle(event(r.data['confirm_command'], 'confirm', 2)).status == 'recorded'
    assert p.sheet_writes == 1


def test_unverified_state_commit_ack_cannot_authorize_model_or_sheet_io():
    a, p = setup(); request = a.state.client.request
    def unknown(method, path, **kw):
        value = request(method, path, **kw)
        if method == 'POST':
            next(iter(p.documents.values()))['fields']['commit_nonce']['stringValue'] = 'different'
            raise OutcomeUnknown('Lost acknowledgement with mismatched nonce')
        return value
    a.state.client.request = unknown
    with pytest.raises(OutcomeUnknown): a.handle(event('BTS 228 บาท', 'create'))
    assert not a.port.requests and not p.sheet_writes


@pytest.mark.parametrize('timing', ['before', 'after'])
def test_sheet_timeout_uses_positive_readback_only_and_never_blind_retry(timing):
    a, p = setup(); r = prepare(p); ev = event(r.data['confirm_command'], 'confirm', 2)
    if timing == 'after': p.fail_after_sheet_write = True
    else:
        request = a.sheets.client.request
        def timeout(method, path, **kw):
            if method == 'POST': raise OutcomeUnknown('Timeout before fake write; real remote outcome is unknown')
            return request(method, path, **kw)
        a.sheets.client.request = timeout
    with pytest.raises(ProviderFailure): a.handle(ev)
    b, _ = setup(p); expected = 'recorded' if timing == 'after' else 'uncertain'
    assert b.handle(ev).status == expected
    assert b.handle(ev).status == expected and p.sheet_writes == (timing == 'after')
    if timing == 'before':
        later = NOW+timedelta(days=2); c, _ = setup(p, later)
        assert c.handle(ev).status == 'uncertain' and not p.sheet_writes
        preview = c.handle(event('BTS 100 บาท', 'new', at=later))
        assert preview.status == 'preview'
        assert c.handle(event(preview.data['confirm_command'], 'new-confirm', 2, at=later)).status == 'uncertain'
        assert not p.sheet_writes


def test_unavailable_readback_recovers_after_restart_without_second_append():
    a, p = setup(); r = prepare(p); ev = event(r.data['confirm_command'], 'confirm', 2)
    request = a.sheets.client.request
    def unavailable(method, path, **kw):
        if p.sheet_writes and '/values/' in path and 'A1%3AE1000' in path:
            raise OutcomeUnknown('Readback unavailable')
        return request(method, path, **kw)
    a.sheets.client.request = unavailable
    with pytest.raises(ProviderFailure): a.handle(ev)
    b, _ = setup(p); assert b.handle(ev).status == 'recorded' and p.sheet_writes == 1


@pytest.mark.parametrize('problem', ['missing_id', 'duplicate_id', 'changed_payload', 'partial_payload'])
def test_success_http_with_incomplete_receipt_never_reports_saved_or_resends(problem):
    a, p = setup(); r = prepare(p); ev = event(r.data['confirm_command'], 'confirm', 2)
    def corrupt(fake):
        if problem == 'missing_id': del fake.cells[('All Expenses', 2, 5)]
        elif problem == 'duplicate_id': fake.cells[('All Expenses', 3, 5)] = fake.cells[('All Expenses', 2, 5)]
        elif problem == 'changed_payload': fake.cells[('All Expenses', 2, 3)] = 999
        else: del fake.cells[('All Expenses', 2, 2)]
    p.after_sheet_write = corrupt
    with pytest.raises(ProviderFailure): a.handle(ev)
    for _ in range(3):
        b, _ = setup(p); result = b.handle(ev)
        assert result.status == 'uncertain' and 'บันทึกแล้ว' not in result.message and p.sheet_writes == 1
    assert next(iter(persisted(p)['drafts'].values()))['status'] != 'recorded'


def test_full_batch_ids_and_inputs_required_for_any_saved_response():
    port = Port(); a, p = setup(port=port)
    def proposal(body):
        ref = json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref': None, 'entries': [
            data(item='BTS', amount=65, note='', source_refs=[ref]), data(item='seven', amount=100, note='', source_refs=[ref])]})
    port.values = [proposal]
    r = a.handle(event('BTS 65\nseven 100', 'create')); assert r.status == 'preview'
    p.after_sheet_write = lambda fake: fake.cells.pop(('All Expenses', 3, 5))
    ev = event(r.data['confirm_command'], 'confirm', 2)
    with pytest.raises(ProviderFailure): a.handle(ev)
    b, _ = setup(p); assert b.handle(ev).status == 'uncertain' and p.sheet_writes == 1


def test_catalog_enrollment_uses_existing_atomic_adapter_and_exact_mapping_readback():
    port = Port(); a, p = setup(port=port)
    def proposal(body):
        ref = json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref': None, 'entries': [
            data(item='น้ำมะนาว', amount=45, note='', source_refs=[ref])]})
    port.values = [proposal, response(text='กรุณาเลือกหมวด')]
    assert a.handle(event('น้ำมะนาว 45 บาท', 'create')).status == 'clarification'
    d = next(iter(a.state.drafts.values())); old = d['entries'][0]
    def mapping(body):
        ref = json.loads(body['input'][0]['content'])['current_source_ref']
        return response(name='prepare_expenses', args={'draft_ref': d['draft_ref'], 'draft_version': d['version'],
            'entries': [data(item=old['item'], amount=old['amount'], date=old['date'], note=old['note'],
                             entry_ref=old['entry_ref'], category='Synthetic', subcategory='', source_refs=[ref])]})
    port.values = [mapping]
    a, _ = setup(p, port=port)
    r = a.handle(event('หมวด Synthetic', 'mapping', 2)); assert r.status == 'preview', r
    assert not p.sheet_writes
    b, _ = setup(p); assert b.handle(event(r.data['confirm_command'], 'confirm', 3)).status == 'recorded'
    assert p.sheet_writes == 1 and p.cells[('Database', 6, 1)] == 'น้ำมะนาว'
    assert p.cells[('Database', 6, 2)] == 'Synthetic' and p.cells[('All Expenses', 2, 2)] == 'น้ำมะนาว'


@pytest.mark.parametrize('phase,expected,writes', [
    ('prepared', 'recorded', 1), ('executing', 'uncertain', 0), ('before_append', 'uncertain', 0),
    ('after_append', 'recorded', 1), ('effect_returned', 'recorded', 1),
    ('action_completed', 'recorded', 1), ('receipt_completed', 'recorded', 1), ('history_turn_completed', 'recorded', 1)])
def test_actual_process_death_and_expired_firestore_lease_recovery(tmp_path, phase, expected, writes):
    directory = tmp_path/'fake'; p = PersistentApiMock(directory)
    r = prepare(p); ev = event(r.data['confirm_command'], 'confirm', 2)
    stopped = worker(directory, ev, phase); assert stopped.returncode == 73, stopped.stderr
    resumed = result(worker(directory, ev, now=NOW+timedelta(seconds=121)), expected)
    assert resumed['model_requests'] == 0
    result(worker(directory, ev, now=NOW+timedelta(seconds=122)), expected)
    final = PersistentApiMock(directory); assert final.sheet_writes == writes
    state = persisted(final); d = next(iter(state['drafts'].values()))
    assert (d['status'] == 'recorded') == (expected == 'recorded')
    assert state['core']['receipts']['confirm']['result']['status'] == expected


def test_actual_review_process_exit_reuses_saved_model_decision_after_lease_expiry(tmp_path):
    directory = tmp_path/'fake'; PersistentApiMock(directory); ev = event('BTS 228 บาท', 'create')
    stopped = worker(directory, ev, 'history_review_pre_receipt'); assert stopped.returncode == 73, stopped.stderr
    r = result(worker(directory, ev, now=NOW+timedelta(seconds=121)), 'preview')
    assert r['model_requests'] == 0 and PersistentApiMock(directory).sheet_writes == 0
    result(worker(directory, event(r['data']['confirm_command'], 'confirm', 123000), now=NOW+timedelta(seconds=123)), 'recorded')
    assert PersistentApiMock(directory).sheet_writes == 1


def test_model_attempt_death_preserves_reservation_without_retry_or_refund(tmp_path):
    directory = tmp_path/'fake'; PersistentApiMock(directory); ev = event('BTS 228 บาท', 'create')
    stopped = worker(directory, ev, 'model_started'); assert stopped.returncode == 73, stopped.stderr
    before = PersistentApiMock(directory)
    snapshot = json.loads(next(iter(before.documents.values()))['fields']['snapshot']['stringValue'])
    assert len(snapshot['core']['model_runs']) == 1
    r = result(worker(directory, ev, now=NOW+timedelta(seconds=121)), 'model_budget_exhausted')
    assert r['model_requests'] == 0
    saved = persisted(PersistentApiMock(directory))
    assert saved['core']['model_budgets'] == snapshot['core']['model_budgets']
    assert next(iter(saved['core']['model_runs'].values()))['status'] == 'reserved'
    assert not PersistentApiMock(directory).sheet_writes


def test_fake_model_saved_claim_never_becomes_business_success():
    port = Port(); a, p = setup(port=port)
    port.values = [response(text='บันทึกรายจ่ายให้แล้ว')]
    r = a.handle(event('BTS 228 บาท', 'create'))
    assert r.status == 'clarification' and not p.sheet_writes and not a.state.actions
    saved = persisted(p)
    assert saved['core']['receipts']['create']['result']['status'] == 'clarification'


@pytest.mark.parametrize('drift', ['catalog', 'metadata', 'payload'])
def test_changes_between_preview_and_confirm_block_write_and_saved_claim(drift):
    a, p = setup(); r = prepare(p)
    if drift == 'catalog': p.cells[('Database', 2, 2)] = 'Changed'
    elif drift == 'metadata': p.cells[('All Expenses', 1, 5)] = 'wrong_id_header'
    else:
        document = next(iter(p.documents.values())); v = json.loads(document['fields']['snapshot']['stringValue'])
        next(iter(v['drafts'].values()))['entries'][0]['amount'] = '999'
        document['fields']['snapshot']['stringValue'] = json.dumps(v)
    b, _ = setup(p)
    if drift == 'payload':
        with pytest.raises(ValueError): b.handle(event(r.data['confirm_command'], 'confirm', 2))
    else:
        assert b.handle(event(r.data['confirm_command'], 'confirm', 2)).status == ('stale' if drift == 'catalog' else 'setup_required')
    assert not p.sheet_writes


def test_six_actual_process_duplicates_use_one_lease_receipt_and_append(tmp_path):
    directory = tmp_path/'fake'; PersistentApiMock(directory); ev = event('BTS 228 บาท', 'create')
    with ThreadPoolExecutor(max_workers=6) as pool:
        processes = list(pool.map(lambda _: worker(directory, ev), range(6)))
    assert all(p.returncode in {0, 75} for p in processes), [p.stderr for p in processes]
    winner = next(p for p in processes if p.returncode == 0); r = result(winner, 'preview')
    for _ in range(3): assert result(worker(directory, ev), 'preview')['data'] == r['data']
    confirm = event(r['data']['confirm_command'], 'confirm', 2)
    with ThreadPoolExecutor(max_workers=6) as pool:
        processes = list(pool.map(lambda _: worker(directory, confirm), range(6)))
    assert any(p.returncode == 0 for p in processes) and all(p.returncode in {0, 75} for p in processes)
    for _ in range(3): result(worker(directory, confirm), 'recorded')
    final = PersistentApiMock(directory); assert final.sheet_writes == 1
    assert len(persisted(final)['core']['actions']) == 1


def test_overlapping_correction_and_confirm_busy_worker_reloads_latest_version():
    a, p = setup(); r = prepare(p); b, _ = setup(p)
    entered, release = threading.Event(), threading.Event()
    def pause(phase, key):
        if phase == 'history_review_pre_receipt': entered.set(); assert release.wait(5)
    a.phase_hook = pause
    with ThreadPoolExecutor(max_workers=2) as pool:
        edit = pool.submit(a.handle, event('แก้ยอด BTS เป็น 250 บาท', 'edit', 20))
        assert entered.wait(5)
        with pytest.raises(LeaseBusy): b.handle(event(r.data['confirm_command'], 'old-confirm', 30))
        release.set(); new = edit.result(); assert new.status == 'preview'
    assert b.handle(event(r.data['confirm_command'], 'old-confirm', 30)).status == 'stale'
    assert b.handle(event(new.data['confirm_command'], 'new-confirm', 40)).status == 'recorded'
    assert p.sheet_writes == 1 and p.cells[('All Expenses', 2, 3)] == 250.0


@pytest.mark.parametrize('boundary', ['before_effect', 'inflight'])
def test_expired_old_worker_cannot_commit_and_takeover_never_duplicates_inflight_write(boundary):
    current = [NOW]; p = MockApiServices(id_column='E')
    a, b = runtime(p, lambda: current[0]), runtime(p, lambda: current[0])
    r = prepare(p); ev = event(r.data['confirm_command'], 'confirm', 2)
    seen = []
    def takeover(*args):
        current[0] += timedelta(seconds=121)
        seen.append(b.handle(ev).status)
    if boundary == 'before_effect':
        a.phase_hook = lambda phase, key: takeover() if phase == 'executing' else None
    else:
        # Request already sent, but fake has not applied its append yet.
        p.before_sheet_write = takeover
    with pytest.raises(LeaseLost): a.handle(ev)
    assert seen == ['uncertain'] and p.sheet_writes == (boundary == 'inflight')
    expected = 'recorded' if boundary == 'inflight' else 'uncertain'
    assert b.handle(ev).status == expected and b.handle(ev).status == expected
    assert p.sheet_writes == (boundary == 'inflight')


def test_out_of_session_checkpoint_and_storage_cas_reject_stale_fence():
    a, p = setup(); state = a.state
    with pytest.raises(LeaseLost): state.checkpoint()
    with state.session():
        old = deepcopy(state.document); state.checkpoint()
        with pytest.raises(LeaseLost):
            state._commit(old, state._fields(state._snapshot_state(), state.holder, state.fence, NOW+timedelta(seconds=120)))
    assert not p.sheet_writes


def test_prepared_action_cannot_append_after_its_original_draft_24h_expiry(tmp_path):
    directory = tmp_path/'fake'; p = PersistentApiMock(directory); r = prepare(p)
    confirmed_at = NOW+timedelta(hours=23, minutes=59)
    ev = event(r.data['confirm_command'], 'confirm', at=confirmed_at)
    stopped = worker(directory, ev, 'prepared', confirmed_at); assert stopped.returncode == 73, stopped.stderr
    result(worker(directory, ev, now=NOW+timedelta(hours=24, minutes=1)), 'expired')
    assert PersistentApiMock(directory).sheet_writes == 0


def test_committed_but_unacknowledged_action_can_be_verified_after_draft_expiry(tmp_path):
    directory = tmp_path/'fake'; p = PersistentApiMock(directory); r = prepare(p)
    confirmed_at = NOW+timedelta(hours=23, minutes=59)
    ev = event(r.data['confirm_command'], 'confirm', at=confirmed_at)
    stopped = worker(directory, ev, 'after_append', confirmed_at); assert stopped.returncode == 73, stopped.stderr
    result(worker(directory, ev, now=NOW+timedelta(hours=24, minutes=1)), 'recorded')
    assert PersistentApiMock(directory).sheet_writes == 1


def test_noninput_formula_columns_untouched_with_separate_z_receipt_metadata():
    from dataclasses import replace
    from line_assistant.sheet_metadata import TransactionIdMetadata
    p = MockApiServices(id_column='Z'); p.column_count = 26
    p.cells[('All Expenses', 1, 5)] = '=synthetic_category_formula'
    p.cells[('All Expenses', 1, 6)] = '=synthetic_subcategory_formula'
    before = deepcopy(p.cells)
    target = replace(mock_target(), transaction_id_metadata=TransactionIdMetadata('Z', schema_approved=True))
    a = history_adapter_assistant(firestore_client=RestClient('firestore', p.transport()),
                                 sheets_client=RestClient('sheets', p.transport()), target=target,
                                 port=ApiScriptPort(), enabled=True, clock=lambda: NOW)
    r = a.handle(event('BTS 228 บาท', 'create')); assert r.status == 'preview'
    assert a.handle(event(r.data['confirm_command'], 'confirm', 2)).status == 'recorded'
    assert all(p.cells[k] == v for k, v in before.items())
    assert all(('All Expenses', 2, c) not in p.cells for c in range(5, 26))
    assert isinstance(p.cells[('All Expenses', 2, 26)], str) and p.sheet_writes == 1


def test_snapshot_capacity_blocks_model_and_business_io():
    p = MockApiServices(id_column='E')
    state = FirestoreHistoryState(RestClient('firestore', p.transport()), 'mock-owner', max_snapshot_bytes=10)
    with pytest.raises(OutcomeUnknown, match='capacity'):
        with state.session(): pytest.fail('Oversized envelope must not acquire a lease')
    assert not p.documents and not p.sheet_writes
