"""Slow turns must keep time for durable receipts and one LINE delivery."""
from copy import deepcopy
from datetime import timedelta
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from line_assistant import pilot_http, response_realization as voice
from line_assistant.deployment_plan import launch_plan
from line_assistant.model_api import PLANNER_TURN_SECONDS
from line_assistant.persistence import result_from
from line_assistant.transport import LiveDisabled
from state_records_hosted import RecordsHosted
from test_expense_proposals import group, record, proposal, call
from test_history_first import response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a, **k): raise AssertionError('Synthetic services and clocks only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def test_model_time_reserve_leaves_rpc_capacity_for_checkpoints(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(pilot_http, 'monotonic', lambda: now[0])
    with pilot_http.rpc_budget(4, seconds=50):
        assert pilot_http.remaining_model_seconds() == 40
        now[0] = 47
        assert pilot_http.take_rpc(max_seconds=30, reserve_seconds=10) == 3
        now[0] = 50
        assert pilot_http.remaining_model_seconds() == 0
        with pytest.raises(LiveDisabled): pilot_http.take_rpc(reserve_seconds=10)
        # Model admission failed before consuming a provider call. Persistence
        # remains available in the same original scope, without extending it.
        assert pilot_http.rpc_left()[0] == 3
        assert pilot_http.take_rpc() == 10
        now[0] = 60
        with pytest.raises(LiveDisabled): pilot_http.take_rpc()


def test_unbounded_local_scope_keeps_existing_mock_behavior():
    assert pilot_http.remaining_model_seconds() is None


def test_deployment_timeout_covers_assistant_delivery_and_cold_start():
    plan = launch_plan()
    assert plan['request_timeout_seconds'] >= PLANNER_TURN_SECONDS + 10 + 20
    assert plan['request_timeout_seconds'] == 90
    assert not plan['launch_authorized'] and not plan['execution_enabled']


@pytest.mark.parametrize('seconds', [None, 45, 50, 60, 80, True, '90'])
def test_release_gate_rejects_an_incomplete_or_too_short_request_window(seconds):
    from line_assistant.deployment_plan import check_request_timeout
    manifest = {'spec': {'template': {'spec': {'timeoutSeconds': seconds}}}}
    with pytest.raises(ValueError): check_request_timeout(manifest)


def test_release_gate_accepts_the_exact_explicit_operator_manifest():
    from line_assistant.deployment_plan import check_request_timeout
    manifest = {'spec': {'template': {'spec': {'timeoutSeconds': 90}}}}
    before = deepcopy(manifest)
    assert check_request_timeout(manifest) == {'status': 'passed', 'request_timeout_seconds': 90, 'minimum_seconds': 90}
    assert manifest == before


class SlowTurn(RecordsHosted):
    def __init__(self, *, planner_seconds=(19, 19), reply_seconds=8):
        self.tick = 0.0; self.timed = False
        self.planner_seconds = list(planner_seconds); self.reply_seconds = reply_seconds
        self.model_timeouts = []; self.reply_requests = 0; self.line_requests = 0
        super().__init__()
        for col, value in enumerate(['Metro', 'Transportation', ''], 1):
            self.google.cells[('Database', 5, col)] = value

    def advance(self, seconds):
        self.tick += seconds; self.now += timedelta(seconds=seconds)

    def handle(self, request):
        if self.timed:
            if request.url.host == 'api.openai.com':
                body = json.loads(request.content)
                reply = body.get('instructions') == voice.INSTRUCTIONS
                if reply: self.reply_requests += 1
                seconds = self.reply_seconds if reply else self.planner_seconds.pop(0)
                timeout = request.extensions['timeout']['read']
                self.model_timeouts.append((reply, timeout))
                if seconds >= timeout:
                    self.advance(timeout)
                    raise httpx.ReadTimeout('Synthetic slow model', request=request)
                self.advance(seconds)
            else:
                self.advance(0.1)
                if request.url.host == 'api.line.me' and request.url.path.endswith('/reply'):
                    self.line_requests += 1
        return super().handle(request)


def slow_clock(monkeypatch, api):
    monkeypatch.setattr(pilot_http, 'monotonic', lambda: api.tick)
    monkeypatch.setattr(voice, 'monotonic', lambda: api.tick)
    api.timed = True


def incomplete_list():
    return proposal([group('g', 'Metro', 'Metro 61, 61, 19')],
        [record('g', 61, date='วันก่อน'), record('g', 61, occurrence=1, date='วันก่อน'), record('g', 19, date='วันก่อน')])


def test_slow_repair_keeps_partial_facts_and_sends_a_durable_reply(monkeypatch):
    api = SlowTurn(); slow_clock(monkeypatch, api)
    app = api.app()
    api.queue = [call(incomplete_list()), response(text='ต้องการวันที่ใดครับ')]
    event = api.event('วันก่อน Metro 61, 61, 19')
    with TestClient(app) as client:
        received = api.send(client, event)
    assert received.status_code == 200, received.text
    snap = api.snapshot(); key = event['webhookEventId']
    result = result_from(snap['receipts'][key]['result'])
    assert result.status == 'clarification' and result.message
    assert snap['replies'][key]['status'] == 'sent' and api.line_requests == 1
    assert api.tick < 55
    assert not api.google.sheet_writes and not api.calendar.posts
    rows = next(d for d in snap['history24']['current']['drafts'].values() if d['status'] != 'recorded')['entries']
    assert [(e['item'], e['amount'], e['date']) for e in rows] == [('Metro', '61', None), ('Metro', '61', None), ('Metro', '19', None)]
    model_calls = len(api.model_timeouts)
    # Delivery and cached results remain replay-safe after a cold restore.
    with TestClient(api.app()) as client:
        assert api.send(client, deepcopy(event)).status_code == 200
    assert len(api.model_timeouts) == model_calls and api.line_requests == 1


def test_slow_first_planner_returns_a_timeout_reply_instead_of_losing_the_receipt(monkeypatch):
    api = SlowTurn(planner_seconds=(60,)); slow_clock(monkeypatch, api)
    api.queue = [response(text='synthetic response that must time out')]
    event = api.event('คำถามทดสอบ')
    with TestClient(api.app()) as client:
        received = api.send(client, event)
    assert received.status_code == 200, received.text
    snap = api.snapshot(); key = event['webhookEventId']
    assert snap['replies'][key]['status'] == 'sent'
    result = result_from(snap['receipts'][key]['result'])
    assert result.status == 'model_unavailable' and result.data['model_failure']['kind'] == 'timeout'
    assert api.reply_requests == 0 and api.line_requests == 1
    assert not api.google.sheet_writes and not api.calendar.posts


def test_optional_composition_is_skipped_before_it_can_use_persistence_time(monkeypatch):
    api = SlowTurn(planner_seconds=(1,)); slow_clock(monkeypatch, api)
    app = api.app()
    # Query/receipt storage may have already consumed most of the foreground
    # scope. Optional language generation must not reserve or transmit a call.
    monkeypatch.setattr(pilot_http, 'remaining_rpc_seconds', lambda: 9.0 if api.model_timeouts else 50.0)
    api.queue = [call(proposal([group('g', 'Metro', 'Metro 61')], [record('g', 61)]))]
    event = api.event('Metro 61')
    # Use the actual RPC scope for model transport; the injected remaining
    # time changes only after the valid preparation call has returned.
    with TestClient(app) as client:
        received = api.send(client, event)
    assert received.status_code == 200, received.text
    assert api.reply_requests == 0 and api.line_requests == 1
    assert len(api.model_timeouts) == 1 and not api.google.sheet_writes
    result = result_from(api.snapshot()['receipts'][event['webhookEventId']]['result'])
    assert result.status == 'preview' and result.data['response_realization']['reason'] == 'deadline'


def spend_model_window(monkeypatch, api, app):
    original = app.state.assistant.tool
    def tool(value):
        result = original(value)
        api.advance(max(0, PLANNER_TURN_SECONDS - 10 - api.tick))
        return result
    monkeypatch.setattr(app.state.assistant, 'tool', tool)


def test_no_new_repair_call_after_the_model_window_while_verified_partial_facts_survive(monkeypatch):
    api = SlowTurn(planner_seconds=(1,)); slow_clock(monkeypatch, api)
    app = api.app(); spend_model_window(monkeypatch, api, app)
    api.queue = [call(incomplete_list())]
    event = api.event('วันก่อน Metro 61, 61, 19')
    with TestClient(app) as client:
        received = api.send(client, event)
    assert received.status_code == 200, received.text
    snap = api.snapshot(); key = event['webhookEventId']
    result = result_from(snap['receipts'][key]['result'])
    assert result.status == 'clarification' and result.data['planning_stopped'] == 'deadline'
    assert snap['replies'][key]['status'] == 'sent' and api.line_requests == 1
    assert len(api.model_timeouts) == 1 and api.reply_requests == 0
    assert len(next(d for d in snap['history24']['current']['drafts'].values() if d['status'] != 'recorded')['entries']) == 3
    assert not api.google.sheet_writes


def test_incomplete_cross_source_analysis_never_becomes_a_complete_answer_on_deadline(monkeypatch):
    from test_read_queries import plan
    api = SlowTurn(planner_seconds=(1,)); slow_clock(monkeypatch, api)
    app = api.app(); spend_model_window(monkeypatch, api, app)
    def query(body):
        value=plan('expense', '2026-10-01')(body)
        args=json.loads(value['output'][0]['arguments']);args['continue_analysis']=True
        value['output'][0]['arguments']=json.dumps(args)
        return value
    api.queue = [query]
    event = api.event('เดือนตุลาคมใช้เท่าไหร่')
    with TestClient(app) as client:
        received = api.send(client, event)
    assert received.status_code == 200, received.text
    snap = api.snapshot(); key = event['webhookEventId']
    result = result_from(snap['receipts'][key]['result'])
    assert result.status == 'clarification' and result.data['tool_result']['complete'] is False
    assert result.data['tool_result']['error_code'] == 'planning_deadline'
    assert snap['replies'][key]['status'] == 'sent' and len(api.model_timeouts) == 1
    assert not api.google.sheet_writes
