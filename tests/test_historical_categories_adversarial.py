"""Independent signed-webhook regressions; synthetic providers and decisions only."""
from copy import deepcopy
from datetime import timedelta
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from line_assistant.persistence import result_from
from line_assistant.pilot_http import rpc_budget
from test_calendar_all_day_v15 import proposal as calendar_proposal
from test_calendar_mvp import response
from test_history_first import data as expense_entry
from test_historical_categories import (
    HistoricalHosted, FullHistoricalHosted, command, context, preview,
    propose, review, scan, turn,
)


@pytest.fixture(autouse=True)
def deny_external_connections(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Adversarial regressions must never use external sockets')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def deliver(api, event, *answers):
    api.queue = list(answers)
    with TestClient(api.app()) as web:
        assert api.send(web, deepcopy(event)).status_code == 200
    return result_from(api.snapshot()['receipts'][event['webhookEventId']]['result'])


def test_delayed_cancel_cannot_discard_a_newer_historical_review():
    api = HistoricalHosted()
    preview(api)
    delayed = api.event('ยกเลิก')
    turn(api, 'เอาเฉพาะ Unknown Metro Ticket',
         review(selection='only', selected=['Unknown Metro Ticket']))
    current = deepcopy(api.snapshot()['pending'])
    result = deliver(api, delayed)
    assert result.status == 'stale'
    assert api.snapshot()['pending'] == current
    assert api.mapping_posts == 0


@pytest.mark.parametrize('historical_pending', [False, True])
def test_new_calendar_review_receives_its_confirmation_while_historical_review_waits(historical_pending):
    api = FullHistoricalHosted()
    if historical_pending:
        preview(api)
    historical = deepcopy(api.snapshot()['pending'])
    result, _ = turn(api, 'เพิ่มนัดตรวจระบบวันที่ 9 พ.ย. 2026 ทั้งวัน',
                     calendar_proposal([{'title': 'ตรวจระบบ', 'date_text': '9 พ.ย. 2026', 'all_day': True}]))
    assert result.status == 'preview' and result.data['calendar_view']['all_day'] is True
    result, _ = turn(api, 'ยืนยัน')
    assert result.status == 'created', result
    assert api.calendar.posts == 1 and api.mapping_posts == 0
    assert api.snapshot()['pending'] == historical


@pytest.mark.parametrize('historical_pending', [False, True])
def test_new_expense_review_receives_confirmation_while_historical_review_waits(historical_pending):
    api = FullHistoricalHosted()
    if historical_pending:
        preview(api)
    historical = deepcopy(api.snapshot()['pending'])

    def expense(body):
        c = context(body)
        return response('prepare_expenses', {'draft_ref': None, 'draft_version': None,
                        'entries': [expense_entry(item='RailPass', amount='17',
                                    source_refs=[c['current_source_ref']])]})

    result, _ = turn(api, 'RailPass 17 บาท', expense)
    assert result.status == 'preview', result
    result, _ = turn(api, 'ยืนยัน')
    assert result.status == 'recorded', result
    assert api.google.sheet_writes == 1 and api.mapping_posts == 0
    assert api.snapshot()['pending'] == historical


@pytest.mark.parametrize('mode', ['permanent', 'query'])
def test_confirmation_from_previous_page_cannot_apply_to_next_page(mode):
    api = HistoricalHosted()
    api.many(12)
    preview(api)
    old = command(api, mode=mode, explicit=True)
    turn(api, 'หน้าถัดไป', review('next'), propose)
    before = deepcopy(api.google.cells)
    result, _ = turn(api, old)
    assert result.status == 'stale'
    assert api.google.cells == before and api.mapping_posts == 0


def test_model_cross_page_correction_is_rejected_without_changing_review():
    api = HistoricalHosted()
    api.many(12)
    result, _ = preview(api)
    old = result.data['tool_result']['groups'][0]
    turn(api, 'หน้าถัดไป', review('next'), propose)
    before = deepcopy(api.snapshot()['pending'])

    def cross_page(body):
        c = context(body)
        return response('review_historical_categories', {
            'review_ref': c['historical_categories']['review_ref'],
            'operation': 'revise', 'selection': 'all_clear', 'selected_refs': [],
            'source_refs': [c['current_source_ref']],
            'changes': [{'group_ref': old['group_ref'], 'status': 'proposed',
                         'category': 'Food', 'subcategory': '', 'candidates': [],
                         'source_text': old['item']}],
        })

    result, _ = turn(api, old['item'] + ' เป็น Food', cross_page)
    assert result.data['tool_result']['status'] == 'historical_unavailable'
    assert api.snapshot()['pending'] == before and api.mapping_posts == 0


@pytest.mark.parametrize('mode', ['permanent', 'query'])
def test_delayed_approval_before_correction_never_authorizes_new_selection(mode):
    api = HistoricalHosted()
    api.many(3)
    preview(api)
    delayed = api.event(command(api, mode=mode))
    turn(api, 'เอาเฉพาะ Unmapped transit sample 02',
         review(selection='only', selected=['Unmapped transit sample 02']))
    before = deepcopy(api.google.cells)
    result = deliver(api, delayed)
    assert result.status == 'stale'
    assert api.google.cells == before and api.mapping_posts == 0


def test_duplicate_correction_event_does_not_reopen_consumed_review_or_rewrite_mapping():
    api = HistoricalHosted()
    preview(api)
    result, event = turn(api, 'Unknown Metro Ticket เป็น Food',
                         review(changes=[('Unknown Metro Ticket', 'Food', '')]))
    assert result.status == 'preview'
    result, _ = turn(api, command(api))
    assert result.status == 'mapping_saved' and api.mapping_posts == 1
    before = deepcopy(api.google.cells)
    deliver(api, event)
    assert api.snapshot()['pending'] is None
    assert api.google.cells == before and api.mapping_posts == 1


class PartialMappingHosted(HistoricalHosted):
    """An uncertain provider result exposes only one of several desired rows."""
    def handle(self, request):
        if self.configured and request.url.host == 'sheets.googleapis.com' and request.method == 'POST':
            body = json.loads(request.content)
            blocks = body.get('requests', [])
            if len(blocks) == 1 and blocks[0].get('appendCells', {}).get('sheetId') == 502:
                self.mapping_posts += 1
                first = blocks[0]['appendCells']['rows'][0]
                last = max(row for (tab, row, col), value in self.google.cells.items()
                           if tab == 'Database' and value not in ('', None))
                for col, cell in enumerate(first['values'], 1):
                    self.google.cells[('Database', last + 1, col)] = cell['userEnteredValue']['stringValue']
                raise httpx.ReadTimeout('Synthetic partial observed state after unknown outcome', request=request)
        return super().handle(request)


def test_partial_observed_mapping_stays_uncertain_and_never_retries_even_after_cancel():
    api = PartialMappingHosted()
    api.many(3)
    preview(api)
    result, event = turn(api, command(api))
    assert result.status == 'uncertain' and api.mapping_posts == 1
    before = deepcopy(api.google.cells)
    result, _ = turn(api, 'ยกเลิก')
    assert result.status == 'uncertain'
    deliver(api, event)
    api.historical = api.labels = api.mapping = False
    result, _ = turn(api, 'ตรวจผลหมวดย้อนหลัง', response('check_historical_mapping', {}))
    action = next(a for a in api.snapshot()['actions'].values()
                  if a['metadata'].get('historical_classification'))
    assert action['status'] == 'uncertain'
    assert api.google.cells == before and api.mapping_posts == 1


@pytest.mark.parametrize('mutation', ['unseen_transaction', 'different_month', 'unrelated_catalog'])
def test_changes_outside_displayed_groups_revoke_whole_snapshot_consent(mutation):
    api = HistoricalHosted()
    api.many(12)
    preview(api)
    if mutation == 'unseen_transaction':
        api.google.cells[('All Expenses', 13, 3)] = 71
    elif mutation == 'different_month':
        api.google.cells[('All Expenses', 2, 1)] += 31
    else:
        api.google.cells[('Database', 3, 3)] = 'Changed Rail'
    before = deepcopy(api.google.cells)
    result, _ = turn(api, command(api))
    assert result.status == 'stale'
    assert api.google.cells == before and api.mapping_posts == 0


def test_query_only_partial_consent_does_not_promote_unselected_or_ambiguous_groups():
    api = HistoricalHosted()
    preview(api)
    result, _ = turn(api, 'เอาเฉพาะ Mystery Token', review(selection='only', selected=['Mystery Token']))
    assert not any(g['included'] for g in result.data['tool_result']['groups'])
    result, _ = turn(api, command(api, mode='query'))
    assert result.status == 'clarification'
    assert api.mapping_posts == 0


@pytest.mark.parametrize('operation', ['refresh', 'scan', 'cancel'])
def test_delayed_model_operation_cannot_replace_newer_owner_selection(operation):
    api = HistoricalHosted()
    api.many(3)
    preview(api)
    delayed = api.event({'refresh': 'ตรวจใหม่', 'scan': 'จัดหมวดใหม่ทั้งหมด',
                         'cancel': 'ไม่เอาชุดนี้แล้ว'}[operation])
    turn(api, 'เอาเฉพาะ Unmapped transit sample 02',
         review(selection='only', selected=['Unmapped transit sample 02']))
    before = deepcopy(api.snapshot()['pending'])
    answers = [scan, propose] if operation == 'scan' else [review(operation), propose]
    result = deliver(api, delayed, *answers)
    assert api.snapshot()['pending'] == before, result
    assert api.mapping_posts == 0


def test_proposal_tool_cannot_reset_an_explicit_partial_selection():
    api = HistoricalHosted()
    api.many(3)
    preview(api)
    turn(api, 'เอาเฉพาะ Unmapped transit sample 02',
         review(selection='only', selected=['Unmapped transit sample 02']))
    before = deepcopy(api.snapshot()['pending'])
    result, _ = turn(api, 'ขอดูข้อเสนอเดิมอีกที', propose)
    assert api.snapshot()['pending'] == before, result
    assert api.mapping_posts == 0


def test_show_same_review_retains_subset_and_mints_a_fresh_review():
    api = HistoricalHosted()
    api.many(3)
    preview(api)
    selected = 'Unmapped transit sample 02'
    turn(api, 'เลือกเฉพาะ ' + selected, review(selection='only', selected=[selected]))
    before = deepcopy(api.snapshot()['pending']['payload'])
    calls = len(api.model_bodies)
    result, _ = turn(api, 'ขอดูร่างเดิมโดยคงการเลือกไว้', review(selection='keep'))
    after = api.snapshot()['pending']['payload']
    assert result.status == 'preview' and len(api.model_bodies) == calls + 1
    assert after['groups'] == before['groups']
    assert after['version'] == before['version'] + 1 and after['nonce'] != before['nonce']
    result, _ = turn(api, command(api, mode='query'))
    assert result.data['included_names'] == [selected] and api.mapping_posts == 0


@pytest.mark.parametrize('obstacle', ['delayed_cancel', 'undelivered'])
def test_redisplay_after_lost_focus_requires_fresh_delivered_consent(obstacle):
    api = HistoricalHosted()
    preview(api)
    delayed = api.event('ยกเลิก') if obstacle == 'delayed_cancel' else None
    selected = 'Unknown Metro Ticket'
    api.reject_reply = obstacle == 'undelivered'
    turn(api, 'เลือกเฉพาะ ' + selected, review(selection='only', selected=[selected]))
    api.reject_reply = False
    before = deepcopy(api.snapshot()['pending']['payload'])
    if delayed:
        assert deliver(api, delayed).status == 'stale'
    result, _ = turn(api, command(api, mode='query'))
    assert result.status == 'stale' and api.snapshot()['pending']['payload'] == before
    result, _ = turn(api, 'ขอดูร่างเดิมโดยคงการเลือกไว้', review(selection='keep'))
    after = api.snapshot()['pending']['payload']
    assert result.status == 'preview' and after['groups'] == before['groups']
    assert after['version'] == before['version'] + 1 and after['nonce'] != before['nonce']
    result, _ = turn(api, command(api, mode='query'))
    assert result.status == 'summary' and result.data['included_names'] == [selected]
    assert api.mapping_posts == 0


def test_redisplay_after_lost_focus_still_checks_source_freshness():
    api = HistoricalHosted()
    preview(api)
    before = deepcopy(api.snapshot()['pending'])
    api.google.cells[('All Expenses', 3, 3)] = 999
    result, _ = turn(api, 'ขอดูร่างเดิม', review(selection='keep'))
    assert result.status != 'preview' and api.snapshot()['pending'] == before
    assert api.mapping_posts == 0


@pytest.mark.parametrize('mode', ['query', 'permanent'])
def test_identical_only_subset_redisplay_after_stale_reply_requires_fresh_consent(mode):
    api = HistoricalHosted()
    api.many(3)
    preview(api)
    delayed = api.event('ยกเลิก')
    selected = 'Unmapped transit sample 01'
    turn(api, 'เลือกเฉพาะ ' + selected, review(selection='only', selected=[selected]))
    before = deepcopy(api.snapshot()['pending']['payload'])
    assert deliver(api, delayed).status == 'stale'
    result, _ = turn(api, 'ขอดูร่างเดิม คงเลือกเฉพาะ ' + selected,
                     review(selection='only', selected=[selected]))
    after = api.snapshot()['pending']['payload']
    assert result.status == 'preview'
    assert after['groups'] == before['groups'] and after['selection'] == before['selection'] == 'only'
    assert after['version'] == before['version'] + 1 and after['nonce'] != before['nonce']
    assert api.mapping_posts == api.google.sheet_writes == api.calendar.posts == 0
    result, _ = turn(api, command(api, mode=mode))
    if mode == 'query':
        assert result.status == 'summary' and result.data['included_names'] == [selected]
        assert api.mapping_posts == 0
    else:
        assert result.status == 'mapping_saved'
        assert [row['item'] for row in result.data['mapping_rows']] == [selected]
        assert api.mapping_posts == 1


@pytest.mark.parametrize('subset', ['replacement', 'expanded', 'empty'])
def test_changed_only_subset_cannot_use_redisplay_exception_after_stale_reply(subset):
    api = HistoricalHosted()
    api.many(3)
    preview(api)
    delayed = api.event('ยกเลิก')
    selected = 'Unmapped transit sample 01'
    other = 'Unmapped transit sample 02'
    turn(api, 'เลือกเฉพาะ ' + selected, review(selection='only', selected=[selected]))
    assert deliver(api, delayed).status == 'stale'
    before = deepcopy(api.snapshot()['pending'])
    wanted = [other] if subset == 'replacement' else [selected, other] if subset == 'expanded' else []
    result, _ = turn(api, 'ขอเลือกชุดใหม่', review(selection='only', selected=wanted))
    assert result.data['tool_result']['status'] == 'historical_unavailable'
    assert api.snapshot()['pending'] == before
    assert api.mapping_posts == api.google.sheet_writes == api.calendar.posts == 0


@pytest.mark.parametrize('operation', ['change', 'only', 'all_clear', 'next', 'previous', 'cancel', 'nonempty_selected_refs'])
def test_redisplay_exception_does_not_allow_mutations_after_intervening_reply(operation):
    api = HistoricalHosted()
    api.many(23)
    preview(api)
    turn(api, 'หน้าถัดไป', review('next'), propose)
    selected = 'Unmapped transit sample 12'
    turn(api, 'เลือกเฉพาะ ' + selected, review(selection='only', selected=[selected]))
    turn(api, 'ขอบคุณ', response(text='ครับ'))
    before = deepcopy(api.snapshot()['pending'])
    if operation == 'change':
        answer = review(changes=[('Unmapped transit sample 10', 'Food', '')])
    elif operation == 'only':
        answer = review(selection='only', selected=['Unmapped transit sample 11'])
    elif operation == 'all_clear':
        answer = review(selection='all_clear')
    elif operation == 'nonempty_selected_refs':
        answer = review(selection='keep', selected=[selected])
    else:
        answer = review(operation)
    result, _ = turn(api, 'Unmapped transit sample 10 เป็น Food และขอเปลี่ยนชุด', answer)
    assert result.data['tool_result']['status'] == 'historical_unavailable'
    assert api.snapshot()['pending'] == before
    assert api.mapping_posts == api.google.sheet_writes == api.calendar.posts == 0


@pytest.mark.parametrize('invalidity', ['catalog', 'expired', 'owner', 'workbook', 'seal'])
def test_redisplay_exception_cannot_renew_invalid_pending_review(invalidity):
    api = HistoricalHosted()
    preview(api)
    ref = api.snapshot()['pending']['payload']['ref']
    turn(api, 'ขอบคุณ', response(text='ครับ'))
    if invalidity == 'catalog':
        api.google.cells[('Database', 3, 3)] = 'Changed Rail'
    elif invalidity == 'expired':
        api.now += timedelta(minutes=31)
    else:
        app = api.app()
        with rpc_budget(40), app.state.assistant.state.session():
            app.state.assistant.state.pending.payload[invalidity] = 'foreign-or-modified'
            app.state.assistant.state.checkpoint()
        app.state.pilot_http.close()
    before = deepcopy(api.snapshot()['pending'])

    def redisplay(body):
        c = context(body)
        return response('review_historical_categories', {
            'review_ref': ref, 'operation': 'revise', 'changes': [],
            'selection': 'keep', 'selected_refs': [],
            'source_refs': [c['current_source_ref']],
        })

    result, _ = turn(api, 'ขอดูร่างเดิม', redisplay)
    assert result.data['tool_result']['status'] == 'historical_unavailable'
    assert api.snapshot()['pending'] == (None if invalidity == 'expired' else before)
    assert api.mapping_posts == api.google.sheet_writes == api.calendar.posts == 0


@pytest.mark.parametrize('mode', ['query', 'permanent'])
@pytest.mark.parametrize('confirmation', ['old_nonce', 'new_nonce', 'plain'])
def test_undelivered_redisplay_authorizes_neither_old_nor_new_confirmation(mode, confirmation):
    api = HistoricalHosted()
    preview(api)
    old = command(api, mode=mode, explicit=True)
    turn(api, 'ขอบคุณ', response(text='ครับ'))
    api.reject_reply = True
    result, _ = turn(api, 'ขอดูร่างเดิม', review(selection='keep'))
    assert result.status == 'preview'
    api.reject_reply = False
    new = command(api, mode=mode, explicit=True)
    assert new != old
    before = deepcopy(api.snapshot()['pending'])
    text = old if confirmation == 'old_nonce' else new if confirmation == 'new_nonce' else command(api, mode=mode)
    result, _ = turn(api, text)
    assert result.status == 'stale'
    assert api.snapshot()['pending'] == before
    assert api.mapping_posts == api.google.sheet_writes == api.calendar.posts == 0


@pytest.mark.parametrize('older_by_ms', [0, 1])
def test_redisplay_exception_rejects_tied_or_older_distinct_events(older_by_ms):
    api = HistoricalHosted()
    preview(api)
    _, selected_event = turn(api, 'เลือกเฉพาะ Unknown Metro Ticket',
                             review(selection='only', selected=['Unknown Metro Ticket']))
    turn(api, 'ขอบคุณ', response(text='ครับ'))
    before = deepcopy(api.snapshot()['pending'])
    event = api.event('ขอดูร่างเดิม')
    event['timestamp'] = selected_event['timestamp'] - older_by_ms
    result = deliver(api, event, review(selection='keep'))
    assert result.data['tool_result']['status'] == 'historical_unavailable'
    assert api.snapshot()['pending'] == before
    assert api.mapping_posts == api.google.sheet_writes == api.calendar.posts == 0


def test_historical_tool_source_choices_are_exact_owned_refs_including_current():
    api = HistoricalHosted()
    preview(api)
    turn(api, 'เลือกเฉพาะ Unknown Metro Ticket',
         review(selection='only', selected=['Unknown Metro Ticket']))
    for body in api.model_bodies:
        c = context(body)
        owned = {c['current_source_ref']} | {
            row['source_ref'] for row in c['history'] if row['role'] == 'user'
        }
        for tool in body['tools']:
            if tool['name'] in {'scan_historical_categories', 'review_historical_categories'}:
                allowed = tool['parameters']['properties']['source_refs']['items']['enum']
                assert c['current_source_ref'] in allowed
                assert set(allowed) <= owned
                assert c['current_source_ref'][:-1] not in allowed


def test_historical_schema_tracks_exact_page_sizes_and_excludes_other_page_refs():
    api = HistoricalHosted()
    api.many(23)
    result, _ = preview(api)
    pages = [{g['group_ref'] for g in result.data['tool_result']['groups']}]
    for _ in range(2):
        result, _ = turn(api, 'หน้าถัดไป', review('next'), propose)
        assert result.status == 'preview'
        pages.append({g['group_ref'] for g in result.data['tool_result']['groups']})
    assert [len(page) for page in pages] == [10, 10, 3]
    assert len(set.union(*pages)) == 23
    all_refs = set.union(*pages)
    checked_counts = []
    for body in api.model_bodies:
        h = context(body)['historical_categories']
        if not h.get('groups'):
            continue
        current = {g['group_ref'] for g in h['groups']}
        tools = {tool['name']: tool['parameters']['properties'] for tool in body['tools']}
        proposal = tools['propose_historical_categories']
        revision = tools['review_historical_categories']
        assert proposal['proposals']['minItems'] == proposal['proposals']['maxItems'] == len(current)
        assert proposal['review_ref']['enum'] == revision['review_ref']['enum'] == [h['review_ref']]
        choices = [proposal['proposals']['items']['properties']['group_ref'],
                   revision['changes']['items']['properties']['group_ref'], revision['selected_refs']['items']]
        for choice in choices:
            assert set(choice['enum']) == current
            assert not set(choice['enum']) & (all_refs - current)
            assert 'hg_foreign' not in choice['enum']
        checked_counts.append(len(current))
    assert 10 in checked_counts and 3 in checked_counts
    assert api.mapping_posts == api.google.sheet_writes == api.calendar.posts == 0


def test_exact_proposal_array_length_does_not_replace_server_unique_coverage_check():
    api = HistoricalHosted()
    api.many(10)

    def duplicate(body):
        args = json.loads(propose(body)['output'][0]['arguments'])
        args['proposals'][-1] = deepcopy(args['proposals'][0])
        assert len(args['proposals']) == 10
        return response('propose_historical_categories', args)

    result, _ = turn(api, 'จัดหมวดย้อนหลังทั้งหมด', scan, duplicate)
    assert result.data['tool_result']['status'] == 'historical_unavailable'
    assert api.snapshot()['pending']['payload']['stage'] == 'unclassified'
    assert api.mapping_posts == api.google.sheet_writes == api.calendar.posts == 0


def test_review_ref_schema_uses_delivered_continuation_after_query_only_consent():
    api = HistoricalHosted()
    api.many(13)
    preview(api)
    old_ref = api.snapshot()['pending']['payload']['ref']
    result, _ = turn(api, command(api, mode='query'))
    continuation = result.data['historical_continuation']['ref']
    assert api.snapshot()['pending'] is None
    result, _ = turn(api, 'หน้าถัดไป', review('next'), propose)
    assert result.status == 'preview' and result.data['tool_result']['shown'] == 3
    navigation = api.model_bodies[-2]
    tool = next(t for t in navigation['tools'] if t['name'] == 'review_historical_categories')
    assert tool['parameters']['properties']['review_ref']['enum'] == [continuation]
    proposal = api.model_bodies[-1]
    tool = next(t for t in proposal['tools'] if t['name'] == 'propose_historical_categories')
    assert tool['parameters']['properties']['review_ref']['enum'] == [context(proposal)['historical_categories']['review_ref']]
    assert context(proposal)['historical_categories']['review_ref'] != old_ref
    assert api.mapping_posts == api.google.sheet_writes == api.calendar.posts == 0


def test_correcting_an_unselected_name_with_keep_selection_does_not_add_it_back():
    api = HistoricalHosted()
    api.many(3)
    preview(api)
    selected = 'Unmapped transit sample 02'
    other = 'Unmapped transit sample 00'
    turn(api, 'เอาเฉพาะ ' + selected, review(selection='only', selected=[selected]))
    result, _ = turn(api, other + ' เป็น Food แต่ยังเลือกเฉพาะ ' + selected,
                     review(selection='keep', changes=[(other, 'Food', '')]))
    groups = result.data['tool_result']['groups']
    assert next(g for g in groups if g['item'] == other)['category'] == 'Food'
    assert [g['item'] for g in groups if g['included']] == [selected]
    result, _ = turn(api, command(api))
    assert [r['item'] for r in result.data['mapping_rows']] == [selected]


def test_undelivered_partial_revision_cannot_use_previously_delivered_consent():
    api = HistoricalHosted()
    api.many(3)
    preview(api)
    previous_command = command(api, explicit=True)
    api.reject_reply = True
    result, _ = turn(api, 'เอาเฉพาะ Unmapped transit sample 02',
                     review(selection='only', selected=['Unmapped transit sample 02']))
    assert result.status == 'preview'
    api.reject_reply = False
    result, _ = turn(api, previous_command)
    assert result.status == 'stale' and api.mapping_posts == 0


def test_partial_selection_can_escape_oversized_ambiguous_candidates():
    api = HistoricalHosted()
    api.many(10)
    pairs = []
    for index in range(3):
        pair = {'category': str(index) + 'c' * 75, 'subcategory': 's' * 75}
        pairs.append(pair)
        for col, value in enumerate(['Canonical anchor ' + str(index), pair['category'], pair['subcategory']], 1):
            api.google.cells[('Database', 4 + index, col)] = value

    def mostly_ambiguous(body):
        h = context(body)['historical_categories']
        labels = []
        for index, group in enumerate(h['groups']):
            labels.append({'group_ref': group['group_ref'],
                           'status': 'ambiguous' if index else 'proposed',
                           'category': None if index else 'Transportation',
                           'subcategory': None if index else 'Rail',
                           'candidates': pairs if index else []})
        return response('propose_historical_categories', {'review_ref': h['review_ref'], 'proposals': labels})

    result, _ = turn(api, 'จัดหมวดย้อนหลังทั้งหมด', scan, mostly_ambiguous)
    assert result.data.get('tool_result', {}).get('confirmation_available') is False, result
    selected = result.data['tool_result']['groups'][0]['item']
    result, _ = turn(api, 'เอาเฉพาะ ' + selected, review(selection='only', selected=[selected]))
    assert result.data['tool_result']['confirmation_available'] is True
    assert len(result.message.encode('utf-16-le')) <= 9000
    result, _ = turn(api, command(api))
    assert result.status == 'mapping_saved'
    assert [r['item'] for r in result.data['mapping_rows']] == [selected]


def calendar_review(api):
    return turn(api, 'เพิ่มนัดตรวจระบบวันที่ 9 พ.ย. 2026 ทั้งวัน',
                calendar_proposal([{'title': 'ตรวจระบบ', 'date_text': '9 พ.ย. 2026', 'all_day': True}]))


@pytest.mark.parametrize('obstacle', ['undelivered', 'intervening', 'latest_historical', 'delayed', 'same_timestamp'])
def test_waiting_historical_review_does_not_relax_calendar_delivery_or_time_checks(obstacle):
    api = FullHistoricalHosted()
    preview(api)
    delayed = api.event('ยืนยัน') if obstacle == 'delayed' else None
    api.reject_reply = obstacle == 'undelivered'
    result, calendar_event = calendar_review(api)
    assert result.status == 'preview'
    api.reject_reply = False
    if obstacle == 'intervening':
        turn(api, 'ขอบคุณ', response(text='ครับ'))
    elif obstacle == 'latest_historical':
        turn(api, 'ตรวจหมวดย้อนหลังใหม่', review('refresh'), propose)
    before = deepcopy(api.snapshot()['pending'])
    event = delayed or api.event('ยืนยัน')
    if obstacle == 'same_timestamp':
        event['timestamp'] = calendar_event['timestamp']
    result = deliver(api, event)
    assert result.status in {'clarification', 'stale'}, result
    assert api.snapshot()['pending'] == before
    assert api.mapping_posts == api.calendar.posts == api.google.sheet_writes == 0


@pytest.mark.parametrize('historical_pending', [False, True])
def test_actual_expense_and_calendar_drafts_still_require_disambiguation(historical_pending):
    api = FullHistoricalHosted()
    if historical_pending:
        preview(api)

    def expense(body):
        c = context(body)
        return response('prepare_expenses', {'draft_ref': None, 'draft_version': None,
                        'entries': [expense_entry(item='RailPass', amount='17',
                                    source_refs=[c['current_source_ref']])]})

    result, _ = turn(api, 'RailPass 17 บาท', expense)
    assert result.status == 'preview'
    result, _ = calendar_review(api)
    assert result.status == 'preview'
    historical = deepcopy(api.snapshot()['pending'])
    result, _ = turn(api, 'ยืนยัน')
    assert result.status == 'clarification'
    assert api.snapshot()['pending'] == historical
    assert api.mapping_posts == api.calendar.posts == api.google.sheet_writes == 0


def test_generic_cancel_with_latest_calendar_and_waiting_history_preserves_both():
    api = FullHistoricalHosted()
    preview(api)
    calendar_review(api)
    before = api.snapshot()
    result, _ = turn(api, 'ยกเลิก')
    assert result.status == 'clarification'
    after = api.snapshot()
    assert after['pending'] == before['pending']
    assert after['history24']['current']['calendar_v1'] == before['history24']['current']['calendar_v1']
    assert api.mapping_posts == api.calendar.posts == 0


def test_generic_cancel_of_latest_history_keeps_an_older_calendar_draft():
    api = FullHistoricalHosted()
    calendar_review(api)
    preview(api)
    before = deepcopy(api.snapshot()['history24']['current']['calendar_v1'])
    result, _ = turn(api, 'ยกเลิก')
    assert result.status == 'cancelled'
    assert api.snapshot()['pending'] is None
    assert api.snapshot()['history24']['current']['calendar_v1'] == before
    assert api.mapping_posts == api.calendar.posts == 0


@pytest.mark.parametrize('operation', ['scan', 'refresh', 'cancel'])
def test_distinct_event_at_same_timestamp_cannot_reset_historical_review(operation):
    api = HistoricalHosted()
    preview(api)
    result, selected_event = turn(api, 'เอาเฉพาะ Unknown Metro Ticket',
                                 review(selection='only', selected=['Unknown Metro Ticket']))
    assert result.status == 'preview'
    before = deepcopy(api.snapshot()['pending'])
    event = api.event('ยกเลิก' if operation == 'cancel' else 'ตรวจใหม่')
    event['timestamp'] = selected_event['timestamp']
    answers = [scan, propose] if operation == 'scan' else [review('refresh'), propose] if operation == 'refresh' else []
    result = deliver(api, event, *answers)
    assert api.snapshot()['pending'] == before, result
    assert api.mapping_posts == 0


def test_partial_selection_can_escape_oversized_spelling_variants():
    api = HistoricalHosted()
    api.many(30)
    for index in range(10):
        name = 'Synthetic grouped name ' + str(index) + 'x' * 155
        for variant, text in enumerate([name, name.upper(), name.lower()]):
            api.google.cells[('All Expenses', 2 + index * 3 + variant, 2)] = text
    result, _ = preview(api)
    assert result.data['tool_result']['confirmation_available'] is False
    selected = result.data['tool_result']['groups'][0]['item']
    result, _ = turn(api, 'เอาเฉพาะ ' + selected,
                     review(selection='only', selected=[selected]))
    assert result.data['tool_result']['confirmation_available'] is True
    assert len(result.message.encode('utf-16-le')) <= 9000
    result, _ = turn(api, command(api))
    assert result.status == 'mapping_saved'
    assert [r['item'] for r in result.data['mapping_rows']] == [selected]


def test_partial_selection_can_escape_full_width_astral_names():
    api = HistoricalHosted()
    api.many(10)
    for index in range(10):
        api.google.cells[('All Expenses', index + 2, 2)] = str(index) + '🧪' * 195
    result, _ = preview(api)
    assert result.data['tool_result']['confirmation_available'] is False
    selected = result.data['tool_result']['groups'][0]['item']
    result, _ = turn(api, 'เอาเฉพาะ ' + selected,
                     review(selection='only', selected=[selected]))
    assert result.data['tool_result']['confirmation_available'] is True
    assert len(result.message.encode('utf-16-le')) <= 9000
    result, _ = turn(api, command(api))
    assert result.status == 'mapping_saved'
    assert [r['item'] for r in result.data['mapping_rows']] == [selected]
