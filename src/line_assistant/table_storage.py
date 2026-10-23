"""Explicit Sheets/Firestore bindings for the metadata table engine.

No provisioning or credential discovery. Every RPC is an exact one-shot permit
from a bound adapter. Firestore mutations atomically include an operation proof;
Sheets writes require a reviewed single-writer setup and never retry an unknown
mutation. All business values are literal cells, never executable formulas.
"""
from copy import deepcopy
import json
import re
from typing import Literal
from urllib.parse import quote

from pydantic import Field, model_validator
from .tables import Strict, Table, Registry, TableError, validate_values,validate_routing,normalize,predicate
from .history_context import digest
from .transport import ApiError, NotReady, LiveDisabled, OutcomeUnknown, WriteBlocked

MAX_BYTES = 400000


class Binding(Strict):
    backend: Literal['sheets', 'firestore']
    spreadsheet_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{20,128}$')
    tab: str | None = Field(default=None, min_length=1, max_length=100)
    sheet_id: int | None = Field(default=None, ge=0, le=2147483647)
    collection_path: str | None = Field(default=None, max_length=500)
    max_records: int = Field(default=1000, ge=1, le=10000)
    writes_enabled: bool = False
    single_writer_reviewed: bool = False

    @model_validator(mode='after')
    def valid(self):
        if self.backend == 'sheets':
            if (not self.spreadsheet_id or not self.tab or self.sheet_id is None or self.collection_path is not None
                    or self.writes_enabled and not self.single_writer_reviewed):
                raise ValueError('Explicit Sheets identity and writer policy required')
        elif (self.spreadsheet_id is not None or self.tab is not None or self.sheet_id is not None
              or not self.collection_path or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)*', self.collection_path)
              or any(p.startswith('assistant_') for p in self.collection_path.split('/'))):
            raise ValueError('Explicit application collection outside internal namespaces required')
        return self


class Registration(Strict):
    table: Table
    storage: Binding


def parse_registry(value):
    try:
        if not isinstance(value, str) or len(value.encode()) > 100000: raise ValueError()
        def unique(pairs):
            result = dict(pairs)
            if len(result) != len(pairs): raise ValueError()
            return result
        rows = json.loads(value, object_pairs_hook=unique) if value else []
        if not isinstance(rows, list) or len(rows) > 100: raise ValueError()
        registrations = [Registration.model_validate(r) for r in rows]
        ids, targets = set(), set()
        for r in registrations:
            target = (r.storage.backend, r.storage.spreadsheet_id, r.storage.sheet_id, r.storage.collection_path)
            if r.table.id=='expenses' or r.table.id in ids or target in targets: raise ValueError()
            if any(v != 'query' for v in r.table.operations) and not r.storage.writes_enabled: raise ValueError()
            ids.add(r.table.id); targets.add(target)
        if any(c.references and c.references not in ids for r in registrations for c in r.table.columns): raise ValueError()
        validate_routing([r.table for r in registrations])
        return registrations
    except (ValueError, TypeError, KeyError):
        raise ValueError('Invalid table registry configuration') from None


class BoundStore:
    def __init__(self, client, binding, *, owner_id, project_id, state=None):
        if client.service != binding.backend or type(binding) is not Binding:
            raise ValueError('Bound table transport required')
        self.client, self.binding, self.state = client, binding, state
        self.owner_id, self.project_id = owner_id, project_id
        self.scope_id = 'table_' + digest([owner_id, project_id, binding.model_dump()])
        self._permit = None

    def _owned(self):
        if self.state is None:
            if not self.client.mocked: raise LiveDisabled('Bound owner state required')
        elif self.state.owner_id != self.owner_id or not self.client.mocked and (not self.state.holder or not self.state.production_ready):
            raise LiveDisabled('Inactive table owner')

    def authorize(self, method, path, body, params):
        if self._permit != (method, path, digest(body), digest(params)): return False
        self._permit = None
        self._owned()
        return True

    def request(self, method, path, *, body=None, params=None, write=False):
        self._owned()
        if write:
            if not self.binding.writes_enabled: raise WriteBlocked('Table writes disabled')
            if self.state is not None: self.state.before_effect()
        self._permit = (method, path, digest(body), digest(params))
        try: return self.client.request(method, path, body=body, params=params)
        finally: self._permit = None


class SheetsTableStore(BoundStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.base = '/v4/spreadsheets/' + quote(self.binding.spreadsheet_id, safe='')

    def snapshot(self, table):
        from .sheet_metadata import column_letters
        from .sheets_api import tab_range
        metadata = self.request('GET', self.base, params={'fields': 'spreadsheetId,sheets(properties(sheetId,title,gridProperties))'})
        tabs = [r.get('properties', {}) for r in metadata.get('sheets', [])]
        tab = next((r for r in tabs if r.get('sheetId') == self.binding.sheet_id and r.get('title') == self.binding.tab), None)
        columns = len(table.columns) + 1
        if (metadata.get('spreadsheetId') != self.binding.spreadsheet_id or not tab
                or type(tab.get('gridProperties', {}).get('rowCount')) is not int
                or tab['gridProperties']['rowCount'] > self.binding.max_records + 1
                or tab['gridProperties']['rowCount'] < 1
                or tab['gridProperties'].get('columnCount', 0) < columns):
            raise NotReady('Complete table grid unavailable')
        cells = tab_range(self.binding.tab, 'A1:' + column_letters(columns) + str(tab['gridProperties']['rowCount']))
        response = self.request('GET', self.base + '/values/' + quote(cells, safe=''),
                                params={'valueRenderOption': 'UNFORMATTED_VALUE', 'dateTimeRenderOption': 'SERIAL_NUMBER'})
        raw = response.get('values', [])
        if (len(json.dumps(raw, ensure_ascii=False).encode()) > MAX_BYTES or not isinstance(raw, list)
                or not raw or raw[0] != ['_id'] + [c.storage_name or c.name for c in table.columns]
                or len(raw) > tab['gridProperties']['rowCount']):
            raise NotReady('Table header or complete scan changed')
        records, positions = [], {}
        for index, row in enumerate(raw[1:], 1):
            if not isinstance(row, list) or len(row) > columns: raise NotReady('Invalid table row')
            if not any(v is not None and v != '' for v in row): continue
            row = row + [''] * (columns - len(row))
            identifier = row[0]
            if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier) or identifier in positions:
                raise NotReady('Unique stable table record IDs required')
            values = {}
            for column, value in zip(table.columns, row[1:]):
                if value == '' or value is None: value = None
                elif column.type == 'decimal' and type(value) in {float, int}: value = str(value)
                elif column.type == 'date' and type(value) in {int, float}:
                    from .sheet_dates import serial_to_date
                    value = serial_to_date(value).isoformat()
                values[column.name] = value
            records.append({'id': identifier, 'version': digest(row), 'values': validate_values(table, values)})
            positions[identifier] = index
        return records, positions, tab['gridProperties']['rowCount']

    def scan(self, table): return self.snapshot(table)[0]

    def reconcile(self, table, change, key):
        # A matching deterministic insert ID proves this operation. An absent
        # update/delete cannot prove who changed the row: remain uncertain.
        if change['operation'] != 'insert': return None
        row = next((r for r in self.scan(table) if r['id'] == change['id']), None)
        if row and row['values'] == change['after']:
            return {'id': row['id'], 'operation': 'insert'}
        return None

    def apply(self, table, change, key):
        if not self.binding.writes_enabled or not self.binding.single_writer_reviewed:
            raise WriteBlocked('Reviewed table writer required')
        records, positions, grid_rows = self.snapshot(table)
        current = next((r for r in records if r['id'] == change['id']), None)
        operation = change['operation']
        if (operation == 'insert' and current is not None
                or operation != 'insert' and (not current or current['version'] != change['before']['version'])):
            raise TableError('record_changed')
        if operation == 'insert' and len(records) >= self.binding.max_records:
            raise WriteBlocked('Configured grid capacity reached')
        def cell(value):
            if value is None: return {}
            return {'userEnteredValue': {'boolValue': value} if type(value) is bool else {'numberValue': value} if type(value) is int else {'stringValue': str(value)}}
        row = {'values': [{} for _ in range(len(table.columns) + 1)]} if operation == 'delete' else {
            'values': [cell(change['id'])] + [cell(change['after'][c.name]) for c in table.columns]}
        index = next((i for i in range(1, self.binding.max_records + 1) if i not in positions.values()), None) if operation == 'insert' else positions[change['id']]
        if operation == 'insert' and index >= grid_rows:
            request = {'appendCells': {'sheetId': self.binding.sheet_id, 'rows': [row], 'fields': 'userEnteredValue'}}
        else:
            request = {'updateCells': {'range': {'sheetId': self.binding.sheet_id, 'startRowIndex': index,
                       'endRowIndex': index + 1, 'startColumnIndex': 0, 'endColumnIndex': len(table.columns) + 1},
                       'rows': [row], 'fields': 'userEnteredValue'}}
        response = self.request('POST', self.base + ':batchUpdate', body={'requests': [request]}, write=True)
        if response.get('spreadsheetId') != self.binding.spreadsheet_id or response.get('replies') != [{}]:
            raise OutcomeUnknown('Table write acknowledgement unavailable')
        return {'id': change['id'], 'operation': operation}


def encode(value):
    if value is None: return {'nullValue': None}
    if type(value) is bool: return {'booleanValue': value}
    if type(value) is int: return {'integerValue': str(value)}
    if isinstance(value, str): return {'stringValue': value}
    raise NotReady('Unsupported table field')


def decode(value):
    if not isinstance(value, dict) or len(value) != 1: raise NotReady('Invalid table field')
    if set(value) == {'nullValue'}: return None
    if set(value) == {'booleanValue'} and type(value['booleanValue']) is bool: return value['booleanValue']
    if set(value) == {'integerValue'} and re.fullmatch(r'-?[0-9]{1,16}', str(value['integerValue'])): return int(value['integerValue'])
    if set(value) == {'doubleValue'} and type(value['doubleValue']) in {int, float}: return str(value['doubleValue'])
    if set(value) == {'stringValue'} and isinstance(value['stringValue'], str): return value['stringValue']
    raise NotReady('Unsupported table field type')


class FirestoreTableStore(BoundStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.database = f'projects/{self.project_id}/databases/(default)'
        self.collection = self.database + '/documents/' + self.binding.collection_path
        self.proofs = self.database + '/documents/assistant_table_operations/' + digest([self.owner_id, self.collection]) + '/operations'

    def capabilities(self):
        return {'strategy':'indexed_filter_then_local_residual','pushdown':['eq','gt','gte','lt','lte'],
                'range_types':['date','integer'],'decimal_comparison':'local_exact',
                'snapshot':'read_only_transaction','display_limit_is_scan_limit':False}

    def scan_filtered(self, table, filters):
        # One supported predicate needs only a single-field index. Keep ALL
        # predicates, aggregation, projection and sorting in the local executor.
        selected=None
        for f in filters:
            if f.field=='id' or len(f.values)!=1 or f.values[0] is None:continue
            column=table.column(f.field)
            if (f.op=='eq' and column.type in {'text','enum','reference','date','integer','boolean'}
                    or f.op in {'gt','gte','lt','lte'} and column.type in {'date','integer'}):
                selected=(column,f);break
        if selected is None:
            from .read_cache import read
            return read(('table_full_scan',id(self),digest(table.model_dump())),lambda:self.scan(table))
        column,f=selected;wanted=normalize(column,f.values[0])
        response=self.request('POST','/v1/'+self.database+'/documents:beginTransaction',body={'options':{'readOnly':{}}})
        token=response.get('transaction')
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_+/=-]{1,4096}',token):raise NotReady('Invalid table read transaction')
        try:return self._filtered(table,column,f,wanted,token)
        finally:
            try:self.request('POST','/v1/'+self.database+'/documents:rollback',body={'transaction':token})
            except Exception:pass

    def _filtered(self, table, column, f, wanted, token):
        from .action_trace import emit
        parent,collection=self.collection.rsplit('/',1)
        name=column.storage_name or column.name
        field=name if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',name) else '`'+name.replace('\\','\\\\').replace('`','\\`')+'`'
        operation={'eq':'EQUAL','gt':'GREATER_THAN','gte':'GREATER_THAN_OR_EQUAL','lt':'LESS_THAN','lte':'LESS_THAN_OR_EQUAL'}[f.op]
        order=([{'field':{'fieldPath':field},'direction':'ASCENDING'}] if f.op!='eq' else [])
        order+=[{'field':{'fieldPath':'__name__'},'direction':'ASCENDING'}]
        base={'from':[{'collectionId':collection}],
              'where':{'fieldFilter':{'field':{'fieldPath':field},'op':operation,'value':encode(wanted)}},'orderBy':order}
        records=[];seen=set();cursor=None;size=0;previous_key=None
        while True:
            limit=min(100,self.binding.max_records+1-len(records))
            query={**base,'limit':limit}
            if cursor:query['startAt']={'values':cursor,'before':False}
            response=self.request('POST','/v1/'+parent+':runQuery',body={'structuredQuery':query,'transaction':token})
            if not isinstance(response,list) or any(not isinstance(v,dict) or 'error' in v for v in response):raise NotReady('Invalid filtered table page')
            size+=len(json.dumps(response,ensure_ascii=False).encode())
            if size>MAX_BYTES:raise NotReady('Complete filtered snapshot exceeds configured capacity')
            documents=[v['document'] for v in response if 'document' in v]
            if len(documents)>limit:raise NotReady('Filtered page exceeds requested limit')
            for document in documents:
                row=self._record(table,document)
                if row['id'] in seen or not predicate(table,f)(dict(id=row['id'],**row['values'])):raise NotReady('Invalid filtered table cursor or predicate')
                key=(row['values'][column.name],row['id']) if f.op!='eq' else (row['id'],)
                if previous_key is not None and key<=previous_key:raise NotReady('Filtered table cursor did not advance')
                previous_key=key
                seen.add(row['id']);records.append(row)
                if len(records)>self.binding.max_records:raise NotReady('Incomplete filtered table snapshot')
            if len(documents)<limit:
                emit('source_read',backend='firestore',strategy='indexed_filter',record_count=len(records))
                return records
            last=documents[-1]
            cursor=([last['fields'][name]] if f.op!='eq' else [])+[{'referenceValue':last['name']}]

    def _record(self, table, document):
        if not isinstance(document,dict):raise NotReady('Invalid table document')
        name=document.get('name','')
        if not isinstance(name,str):raise NotReady('Invalid table document identity')
        identifier=name.removeprefix(self.collection+'/')
        if (name!=self.collection+'/'+identifier or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identifier)
                or not isinstance(document.get('updateTime'),str) or not document['updateTime']):raise NotReady('Invalid table document identity')
        fields=document.get('fields',{});expected={c.storage_name or c.name for c in table.columns}
        if not isinstance(fields,dict) or set(fields)-expected:raise NotReady('Table document schema changed')
        values={c.name:decode(fields[c.storage_name or c.name]) if (c.storage_name or c.name) in fields else None for c in table.columns}
        return {'id':identifier,'version':document['updateTime'],'values':validate_values(table,values)}

    def scan(self, table):
        response=self.request('POST','/v1/'+self.database+'/documents:beginTransaction',body={'options':{'readOnly':{}}})
        token=response.get('transaction')
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_+/=-]{1,4096}',token):raise NotReady('Invalid table read transaction')
        try:return self._scan(table,token)
        finally:
            try:self.request('POST','/v1/'+self.database+'/documents:rollback',body={'transaction':token})
            except Exception:pass

    def _scan(self, table, token):
        records, seen, cursor, size = [], set(), None, 0
        identifiers=set()
        while True:
            params = {'pageSize': min(100, self.binding.max_records + 1),'transaction':token}
            if cursor: params['pageToken'] = cursor
            response = self.request('GET', '/v1/' + self.collection, params=params)
            documents = response.get('documents', [])
            if not isinstance(documents, list): raise NotReady('Invalid table page')
            size += len(json.dumps(response, ensure_ascii=False).encode())
            if size > MAX_BYTES: raise NotReady('Complete table snapshot exceeds configured capacity')
            for document in documents:
                record=self._record(table,document)
                if record['id'] in identifiers:raise NotReady('Duplicate table document identity')
                identifiers.add(record['id']);records.append(record)
                if len(records) > self.binding.max_records: raise NotReady('Incomplete table snapshot')
            cursor = response.get('nextPageToken')
            if not cursor: return records
            if not isinstance(cursor, str) or cursor in seen or not documents: raise NotReady('Incomplete table pagination')
            seen.add(cursor)

    def reconcile(self, table, change, key):
        name = self.proofs + '/' + key
        try: proof = self.request('GET', '/v1/' + name)
        except ApiError as error:
            if error.status == 404: return None
            raise
        expected = {'payload_hash': encode(digest(change)), 'record_id': encode(change['id']), 'operation': encode(change['operation'])}
        if proof.get('name') != name or proof.get('fields') != expected: raise NotReady('Table operation proof mismatch')
        return {'id': change['id'], 'operation': change['operation']}

    def apply(self, table, change, key):
        name = self.collection + '/' + change['id']
        operation = change['operation']
        condition = {'exists': False} if operation == 'insert' else {'updateTime': change['before']['version']}
        if operation == 'delete': write = {'delete': name, 'currentDocument': condition}
        else: write = {'update': {'name': name, 'fields': {c.storage_name or c.name: encode(change['after'][c.name]) for c in table.columns}},
                      'currentDocument': condition}
        proof = {'update': {'name': self.proofs + '/' + key, 'fields': {
            'payload_hash': encode(digest(change)), 'record_id': encode(change['id']), 'operation': encode(operation)}},
            'currentDocument': {'exists': False}}
        try:
            response = self.request('POST', '/v1/' + self.database + '/documents:commit', body={'writes': [write, proof]}, write=True)
        except ApiError as error:
            if error.status in {404, 409, 412}: raise TableError('record_changed') from None
            raise
        if len(response.get('writeResults', [])) != 2 or not response.get('commitTime'):
            raise OutcomeUnknown('Table commit acknowledgement unavailable')
        return {'id': change['id'], 'operation': operation}


def build_registry(registrations, *, clients, owner_id, project_id, state):
    stores = {}
    for registration in registrations:
        binding = registration.storage
        cls = SheetsTableStore if binding.backend == 'sheets' else FirestoreTableStore
        stores[registration.table.id] = cls(clients[binding.backend], binding, owner_id=owner_id, project_id=project_id, state=state)
    return Registry([r.table for r in registrations], stores)


class LegacyExpenseTableStore:
    """Read view of the existing ledger; its established write workflow stays bound."""
    def __init__(self, expense_store):
        from .expense_storage import storage_scope
        self.expense_store=expense_store
        self.scope_id=storage_scope(expense_store)

    def scan(self, table):
        rows=self.expense_store.classification_entries()
        result=[]
        for index,row in enumerate(rows):
            values={k:row.get(k) for k in ('date','item','amount','note')}
            if values['note']=='':values['note']=None
            result.append({'id':'expense_'+digest([index,row])[:32],'version':digest(row),'values':values})
        return result

    def apply(self,*args):raise WriteBlocked('Use the existing expense proposal workflow')
    def reconcile(self,*args):return None


def include_expenses(registry, expense_store):
    if not registry.tables:return registry
    from .tables import Column
    table=Table(id='expenses',label='รายจ่ายส่วนตัว',description='บันทึกรายจ่ายส่วนตัวเดิม อ่านเป็น table ได้ การเพิ่มหรือแก้ใช้ขั้นตอนเตรียมรายจ่ายเดิมเพื่อรักษาหมวดและการยืนยัน',
        aliases=['รายจ่าย','ค่าใช้จ่าย'],operations=['query'],columns=[
            Column(name='date',label='วันที่',type='date',required=True),Column(name='item',label='รายการ',type='text',required=True),
            Column(name='amount',label='จำนวนเงิน',type='decimal',required=True,unit='THB'),Column(name='note',label='หมายเหตุ',type='text')])
    if table.id in registry.tables:raise ValueError('Existing expense view ID is reserved')
    return Registry([*registry.tables.values(),table],{**registry.stores,table.id:LegacyExpenseTableStore(expense_store)})
