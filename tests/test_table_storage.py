"""Both real REST adapters against strict synthetic providers; no live services."""
from copy import deepcopy
import json
from urllib.parse import unquote
import httpx
import pytest

from line_assistant.table_storage import SheetsTableStore, FirestoreTableStore, Binding, encode
from line_assistant.tables import TableError
from line_assistant.transport import RestClient, NotReady, OutcomeUnknown, WriteBlocked
from test_tables import FIXTURE
from line_assistant.table_storage import parse_registry


class SheetServer:
    def __init__(self, table, binding):
        self.table, self.binding = table, binding
        self.rows = [['_id'] + [c.storage_name or c.name for c in table.columns]]
        self.grid_rows = 10
        self.calls, self.writes = [], []
        self.lost = False

    def handle(self, request):
        assert request.url.host == 'sheets.googleapis.com'
        self.calls.append((request.method, unquote(request.url.path)))
        if request.method == 'GET' and '/values/' not in request.url.path:
            return httpx.Response(200, json={'spreadsheetId': self.binding.spreadsheet_id, 'sheets': [
                {'properties': {'sheetId': self.binding.sheet_id, 'title': self.binding.tab,
                                'gridProperties': {'rowCount': self.grid_rows, 'columnCount': len(self.table.columns) + 1}}}]})
        if request.method == 'GET': return httpx.Response(200, json={'values': deepcopy(self.rows)})
        assert request.method == 'POST' and request.url.path.endswith(':batchUpdate')
        body = json.loads(request.content); self.writes.append(body)
        value = body['requests'][0]
        name = next(iter(value)); payload = value[name]
        assert name in {'updateCells', 'appendCells'}
        if name == 'appendCells': index = len(self.rows)
        else: index = payload['range']['startRowIndex']
        cells = [next(iter(c['userEnteredValue'].values())) if c.get('userEnteredValue') else '' for c in payload['rows'][0]['values']]
        while len(self.rows) <= index: self.rows.append([])
        self.rows[index] = cells
        if self.lost: raise httpx.ReadTimeout('synthetic lost acknowledgement', request=request)
        return httpx.Response(200, json={'spreadsheetId': self.binding.spreadsheet_id, 'replies': [{}]})


def sheet():
    r = parse_registry(FIXTURE.read_text())[0]
    server = SheetServer(r.table, r.storage)
    store = SheetsTableStore(RestClient('sheets', httpx.MockTransport(server.handle)), r.storage, owner_id='mock-owner', project_id='mock-project')
    return r.table, store, server


def change(operation='insert', before=None, after=None, identifier='new-row'):
    return {'table_id': 'people', 'operation': operation, 'id': identifier, 'before': before,
            'after': after if after is not None else {'name': 'A', 'phone': '00001', 'status': 'pending'}}


def test_sheets_insert_update_delete_use_ids_versions_and_literal_cells():
    table, store, server = sheet()
    value = change(after={'name': '=IMPORTXML("example")', 'phone': '00001', 'status': 'pending'})
    assert store.apply(table, value, 'op1')['id'] == 'new-row'
    current = store.scan(table)[0]
    assert current['values']['phone'] == '00001'
    assert 'stringValue' in server.writes[0]['requests'][0]['updateCells']['rows'][0]['values'][1]['userEnteredValue']
    updated = change('update', before=current, after={**current['values'], 'name': 'B'})
    store.apply(table, updated, 'op2')
    latest = store.scan(table)[0]
    assert latest['values']['name'] == 'B'
    store.apply(table, change('delete', before=latest), 'op3')
    assert store.scan(table) == [] and len(server.writes) == 3


def test_sheets_lost_insert_acknowledgement_has_positive_id_proof_without_second_write():
    table, store, server = sheet(); server.lost = True
    value = change()
    with pytest.raises(OutcomeUnknown): store.apply(table, value, 'op1')
    assert store.reconcile(table, value, 'op1') == {'id': 'new-row', 'operation': 'insert'}
    assert len(server.writes) == 1
    assert store.reconcile(table, change('update'), 'op2') is None


def test_sheets_changed_row_and_duplicate_id_never_write():
    table, store, server = sheet()
    store.apply(table, change(), 'op1'); before = store.scan(table)[0]
    server.rows[1][1] = 'external change'
    with pytest.raises(TableError, match='record_changed'): store.apply(table, change('update', before=before), 'op2')
    assert len(server.writes) == 1
    server.rows.append(deepcopy(server.rows[1]))
    with pytest.raises(NotReady): store.scan(table)


@pytest.mark.parametrize('fault', ['wrong_header', 'oversize_grid', 'missing_id', 'bad_enum'])
def test_sheets_schema_or_coverage_failure_is_not_an_empty_table(fault):
    table, store, server = sheet()
    if fault == 'wrong_header': server.rows[0][1] = 'changed'
    elif fault == 'oversize_grid': server.grid_rows = 1002
    elif fault == 'missing_id': server.rows.append(['', 'A', '', 'pending'])
    else: server.rows.append(['row', 'A', '', 'unknown'])
    with pytest.raises((NotReady, TableError)): store.scan(table)
    assert not server.writes


def test_sheets_at_end_of_grid_appends_and_integer_boolean_roundtrip():
    r = parse_registry(FIXTURE.read_text())[2]
    binding = Binding(backend='sheets', spreadsheet_id='synthetic-shopping-sheet', tab='Shopping', sheet_id=502, writes_enabled=True, single_writer_reviewed=True)
    server = SheetServer(r.table, binding); server.grid_rows = 1
    store = SheetsTableStore(RestClient('sheets', httpx.MockTransport(server.handle)), binding, owner_id='mock-owner', project_id='mock-project')
    value = change(after={'item': 'Paper', 'quantity': 2, 'purchased': False, 'week': None})
    store.apply(r.table, value, 'op1')
    server.grid_rows = 2
    assert 'appendCells' in server.writes[0]['requests'][0]
    assert store.scan(r.table)[0]['values'] == value['after']


class FirestoreServer:
    def __init__(self):
        self.documents = {}; self.commits = []; self.calls = []; self.lost = False; self.page_size = 100
        self.snapshots = {}; self.counter = 0; self.repeat_cursor = False

    def handle(self, request):
        assert request.url.host == 'firestore.googleapis.com'
        path = request.url.path.removeprefix('/v1/')
        self.calls.append((request.method, path, dict(request.url.params)))
        if path.endswith(':beginTransaction'):
            assert json.loads(request.content) == {'options': {'readOnly': {}}}
            self.counter += 1; token = 'snapshot' + str(self.counter)
            self.snapshots[token] = deepcopy(self.documents)
            return httpx.Response(200, json={'transaction': token})
        if path.endswith(':rollback'):
            assert json.loads(request.content)['transaction'] in self.snapshots
            return httpx.Response(200, json={})
        if path.endswith(':commit'):
            body = json.loads(request.content); self.commits.append(body)
            for write in body['writes']:
                name = write.get('delete') or write['update']['name']
                before = self.documents.get(name); condition = write['currentDocument']
                if condition.get('exists') is False and before or condition.get('updateTime') and (not before or before['updateTime'] != condition['updateTime']):
                    return httpx.Response(409, json={'error': {'status': 'FAILED_PRECONDITION'}})
            for write in body['writes']:
                if 'delete' in write: self.documents.pop(write['delete'])
                else:
                    row = deepcopy(write['update']); row['updateTime'] = f'2026-12-01T00:00:0{len(self.commits)}Z'
                    self.documents[row['name']] = row
            if self.lost: raise httpx.ReadTimeout('synthetic lost commit', request=request)
            return httpx.Response(200, json={'writeResults': [{}, {}], 'commitTime': '2026-12-01T00:00:00Z'})
        assert request.method == 'GET'
        if 'pageSize' in request.url.params:
            token = request.url.params['transaction']; documents = self.snapshots[token]
            selected = [r for name, r in sorted(documents.items()) if name.startswith(path + '/') and '/' not in name[len(path) + 1:]]
            offset = int(request.url.params.get('pageToken', '0'))
            rows = selected[offset:offset + self.page_size]
            result = {'documents': rows}
            if offset + len(rows) < len(selected): result['nextPageToken'] = '1' if self.repeat_cursor else str(offset + len(rows))
            return httpx.Response(200, json=result)
        document = self.documents.get(path)
        return httpx.Response(200, json=deepcopy(document)) if document else httpx.Response(404, json={})


def firestore():
    registration = parse_registry(FIXTURE.read_text())[0]
    server = FirestoreServer()
    store = FirestoreTableStore(RestClient('firestore', httpx.MockTransport(server.handle)),
        Binding(backend='firestore', collection_path='people', writes_enabled=True), owner_id='mock-owner', project_id='mock-project')
    return registration.table, store, server


def test_firestore_atomic_operation_proof_and_conditional_update_delete():
    table, store, server = firestore()
    first = change(); store.apply(table, first, 'op1')
    assert len(server.commits[0]['writes']) == 2
    assert store.reconcile(table, first, 'op1') == {'id': 'new-row', 'operation': 'insert'}
    before = store.scan(table)[0]
    second = change('update', before=before, after={**before['values'], 'name': 'B'})
    store.apply(table, second, 'op2')
    assert server.commits[1]['writes'][0]['currentDocument'] == {'updateTime': before['version']}
    with pytest.raises(TableError): store.apply(table, change('update', before=before), 'op3')
    latest = store.scan(table)[0]; store.apply(table, change('delete', before=latest), 'op4')
    assert store.scan(table) == []


def test_firestore_unknown_commit_has_positive_receipt_and_no_retry():
    table, store, server = firestore(); server.lost = True
    first = change()
    with pytest.raises(OutcomeUnknown): store.apply(table, first, 'op1')
    assert store.reconcile(table, first, 'op1')['id'] == 'new-row' and len(server.commits) == 1
    with pytest.raises(NotReady): store.reconcile(table, change(after={'name': 'B'}), 'op1')


def test_firestore_pagination_uses_same_snapshot_and_detects_repeated_cursor():
    table, store, server = firestore(); server.page_size = 1
    for i in range(3): store.apply(table, change(identifier='r' + str(i)), 'op' + str(i))
    assert len(store.scan(table)) == 3
    transactions = {p['transaction'] for _, _, p in server.calls if 'pageSize' in p}
    assert len(transactions) == 1
    server.repeat_cursor = True
    with pytest.raises(NotReady): store.scan(table)


@pytest.mark.parametrize('backend', ['sheets', 'firestore'])
def test_disabled_write_and_direct_rpc_authorization_fail_closed(backend):
    table, store, server = sheet() if backend == 'sheets' else firestore()
    assert not store.authorize('POST', '/foreign', {}, None)
    store.binding.writes_enabled = False
    with pytest.raises(WriteBlocked): store.apply(table, change(), 'op1')
    assert not (server.writes if backend == 'sheets' else server.commits)
