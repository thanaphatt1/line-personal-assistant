"""Expanded history and compressed storage have distinct, finite bounds."""
from copy import deepcopy
from datetime import timedelta
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from calendar_reschedule_hosted import RescheduleHosted, find_last, move
from test_calendar_created_reschedule import created
from test_calendar_factory import OWNER, values
from test_calendar_mvp import response
from test_calendar_text_confirmation_v13 import mutate, turn
from test_deployment_configuration import TARGET
from test_response_realization import generated
from line_assistant import response_realization as voice
from line_assistant.history_cloud_run import create_history_cloud_run_app
from line_assistant.history_context import HistoryState, digest
from line_assistant.history_persistence import (
    MAX_LOCAL_SNAPSHOT_BYTES, history_snapshot, restore_history,
)
from line_assistant.pilot_http import rpc_budget
from line_assistant.snapshot_codec import MAX_EXPANDED_BYTES, encode
from line_assistant.state import Claim, ModelRun
from line_assistant.transport import OutcomeUnknown


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Injected synthetic services only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def retained_runs(state, now):
    """Completed cache records remain opaque proofs, never new model calls."""
    for i in range(160):
        key = 'retained-model-' + str(i)
        state.claims[key] = Claim(digest(key), now + timedelta(days=30))
        state.model_runs[key] = ModelRun(digest(key), now.strftime('%Y-%m'), 1,
            now + timedelta(days=30), 'completed', {'message': 'synthetic retained result ' * 250})


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


class CapacityHosted(RescheduleHosted):
    def __init__(self):
        self.codec = False
        super().__init__()

    def configuration(self, enabled=True):
        return {**values(enabled), **TARGET,
            'PILOT_STATE_CODEC_ENABLED': str(self.codec).lower(),
            'PILOT_MODEL_USAGE_GATES_ENABLED': 'false',
            'PILOT_RESPONSE_REALIZATION_ENABLED': 'true',
            'PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED': 'true',
            'PILOT_SAVED_EXPENSE_EDITS_ENABLED': 'true',
            'PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY': 'reviewed_single_writer',
            'PILOT_SAVED_EXPENSE_DELETE_ENABLED': 'true',
            'PILOT_CATALOG_ALIASES_ENABLED': 'true',
            'PILOT_RECORD_ACTIONS_ENABLED': 'true',
            'PILOT_LEGACY_SPREADSHEET_ID': 'retained-workbook-00002'}

    def app(self, enabled=True):
        return create_history_cloud_run_app(self.configuration(enabled),
            transport=httpx.MockTransport(self.handle),
            secret_loader=lambda name: SecretStr('synthetic-' + name), clock=lambda: self.now)

    def handle(self, request):
        if request.url.host == 'api.openai.com':
            body = json.loads(request.content)
            if body.get('instructions') == voice.INSTRUCTIONS:
                self.model_bodies.append(deepcopy(body))
                return httpx.Response(200, json=generated(body))
        return super().handle(request)


def seed(api, owner_features=False):
    def put(state):
        retained_runs(state, api.now)
        state.owner_features_used = owner_features
    mutate(api, put)
    value = api.snapshot()
    assert size(value) > MAX_LOCAL_SNAPSHOT_BYTES
    wire_limit = 800000 if api.codec else 200000
    assert len(encode(value, wire_limit, compact=True).encode()) < wire_limit
    return {k: deepcopy(v) for k, v in value['model_runs'].items() if k.startswith('retained-model-')}


@pytest.mark.parametrize('owner_features', [False, True])
@pytest.mark.parametrize('codec', [False, True])
def test_large_history_typo_refresh_cold_confirmation_and_duplicate_deliver_once(owner_features, codec):
    api = CapacityHosted()
    api.codec = codec
    saved = created(api)
    retained = seed(api, owner_features)
    review, _ = turn(api, 'เปลี่ยนเป็นวันที่ 14', find_last, move())
    assert review.status == 'preview' and api.patches == 0
    prior = deepcopy(api.snapshot()['pending']['payload'])
    api.now += timedelta(minutes=1)
    refreshed, _ = turn(api, 'ยืนยะน', response('review_saved_record_change', {}))
    assert refreshed.status == 'preview' and api.patches == 0
    latest = api.snapshot()['pending']['payload']
    assert latest['version'] == prior['version'] + 1 and latest['nonce'] != prior['nonce']
    api.now += timedelta(minutes=12)
    updated, event = turn(api, 'ยืนยัน')
    assert updated.status == 'updated' and updated.data['id'] == saved.data['event_id']
    assert updated.data['after']['start'] == '2026-11-14T16:00:00+07:00'
    assert updated.data['after']['end'] == '2026-11-14T22:00:00+07:00'
    assert api.calendar.posts == 1 and api.patches == 1 and api.google.sheet_writes == 0
    with TestClient(api.app()) as client:
        assert api.send(client, deepcopy(event)).status_code == 200
    after = api.snapshot()
    assert api.patches == 1 and {k: after['model_runs'][k] for k in retained} == retained
    assert after['history24']['current']['format'] == ('local-history-state-v2' if owner_features else 'local-history-state-v1')


def test_large_history_undelivered_refresh_cannot_authorize_a_write():
    api = CapacityHosted()
    created(api)
    seed(api, True)
    turn(api, 'เปลี่ยนเป็นวันที่ 14', find_last, move())
    api.reject_reply = True
    turn(api, 'ยืนยะน', response('review_saved_record_change', {}))
    api.reject_reply = False
    result, _ = turn(api, 'ยืนยัน')
    assert result.status == 'stale' and api.patches == 0 and api.calendar.posts == 1
    review, _ = turn(api, 'ขอดูรายการแก้ไขอีกครั้ง', response('review_saved_record_change', {}))
    assert review.status == 'preview'
    result, _ = turn(api, 'ยืนยัน')
    assert result.status == 'updated' and api.patches == 1 and api.calendar.posts == 1


def test_new_confirmation_claim_crosses_old_limit_without_stranding_pending_review():
    api = CapacityHosted()
    created(api)
    turn(api, 'เปลี่ยนเป็นวันที่ 14', find_last, move())
    app = api.app()
    state = app.state.assistant.state
    with rpc_budget(96), state.session():
        # Simulate a retained cache just below the previous expanded gate.
        # The next ordinary event claim/receipt must fit through the codec.
        key = 'retained-boundary-model'
        state.model_runs[key] = ModelRun(digest(key), api.now.strftime('%Y-%m'), 1,
            api.now + timedelta(days=30), 'completed', {'message': ''})
        view = history_snapshot(state, max_snapshot_bytes=MAX_EXPANDED_BYTES)
        state.model_runs[key].decision['message'] = 'x' * (MAX_LOCAL_SNAPSHOT_BYTES - size(view) - 20)
        assert size(history_snapshot(state, max_snapshot_bytes=MAX_EXPANDED_BYTES)) == MAX_LOCAL_SNAPSHOT_BYTES - 20
        state.checkpoint()
    app.state.pilot_http.close()
    result, _ = turn(api, 'ยืนยัน')
    assert result.status == 'updated' and api.patches == 1 and api.calendar.posts == 1
    after = api.snapshot()
    assert size(after) > MAX_LOCAL_SNAPSHOT_BYTES
    assert after['model_runs'][key]['decision']['message']


def test_large_core_survives_inactive_workspace_validation_and_switch_back():
    api = CapacityHosted()
    created(api)
    retained = seed(api, True)
    app = api.app()
    state = app.state.assistant.state
    with rpc_budget(96), state.session():
        state.select_workbook('retained-workbook-00002', legacy_workbook=TARGET['PILOT_SPREADSHEET_ID'])
        state.checkpoint()
    app.state.pilot_http.close()
    app = api.app()
    state = app.state.assistant.state
    with rpc_budget(96), state.session():
        assert TARGET['PILOT_SPREADSHEET_ID'] in state.history_workspaces
        state.select_workbook(TARGET['PILOT_SPREADSHEET_ID'], legacy_workbook=TARGET['PILOT_SPREADSHEET_ID'])
        assert state.owner_features_used
        assert set(retained) <= set(state.model_runs)
        state.checkpoint()
    app.state.pilot_http.close()
    assert api.calendar.posts == 1 and api.patches == 0


@pytest.mark.parametrize('codec', [False, True])
def test_hosted_codec_flag_selects_wire_limit_independently_of_expanded_history(codec):
    api = CapacityHosted()
    api.codec = codec
    app = api.app()
    state = app.state.assistant.state
    assert state.history_snapshot_bytes == MAX_EXPANDED_BYTES
    assert state.max_snapshot_bytes == (800000 if codec else 200000)
    assert state.compact_snapshots
    app.state.pilot_http.close()


def test_local_bound_is_unchanged_and_explicit_codec_bound_preserves_proofs():
    api = CapacityHosted()
    state = HistoryState()
    state.owner_id = OWNER
    retained_runs(state, api.now)
    with pytest.raises(ValueError, match='capacity'):
        history_snapshot(state)
    value = history_snapshot(state, max_snapshot_bytes=MAX_EXPANDED_BYTES)
    restored = HistoryState()
    with pytest.raises(ValueError, match='capacity'):
        restore_history(restored, deepcopy(value), OWNER)
    assert not restored.model_runs
    restore_history(restored, deepcopy(value), OWNER, max_snapshot_bytes=MAX_EXPANDED_BYTES)
    assert history_snapshot(restored, max_snapshot_bytes=MAX_EXPANDED_BYTES) == value


def test_expanded_history_cap_rejects_oversize_without_erasing_valid_state():
    state = HistoryState()
    state.owner_id = OWNER
    before = history_snapshot(state)
    oversized = deepcopy(before)
    oversized['core']['claims']['synthetic-large-claim'] = {'fingerprint': 'x' * MAX_EXPANDED_BYTES}
    with pytest.raises(ValueError, match='capacity'):
        restore_history(state, oversized, OWNER, max_snapshot_bytes=MAX_EXPANDED_BYTES)
    assert history_snapshot(state) == before
    with pytest.raises(OutcomeUnknown, match='capacity'):
        encode(oversized, 800000, compact=True)


@pytest.mark.parametrize('limit', [0, -1, True, 8_000_001, '800000'])
def test_history_limit_cannot_bypass_codec_expansion_bound(limit):
    state = HistoryState()
    with pytest.raises(ValueError, match='capacity'):
        history_snapshot(state, max_snapshot_bytes=limit)
