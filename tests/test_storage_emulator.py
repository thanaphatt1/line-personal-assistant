"""Optional official emulator tests; run with scripts/run_firestore_emulator_tests.py.

Explicit loopback/demo project only. No SDK, credentials, discovery or live URL.
"""
from copy import deepcopy
from datetime import date
import json
import os
import re
import socket
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from line_assistant.contracts import Transaction
from line_assistant.expense_initialization import FirestoreInitializer
from line_assistant.firestore_expenses import FirestoreExpenseStore, FirestoreExpenseTarget, FirestoreSavedExpenseEditor
from line_assistant.snapshot_codec import decode
from line_assistant.adapters import Conflict
from line_assistant.transport import ApiError, OutcomeUnknown
from expense_storage_hosted import StorageHosted
from test_expense_storage import CATALOG, transaction
from test_previous_expense_prices import offer, accepted, turn
from test_calendar_all_day_v15 import proposal, event

PROJECT = 'demo-expense-storage'
ENDPOINT = os.environ.get('ASSISTANT_FIRESTORE_EMULATOR', '')
pytestmark = pytest.mark.skipif(not ENDPOINT, reason='explicit loopback Firestore emulator is not running')


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Override the offline fixture ONLY for this one explicit loopback port."""
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', ENDPOINT):
        raise ValueError('Explicit loopback emulator required')
    address = ('127.0.0.1', int(ENDPOINT.rsplit(':', 1)[1]))
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def guarded(original):
        def call(sock, target):
            if target != address:
                raise AssertionError('Emulator tests cannot connect outside their explicit loopback port')
            return original(sock, target)
        return call
    monkeypatch.setattr(socket.socket, 'connect', guarded(connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))


class EmulatorPort:
    service = 'firestore'
    mocked = True  # Synthetic loopback only; never used by production factory.

    def __init__(self, endpoint):
        if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', endpoint):
            raise ValueError('Explicit numeric loopback emulator endpoint required')
        self.client = httpx.Client(base_url=endpoint, timeout=12, follow_redirects=False, trust_env=False)

    def request(self, method, path, *, body=None, params=None):
        if not path.startswith('/v1/projects/' + PROJECT + '/databases/(default)/'):
            raise ValueError('Synthetic demo database only')
        try:
            response = self.client.request(method, path, json=body, params=params)
        except httpx.TransportError:
            raise OutcomeUnknown('Emulator acknowledgement unavailable') from None
        if not response.is_success:
            raise ApiError(response.status_code)
        return response.json()


@pytest.fixture
def store():
    port = EmulatorPort(ENDPOINT)
    target = FirestoreExpenseTarget(PROJECT, 'synthetic-owner', uuid.uuid4().hex, writes_enabled=True)
    initializer = FirestoreInitializer(port, target)
    initializer.apply(initializer.preview(CATALOG), exclusive_setup=True)
    yield FirestoreExpenseStore(port, target)
    port.client.close()


def test_emulator_bootstrap_append_receipt_recreation_and_complete_read(store):
    key = 'emulator-action'; entries = [transaction(amount='0.10'), transaction(amount='0.20')]
    receipt = store.add(entries, key)
    fresh = FirestoreExpenseStore(store.client, store.target)
    assert fresh.add(entries, key) == receipt
    assert fresh.reconcile(key, {'entries': [e.model_dump(mode='json') for e in entries]}).receipt == receipt
    assert fresh.summary(date(2026, 10, 18), date(2026, 10, 18))['expense'] == '0.30'


def test_emulator_saved_edit_provider_update_time_and_stale_review(store):
    identifier = store.add([transaction(note='preserve')], 'append')[0]
    editor = FirestoreSavedExpenseEditor(store, writes_enabled=True); before = editor.get(identifier)
    payload = {'domain': 'expense', 'id': identifier, 'before': before, 'changes': {'amount': '42.00'},
               'after': {'item': before['item'], 'date': before['date'], 'amount': '42.00'}}
    result = editor.apply(payload, 'edit', bind_request=lambda *args: None)
    assert result['after']['amount'] == '42.00' and editor.get(identifier)['note'] == 'preserve'
    with pytest.raises(Conflict):editor.apply(payload, 'new-stale-review', bind_request=lambda *args: None)


def test_emulator_atomic_enrollment_and_duplicate_action(store):
    payload = {'entries': [transaction('Metro pass').model_dump(mode='json')],
               'catalog': [{'item': 'Metro pass', 'category': 'Transportation', 'subcategory': 'Rail'}]}
    receipt = store.enroll_and_add(payload, 'enrollment')
    assert store.enroll_and_add(payload, 'enrollment') == receipt
    assert len(store.classification_entries()) == 1 and store.catalog().identity('Metro pass').canonical == 'Metro pass'


def test_emulator_paginates_over_one_hundred_documents_at_one_snapshot(store):
    for start in range(0, 105, 5):
        store.add([transaction(amount='1') for _ in range(5)], 'page-' + str(start))
    assert len(store.classification_entries()) == 105
    assert store.summary(date(2026, 1, 1), date(2026, 12, 31))['expense'] == '105.00'


def test_emulator_separate_owner_and_dataset_do_not_share_records(store):
    store.add([transaction()], 'same-key')
    target = FirestoreExpenseTarget(PROJECT, 'different-owner', uuid.uuid4().hex, writes_enabled=True)
    initializer = FirestoreInitializer(store.client, target); initializer.apply(initializer.preview(CATALOG), exclusive_setup=True)
    other = FirestoreExpenseStore(store.client, target)
    assert other.classification_entries() == [] and other.reconcile('same-key', {'entries': [transaction().model_dump(mode='json')]}).outcome == 'unknown'


class EmulatorHosted(StorageHosted):
    def __init__(self):
        self.emulator = None
        super().__init__('firestore')
        self.emulator = EmulatorPort(ENDPOINT)
        # Seed wholly synthetic business and workflow documents; no real owner state.
        writes = [{'update': {'name': doc['name'].replace('projects/configured-project/', 'projects/' + PROJECT + '/'),
                              'fields': doc['fields']}, 'currentDocument': {'exists': False}}
                  for doc in self.google.documents.values()]
        self.emulator.request('POST', '/v1/projects/' + PROJECT + '/databases/(default)/documents:commit', body={'writes': writes})

    def configuration(self, enabled=True):
        return {**super().configuration(enabled), 'PILOT_PROJECT_ID': PROJECT,
            'PILOT_SERVICE_ACCOUNT': 'assistant@' + PROJECT + '.iam.gserviceaccount.com'}

    def handle(self, request):
        if self.emulator is not None and request.url.host == 'firestore.googleapis.com':
            response = self.emulator.client.request(request.method, request.url.path, params=request.url.params,
                                                    content=request.content, headers={'Content-Type': 'application/json'})
            return httpx.Response(response.status_code, json=response.json())
        if request.url.host == 'metadata.google.internal' and request.url.path.endswith('/email'):
            return httpx.Response(200, text='assistant@' + PROJECT + '.iam.gserviceaccount.com', headers={'Metadata-Flavor': 'Google'})
        return super().handle(request)

    def snapshot(self):
        import hashlib
        from test_calendar_factory import OWNER
        name = '/v1/projects/' + PROJECT + '/databases/(default)/documents/assistant_pilot_states/' + hashlib.sha256(OWNER.encode()).hexdigest()
        return decode(self.emulator.request('GET', name)['fields']['snapshot']['stringValue'], 800000)


def test_emulator_signed_cold_price_confirmation_replay_and_calendar():
    api = EmulatorHosted()
    try:
        assert offer(api)[0].status == 'clarification'
        assert accepted(api)[0].status == 'preview'
        saved, confirmation = turn(api, 'ยืนยัน'); assert saved.status == 'recorded', saved
        with TestClient(api.app()) as web:assert api.send(web, deepcopy(confirmation)).status_code == 200
        before = len(api.model_bodies)
        result, _ = turn(api, 'เพิ่มนัดนิทรรศการทดสอบ 22 ตุลาคม 2026 ทั้งวัน', proposal([event(day='22 ตุลาคม 2026')]))
        assert result.status == 'preview', result
        assert turn(api, 'ยืนยัน')[0].status == 'created'
        assert api.calendar.posts == 1 and api.google.sheet_writes == 0
    finally:
        api.emulator.client.close()
