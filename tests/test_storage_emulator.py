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


def test_emulator_generic_table_indexed_date_range_exact_decimal_and_pagination():
    from line_assistant.table_storage import FirestoreTableStore,Binding,encode
    from line_assistant.tables import Table,Column,Registry,Query,Predicate,query
    port=EmulatorPort(ENDPOINT)
    try:
        table=Table(id='visits',label='Visits',description='Synthetic emulator table',columns=[
            Column(name='day',label='Day',type='date',required=True),
            Column(name='amount',label='Amount',type='decimal',required=True)])
        storage=FirestoreTableStore(port,Binding(backend='firestore',collection_path='visits_'+uuid.uuid4().hex),
                                   owner_id='synthetic-owner',project_id=PROJECT)
        writes=[]
        for index in range(105):
            writes.append({'update':{'name':storage.collection+f'/row{index:04d}',
                'fields':{'day':encode('2026-10-21'),'amount':encode('0.10')}}})
        writes.append({'update':{'name':storage.collection+'/outside','fields':{'day':encode('2026-10-20'),'amount':encode('99.00')}}})
        port.request('POST','/v1/'+storage.database+'/documents:commit',body={'writes':writes})
        result=query(Registry([table],{'visits':storage}),Query(table_id='visits',
            filters=[Predicate(field='day',op='gte',values=['2026-10-21'])],
            aggregates=[{'op':'sum','field':'amount','alias':'total'}],limit=1,source_refs=['synthetic']))
        assert result['complete'] and result['count']==105 and result['rows']==[{'total':'10.50'}]
    finally:port.client.close()


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


def test_emulator_conditional_delete_and_positive_receipt_after_restart(store):
    identifier=store.add([transaction(note='remove only this record')],'append-delete')[0]
    editor=FirestoreSavedExpenseEditor(store,writes_enabled=True,delete_enabled=True)
    before=editor.get(identifier)
    payload={'domain':'expense','operation':'delete','id':identifier,'before':before,'after':None,'changes':{'operation':'delete'}}
    result=editor.apply(payload,'delete',bind_request=lambda *args:None)
    assert result['after'] is None and not store.classification_entries()
    fresh=FirestoreSavedExpenseEditor(FirestoreExpenseStore(store.client,store.target),writes_enabled=False)
    assert fresh.reconcile('delete',payload).receipt==result
    assert store.summary(date(2026,10,18),date(2026,10,18))['expense']=='0.00'


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


def test_emulator_owner_evidence_migration_cold_replay_reschedule_and_count():
    import hashlib
    from state_records_hosted import RecordsHosted
    from line_assistant import state_record_codec as record_codec
    from line_assistant.snapshot_codec import encode
    from line_assistant.pilot_http import rpc_budget
    from calendar_reschedule_hosted import find_last,move
    from test_calendar_created_reschedule import created

    class EmulatorRecords(RecordsHosted):
        def __init__(self):
            self.emulator=None;self.owner='U'+uuid.uuid4().hex
            super().__init__()
            self.emulator=EmulatorPort(ENDPOINT)
            original=next(d for n,d in self.google.documents.items() if '/assistant_pilot_states/' in n)
            snapshot=decode(original['fields']['snapshot']['stringValue'],800000)
            assert not snapshot['actions'] and not snapshot['claims']
            snapshot['owner_id']=self.owner
            owner_hash=hashlib.sha256(self.owner.encode()).hexdigest()
            self.owner_name='projects/'+PROJECT+'/databases/(default)/documents/assistant_pilot_states/'+owner_hash
            fields={**deepcopy(original['fields']),'owner_hash':{'stringValue':owner_hash},
                    'snapshot':{'stringValue':encode(snapshot,200000,compact=True)}}
            self.emulator.request('POST','/v1/projects/'+PROJECT+'/databases/(default)/documents:commit',
                body={'writes':[{'update':{'name':self.owner_name,'fields':fields},'currentDocument':{'exists':False}}]})
        def configuration(self,enabled=True):
            return {**super().configuration(enabled),'PILOT_PROJECT_ID':PROJECT,
                'PILOT_SERVICE_ACCOUNT':'assistant@'+PROJECT+'.iam.gserviceaccount.com','PILOT_LINE_OWNER_ID':self.owner}
        def event(self,*args,**kwargs):
            value=super().event(*args,**kwargs);value['source']['userId']=self.owner;return value
        def handle(self,request):
            if self.emulator is not None and request.url.host=='firestore.googleapis.com':
                response=self.emulator.client.request(request.method,request.url.path,params=request.url.params,
                    content=request.content,headers={'Content-Type':'application/json'})
                return httpx.Response(response.status_code,json=response.json())
            if request.url.host=='metadata.google.internal' and request.url.path.endswith('/email'):
                return httpx.Response(200,text='assistant@'+PROJECT+'.iam.gserviceaccount.com',headers={'Metadata-Flavor':'Google'})
            return super().handle(request)
        def stored_snapshot(self):
            if self.emulator is None:return super().stored_snapshot()
            return decode(self.emulator.request('GET','/v1/'+self.owner_name)['fields']['snapshot']['stringValue'],200000)
        def snapshot(self):
            value=self.stored_snapshot()
            if value.get('state_records_v1',{}).get('phase')!='ready':return value
            for group in ('events','actions','models','budgets'):
                parent=self.owner_name+'/evidence_sets/v1/groups/'+group
                rows=self.emulator.request('POST','/v1/'+parent+':runQuery',body={'structuredQuery':{'from':[{'collectionId':'records'}]}})
                for result in rows:
                    if 'document' not in result:continue
                    doc=result['document'];record=record_codec.decode(doc,doc['name'],'synthetic-line-channel-secret')
                    for kind,payload in record['values'].items():value[kind][record['key']]=deepcopy(payload)
            return value

    api=EmulatorRecords()
    try:
        saved=created(api)
        review,_=turn(api,'เปลี่ยนเป็นวันที่ 14',find_last,move())
        assert review.status=='preview'
        updated,confirmation=turn(api,'ยืนยัน')
        assert updated.status=='updated' and updated.data['id']==saved.data['event_id']
        with TestClient(api.app()) as client:assert api.send(client,deepcopy(confirmation)).status_code==200
        app=api.app()
        try:
            with rpc_budget(512),app.state.assistant.state.session():
                state=app.state.assistant.state
                assert state.model_record_count()>0
                assert state.record_store.mode=='ready'
        finally:app.state.pilot_http.close()
        assert api.calendar.posts==1 and api.patches==1
        stored=api.stored_snapshot()
        assert stored['version']==2 and not stored['receipts'] and not stored['model_runs']
        from datetime import timedelta
        api.now+=timedelta(days=31)
        app=api.app()
        try:
            with rpc_budget(512),app.state.assistant.state.session():
                assert app.state.assistant.state.model_record_count()==0
        finally:app.state.pilot_http.close()
    finally:api.emulator.client.close()
