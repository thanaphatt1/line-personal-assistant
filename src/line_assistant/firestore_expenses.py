"""Owner-scoped expense/catalog REST adapter with atomic operation receipts.

No credential discovery, migration, startup initialization or automatic commit
retry. State, original messages and business documents have separate paths.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import re

from .adapters import Conflict, NotFound, Reconciliation
from .catalog import normalized
from .contracts import AddTransactions, Transaction
from .enrollment import Enrollment
from .expense_edits import expense_after
from .history_context import digest
from .sheet_metadata import transaction_id
from .taxonomy import Taxonomy
from .transport import ApiError, LiveDisabled, NotReady, OutcomeUnknown, WriteBlocked

SCHEMA = 1
PAGE_SIZE = 100


def fields(value):
    result = {}
    for key, item in value.items():
        if type(item) is int:
            result[key] = {'integerValue': str(item)}
        elif isinstance(item, str):
            result[key] = {'stringValue': item}
        else:
            raise ValueError('Only explicit string/integer business fields are supported')
    return result


def unfields(document):
    result = {}
    for key, value in document.get('fields', {}).items():
        if not isinstance(value, dict) or len(value) != 1:
            raise NotReady('Invalid business document field')
        if set(value) == {'stringValue'} and isinstance(value['stringValue'], str):
            result[key] = value['stringValue']
        elif set(value) == {'integerValue'} and re.fullmatch(r'-?[0-9]+', str(value['integerValue'])):
            result[key] = int(value['integerValue'])
        else:
            raise NotReady('Unexpected business document type')
    return result


@dataclass(frozen=True, repr=False)
class FirestoreExpenseTarget:
    project_id: str
    owner_id: str
    dataset_id: str = 'default'
    database_id: str = '(default)'
    max_records: int = 1000
    writes_enabled: bool = False

    def __post_init__(self):
        if (not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', self.project_id)
                or not self.owner_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.dataset_id)
                or self.database_id != '(default)' or type(self.max_records) is not int
                or not 1 <= self.max_records <= 1000 or type(self.writes_enabled) is not bool):
            raise ValueError('Explicit bounded owner/dataset in the existing default database required')

    @property
    def database(self):
        return f'projects/{self.project_id}/databases/{self.database_id}'

    @property
    def owner_hash(self):
        return hashlib.sha256(self.owner_id.encode()).hexdigest()

    @property
    def scope_id(self):
        return 'firestore-expenses-v1_' + digest([self.database, self.owner_hash, self.dataset_id])

    @property
    def parent(self):
        return self.database + '/documents/assistant_expense_owners/' + self.owner_hash + '/ledgers/' + self.dataset_id


class FirestoreExpenseStore:
    backend = 'firestore'

    def __init__(self, client, target, *, state=None, effect_guard=None):
        if client.service != 'firestore' or type(target) is not FirestoreExpenseTarget:
            raise ValueError('Explicit Firestore expense transport/target required')
        if not client.mocked and (state is None or effect_guard != state.before_effect):
            raise LiveDisabled('Live expense transport requires the bound owner state')
        self.client, self.target, self.state = client, target, state
        self.effect_guard = effect_guard or (lambda: None)
        self._permit = None
        self._transactions = set()
        self._tx_leases = {}
        self._attempted = set()
        self._catalog_revision = self._price_revision = None

    @property
    def scope_id(self):
        return self.target.scope_id

    def authorize(self, method, path, body, params):
        """One exact server-generated RPC; no prefix-based commit permission."""
        expected = (method, path, digest(body), digest(params))
        if self._permit != expected:
            return False
        self._permit = None
        self._owned()
        return True

    def _owned(self):
        if self.state is None:
            if not self.client.mocked:
                raise LiveDisabled('Synthetic standalone business adapter required')
            return
        s = self.state
        if (s.owner_id != self.target.owner_id or s.database != self.target.database
                or not s.holder or not s.production_ready or s.workbook_id != self.scope_id):
            raise LiveDisabled('Business RPC is outside the active owner/storage scope')

    def _request(self, method, path, *, body=None, params=None):
        self._owned()
        self._permit = (method, path, digest(body), digest(params))
        try:
            return self.client.request(method, path, body=body, params=params)
        finally:
            self._permit = None

    def _begin(self, *, write=False):
        response = self._request('POST', '/v1/' + self.target.database + '/documents:beginTransaction',
                                 body={'options': {'readWrite' if write else 'readOnly': {}}})
        token = response.get('transaction')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_+/=-]{1,4096}', token):
            raise NotReady('Invalid business transaction token')
        self._transactions.add(token)
        return token

    def _rollback(self, token):
        if token in self._transactions:
            self._transactions.remove(token)
            self._tx_leases.pop(token, None)
            try:
                self._request('POST', '/v1/' + self.target.database + '/documents:rollback', body={'transaction': token})
            except Exception:
                pass  # Never retry a commit or conceal its original outcome.

    def _get(self, name, tx=None):
        if not (name == self.target.parent or name.startswith(self.target.parent + '/')
                or self.state is not None and name == self.state.name):
            raise LiveDisabled('Foreign business document')
        try:
            if tx:
                rows = self._request('POST', '/v1/' + self.target.database + '/documents:batchGet',
                                     body={'documents': [name], 'transaction': tx})
                if not isinstance(rows, list) or len(rows) != 1:
                    raise NotReady('Invalid transactional business document response')
                if rows[0].get('missing') == name:
                    return None
                document = rows[0].get('found', {})
            else:
                document = self._request('GET', '/v1/' + name)
        except ApiError as error:
            if error.status == 404:
                return None
            raise
        if document.get('name') != name or not isinstance(document.get('updateTime'), str):
            raise NotReady('Business document identity/version changed')
        return document

    def _scan(self, collection, tx):
        if collection not in {'expenses', 'catalog'} or tx not in self._transactions:
            raise LiveDisabled('Fixed business collection/transaction required')
        documents, cursor, size = [], None, 0
        while True:
            query = {'from': [{'collectionId': collection}],
                     'orderBy': [{'field': {'fieldPath': '__name__'}, 'direction': 'ASCENDING'}], 'limit': PAGE_SIZE}
            if cursor:
                query['startAt'] = {'values': [{'referenceValue': cursor}], 'before': False}
            page = self._request('POST', '/v1/' + self.target.parent + ':runQuery',
                                 body={'structuredQuery': query, 'transaction': tx})
            if not isinstance(page, list):
                raise NotReady('Invalid business query page')
            rows = [item['document'] for item in page if 'document' in item]
            if len(rows) > PAGE_SIZE:
                raise NotReady('Business query page exceeded its bound')
            prefix = self.target.parent + '/' + collection + '/'
            for document in rows:
                name = document.get('name', '')
                if (not name.startswith(prefix) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', name[len(prefix):])
                        or not document.get('updateTime') or cursor and name <= cursor):
                    raise NotReady('Foreign, duplicate or unordered business query document')
                size += len(json.dumps(document).encode())
                if size > 2_000_000:
                    raise NotReady('Complete business read exceeds byte bound')
                documents.append(document)
                cursor = name
                if len(documents) > self.target.max_records:
                    raise NotReady('Complete business read exceeds configured record bound')
            if len(rows) < PAGE_SIZE:
                return documents

    def _meta(self, tx):
        document = self._get(self.target.parent, tx)
        if document is None:
            raise NotReady('Expense dataset is not initialized; use explicit setup')
        value = unfields(document)
        expected = {'schema_version', 'owner_hash', 'dataset_id', 'revision', 'catalog_revision', 'expense_count', 'catalog_count'}
        if (set(value) != expected or value['schema_version'] != SCHEMA or value['owner_hash'] != self.target.owner_hash
                or value['dataset_id'] != self.target.dataset_id
                or any(type(value[k]) is not int or value[k] < 0 for k in ['revision', 'catalog_revision', 'expense_count', 'catalog_count'])
                or max(value['expense_count'], value['catalog_count']) > self.target.max_records):
            raise NotReady('Business dataset schema/owner/capacity changed')
        return document, value

    def _load(self, tx, *, expenses=True):
        document, meta = self._meta(tx)
        cats = self._scan('catalog', tx)
        rows = self._scan('expenses', tx) if expenses else []
        if len(cats) != meta['catalog_count'] or expenses and len(rows) != meta['expense_count']:
            raise NotReady('Business dataset counts differ; complete read is unproven')
        catalog = []
        for row in cats:
            value = unfields(row)
            if (set(value) != {'schema_version', 'owner_hash', 'item', 'category', 'subcategory'}
                    or value['schema_version'] != SCHEMA or value['owner_hash'] != self.target.owner_hash
                    or row['name'].rsplit('/', 1)[1] != self.catalog_id(value['item'])):
                raise NotReady('Catalog schema/identity changed')
            catalog.append([value[k] for k in ['item', 'category', 'subcategory']])
        taxonomy = Taxonomy(catalog)
        entries = [self._record(row) for row in rows]
        return document, meta, taxonomy, entries

    @staticmethod
    def catalog_id(item):
        if not isinstance(item, str) or not normalized(item):
            raise NotReady('Nonblank catalog item required')
        return hashlib.sha256(normalized(item).encode()).hexdigest()

    def _record(self, document):
        value = unfields(document)
        if (set(value) != {'schema_version', 'owner_hash', 'date', 'item', 'amount_minor', 'currency', 'note', 'action_hash'}
                or value['schema_version'] != SCHEMA or value['owner_hash'] != self.target.owner_hash
                or value['currency'] != 'THB' or type(value['amount_minor']) is not int or not 0 < value['amount_minor'] < 1_000_000_000):
            raise NotReady('Expense schema/owner/amount changed')
        try:
            entry = Transaction.model_validate({'date': value['date'], 'item': value['item'],
                'amount': f"{Decimal(value['amount_minor']) / 100:.2f}", 'note': value['note'], 'kind': 'expense'})
        except Exception:
            raise NotReady('Expense inputs cannot be decoded completely') from None
        if entry.date.isoformat() != value['date']:
            raise NotReady('Canonical expense date required')
        identifier = document['name'].rsplit('/', 1)[1]
        return {'id': identifier, 'date': value['date'], 'item': value['item'], 'amount': entry.amount,
                'note': value['note'], 'version': document['updateTime'], 'workbook': self.scope_id}

    def _snapshot(self, *, expenses=True):
        tx = self._begin()
        try:
            return self._load(tx, expenses=expenses)
        finally:
            self._rollback(tx)

    def taxonomy(self):
        _, meta, tax, _ = self._snapshot(expenses=False)
        self._catalog_revision = meta['catalog_revision']
        return tax

    def catalog(self):
        return self.taxonomy().catalog

    def classification_entries(self):
        _, meta, _, entries = self._snapshot()
        self._price_revision = meta['revision']
        return [{**{k: r[k] for k in ['date', 'item', 'amount', 'note']}, 'transaction_id': r['id']} for r in entries]

    def saved_entries(self, start, end):
        return [{k: r[k] for k in ['date', 'item', 'amount', 'note']}
                for r in self.classification_entries() if start.isoformat() <= r['date'] <= end.isoformat()]

    def summary(self, start, end):
        total = sum((Decimal(r['amount']) for r in self.saved_entries(start, end)), Decimal(0))
        return {'income': None, 'income_supported': False, 'expense': f'{total:.2f}'}

    def _operation_name(self, key):
        return self.target.parent + '/operations/' + digest(key)

    def _receipt(self, key, payload, kind, tx=None):
        document = self._get(self._operation_name(key), tx)
        if document is None:
            return None
        value = unfields(document)
        if (set(value) != {'schema_version', 'owner_hash', 'scope_hash', 'kind', 'payload_hash', 'result'}
                or value['schema_version'] != SCHEMA or value['owner_hash'] != self.target.owner_hash
                or value['scope_hash'] != digest(self.scope_id) or value['kind'] != kind
                or value['payload_hash'] != digest(payload)):
            raise Conflict('Operation key was used with another scope/kind/payload')
        try:
            result = json.loads(value['result'])
            if kind in {'add_transactions', 'catalog_expense'}:
                if (not isinstance(result, list) or result != [transaction_id(key, i) for i in range(len(payload['entries']))]):
                    raise ValueError()
                return tuple(result)
            if not isinstance(result, dict):
                raise ValueError()
            if kind in {'edit_saved_expense','delete_saved_expense'}:
                if (set(result) != {'id', 'before', 'after', 'proof'} or result['id'] != payload['id']
                        or result['before'] != {k: payload['before'][k] for k in ['item', 'amount', 'date']}
                        or result['after'] != payload['after'] or result['proof'] != 'transaction_receipt'):
                    raise ValueError()
            elif kind == 'save_proposed_mapping':
                if result != {'rows': payload['rows'], 'proof': 'transaction_receipt'}:
                    raise ValueError()
            else:
                raise ValueError()
            return result
        except Exception:
            raise NotReady('Invalid operation receipt') from None

    def _action(self, kind, payload, key):
        self._owned()
        if not self.target.writes_enabled:
            raise WriteBlocked('Expense backend writes are disabled')
        if self.state is None:
            return None
        action = self.state.actions.get(key)
        if (not key.startswith(self.target.owner_id + ':') or not action or action.kind != kind
                or action.payload != payload or action.status != 'executing' or action.deadline <= self.state.clock()):
            raise LiveDisabled('Exact executing owner-confirmed business action required')
        config = self.state.client.http.config
        if (kind in {'edit_saved_expense','delete_saved_expense'} and (not config.saved_expense_enabled or config.saved_expense_concurrency != 'firestore_transaction')
                or kind=='delete_saved_expense' and not config.saved_expense_delete_enabled
                or kind == 'save_proposed_mapping' and (not config.provisional_mapping_enabled or not config.provisional_categories_enabled
                    or config.saved_expense_concurrency != 'firestore_transaction'
                    or action.metadata.get('historical_classification') and not config.historical_categories_enabled)
                or action.metadata.get('previous_price_bases') and not config.previous_prices_enabled):
            raise LiveDisabled('Business action feature/concurrency policy is disabled')
        if kind in {'edit_saved_expense','delete_saved_expense', 'save_proposed_mapping'}:
            m = action.metadata
            if (m.get('owner') != self.target.owner_id or m.get('workbook') != self.scope_id
                    or m.get('payload_hash') != digest(payload)):
                raise LiveDisabled('Business action metadata binding changed')
        elif not 1 <= len(payload.get('entries', [])) <= 5:
            raise LiveDisabled('Confirmed business batch exceeds five expenses')
        return action

    def _fence(self, tx):
        if self.state is None:
            return
        document = self._get(self.state.name, tx)
        f = document.get('fields', {}) if document else {}
        try:
            valid = (f['owner_hash']['stringValue'] == self.target.owner_hash
                and f['lease_owner']['stringValue'] == self.state.holder
                and int(f['fence']['integerValue']) == self.state.fence
                and datetime.fromisoformat(f['lease_until']['timestampValue'].replace('Z', '+00:00')) > self.state.clock())
        except Exception:
            valid = False
        if not valid:
            raise Conflict('Business transaction owner lease was replaced or expired')
        self._tx_leases[tx] = datetime.fromisoformat(f['lease_until']['timestampValue'].replace('Z', '+00:00'))

    def _write(self, kind, payload, key):
        action = self._action(kind, payload, key)
        self.effect_guard()  # Checkpoint BEFORE transaction; never checkpoint under its read lock.
        tx = self._begin(write=True)
        try:
            self._fence(tx)
            receipt = self._receipt(key, payload, kind, tx)
            if receipt is not None:
                return receipt
            if key in self._attempted:
                raise OutcomeUnknown('Business commit was already attempted; reconcile only')
            document, meta, tax, records = self._load(tx)
            if self._catalog_revision is not None and self._catalog_revision != meta['catalog_revision']:
                raise Conflict('Catalog changed after the current review/read')
            if action and action.metadata.get('previous_price_bases') and self._price_revision != meta['revision']:
                raise Conflict('Previous-price source changed before commit')
            if action and action.metadata.get('draft_ref'):
                draft = self.state.drafts.get(action.metadata['draft_ref'], {})
                review = draft.get('review') or {}
                if review.get('catalog_hash') != digest([[r.item, r.category, r.subcategory] for r in tax.rows]):
                    raise Conflict('Confirmed catalog changed before commit')
            updates, additions, new_expenses = [], [], [];removed_count=0
            if kind in {'add_transactions', 'catalog_expense'}:
                plan = Enrollment.model_validate(payload).checked() if kind == 'catalog_expense' else AddTransactions.model_validate(payload)
                if kind == 'catalog_expense':
                    pairs = {(r.category, r.subcategory) for r in tax.rows}
                    for row in plan.catalog:
                        found = [r for r in tax.rows if normalized(r.item) == normalized(row.item)]
                        if (row.category, row.subcategory) not in pairs or found and [(r.item,r.category,r.subcategory) for r in found] != [(row.item,row.category,row.subcategory)]:
                            raise Conflict('Enrollment pair/mapping changed')
                        if not found:
                            additions.append({'item': row.item, 'category': row.category, 'subcategory': row.subcategory})
                allowed = {r.item for r in tax.rows} | {r['item'] for r in additions}
                if any(e.kind != 'expense' or e.item not in allowed for e in plan.entries):
                    raise Conflict('Exact canonical expense catalog names required')
                result = tuple(transaction_id(key, i) for i in range(len(plan.entries)))
                for identifier, entry in zip(result, plan.entries):
                    new_expenses.append((identifier, entry))
            elif kind == 'delete_saved_expense':
                current=next((r for r in records if r['id']==payload['id']),None)
                if current is None:raise NotFound('Saved expense is missing')
                if current['version']!=payload['before']['version']:raise Conflict('Saved expense changed after review')
                if payload.get('operation')!='delete' or payload['after'] is not None or payload.get('changes')!={'operation':'delete'}:
                    raise WriteBlocked('Invalid confirmed removal')
                updates.append({'delete':self.target.parent+'/expenses/'+current['id'],
                                'currentDocument':{'updateTime':current['version']}})
                removed_count=1
                result={'id':current['id'],'before':{k:current[k] for k in ['item','amount','date']},
                        'after':None,'proof':'transaction_receipt'}
            elif kind == 'edit_saved_expense':
                current = next((r for r in records if r['id'] == payload['id']), None)
                if current is None:
                    raise NotFound('Saved expense is missing')
                if current['version'] != payload['before']['version']:
                    raise Conflict('Saved expense changed after review')
                after = expense_after(payload['before'], payload['changes'])
                if after != payload['after'] or tax.catalog.match(after['item']).canonical != after['item']:
                    raise Conflict('Saved expense proposal/catalog changed')
                entry = Transaction.model_validate({**after, 'note': current['note'], 'kind': 'expense'})
                updates.append(self._update(self.target.parent + '/expenses/' + current['id'],
                    self.expense_fields(entry, key), {'updateTime': current['version']}))
                result = {'id': current['id'], 'before': {k: current[k] for k in ['item', 'amount', 'date']},
                          'after': after, 'proof': 'transaction_receipt'}
            elif kind == 'save_proposed_mapping':
                if digest([[r.item, r.category, r.subcategory] for r in tax.rows]) != payload['catalog_hash']:
                    raise Conflict('Mapping catalog changed after review')
                # Reuse bounded row validation, without constructing provider coordinates.
                if not isinstance(payload.get('rows'), list) or not 1 <= len(payload['rows']) <= 10:
                    raise ValueError('Bounded mapping rows required')
                pairs = {(r.category, r.subcategory) for r in tax.rows if r.category}
                for row in payload['rows']:
                    if (set(row) != {'item', 'category', 'subcategory'}
                            or any(not isinstance(row[k], str) or len(row[k]) > 200 for k in row)
                            or not row['item'].strip() or (row['category'], row['subcategory']) not in pairs
                            or tax.catalog.identity(row['item']).canonical or tax.catalog.match(row['item']).suggestions):
                        raise Conflict('Mapping requires a fresh explicit unused name/existing pair')
                    additions.append(deepcopy(row))
                result = {'rows': deepcopy(additions), 'proof': 'transaction_receipt'}
            else:
                raise WriteBlocked('Unknown expense operation')
            if (meta['expense_count'] + len(new_expenses) > self.target.max_records
                    or meta['catalog_count'] + len(additions) > self.target.max_records):
                raise WriteBlocked('Explicit expense dataset capacity reached')
            try:
                Taxonomy([[r.item, r.category, r.subcategory] for r in tax.rows]
                         + [[r[k] for k in ['item', 'category', 'subcategory']] for r in additions])
            except ValueError:
                raise Conflict('Proposed catalog names collide') from None
            for identifier, entry in new_expenses:
                updates.append(self._update(self.target.parent + '/expenses/' + identifier,
                    self.expense_fields(entry, key), {'exists': False}))
            for row in additions:
                updates.append(self._update(self.target.parent + '/catalog/' + self.catalog_id(row['item']),
                    {'schema_version': SCHEMA, 'owner_hash': self.target.owner_hash, **row}, {'exists': False}))
            meta.update(revision=meta['revision'] + 1, catalog_revision=meta['catalog_revision'] + bool(additions),
                        expense_count=meta['expense_count'] + len(new_expenses)-removed_count, catalog_count=meta['catalog_count'] + len(additions))
            updates.append(self._update(self.target.parent, meta, {'updateTime': document['updateTime']}))
            updates.append(self._update(self._operation_name(key), {'schema_version': SCHEMA,
                'owner_hash': self.target.owner_hash, 'scope_hash': digest(self.scope_id), 'kind': kind,
                'payload_hash': digest(payload), 'result': json.dumps(result, ensure_ascii=False, sort_keys=True)}, {'exists': False}))
            if len(updates) > 32 or len(json.dumps(updates).encode()) > 200_000:
                raise WriteBlocked('Business transaction exceeds fixed write/byte bound')
            self._action(kind, payload, key)
            if self.state is not None and self._tx_leases.get(tx, self.state.clock()) <= self.state.clock():
                raise Conflict('Owner lease expired while preparing the business transaction')
            self._attempted.add(key)
            try:
                response = self._request('POST', '/v1/' + self.target.database + '/documents:commit',
                                         body={'writes': updates, 'transaction': tx})
            except ApiError as error:
                if error.status in {409, 412}:
                    raise Conflict('Business transaction was rejected; refresh the review') from None
                if error.status in {400, 401, 403, 404}:
                    raise WriteBlocked('Business commit was rejected by schema/access preconditions') from None
                raise OutcomeUnknown('Business commit acknowledgement remains uncertain') from None
            if not isinstance(response.get('writeResults'), list) or len(response['writeResults']) != len(updates) or not response.get('commitTime'):
                raise OutcomeUnknown('Business commit acknowledgement is incomplete')
            self._transactions.discard(tx)
            self._tx_leases.pop(tx, None)
            return result
        except (NotReady, ApiError):
            if key not in self._attempted:
                raise WriteBlocked('Read-only business preconditions did not pass') from None
            raise
        finally:
            self._rollback(tx)

    @staticmethod
    def _update(name, value, condition):
        return {'update': {'name': name, 'fields': fields(value)}, 'currentDocument': condition}

    def expense_fields(self, entry, key):
        return {'schema_version': SCHEMA, 'owner_hash': self.target.owner_hash, 'date': entry.date.isoformat(),
                'item': entry.item, 'amount_minor': int(Decimal(entry.amount) * 100), 'currency': 'THB',
                'note': entry.note, 'action_hash': digest(key)}

    def add(self, entries, key):
        payload = AddTransactions(entries=entries).model_dump(mode='json')
        return self._write('add_transactions', payload, key)

    def enroll_and_add(self, payload, key):
        return self._write('catalog_expense', payload, key)

    def reconcile_kind(self, key, payload, kind):
        result = self._receipt(key, payload, kind)
        return Reconciliation('unknown') if result is None else Reconciliation('committed', result)

    def reconcile(self, key, payload):
        return self.reconcile_kind(key, payload, 'add_transactions')

    def reconcile_enrollment(self, key, payload):
        return self.reconcile_kind(key, payload, 'catalog_expense')


class FirestoreSavedExpenseEditor:
    def __init__(self, store, *, writes_enabled=False,delete_enabled=False):
        self.sheets = store  # Host compatibility; no Sheet coordinates exposed.
        self.writes_enabled = writes_enabled
        self.delete_enabled=delete_enabled

    def records(self, start=None, end=None, *, ids=None):
        if ids is not None and (not 1 <= len(ids) <= 10 or any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', i) for i in ids)):
            raise ValueError('Bounded stable expense IDs required')
        rows = self.sheets._snapshot()[3]
        rows = [r for r in rows if r['id'] in ids] if ids is not None else [r for r in rows if start is not None and end is not None and start.isoformat() <= r['date'] <= end.isoformat()]
        return {'records': rows, 'unidentified_count': 0, 'complete': True}

    def get(self, identifier):
        rows = self.records(ids=[identifier])['records']
        if len(rows) != 1:
            raise NotFound('Saved expense is missing')
        return rows[0]

    def apply(self, payload, key, *, bind_request):
        if not self.writes_enabled:
            raise WriteBlocked('Saved expense editing is disabled')
        kind='delete_saved_expense' if payload.get('operation')=='delete' else 'edit_saved_expense'
        if kind=='delete_saved_expense' and not self.delete_enabled:raise WriteBlocked('Saved expense removal is disabled')
        bind_request({'operation': kind, 'payload_hash': digest(payload)},
                     {'id': payload['id'], 'version': payload['before']['version']})
        return self.sheets._write(kind, payload, key)

    def reconcile(self, key, payload):
        return self.sheets.reconcile_kind(key, payload, 'delete_saved_expense' if payload.get('operation')=='delete' else 'edit_saved_expense')


class FirestoreMappingEditor:
    def __init__(self, store, *, enabled=False):
        self.sheets, self.enabled = store, enabled

    def apply(self, payload, key, *, bind):
        if not self.enabled:
            raise WriteBlocked('Mapping changes are disabled')
        bind({'operation': 'save_proposed_mapping', 'payload_hash': digest(payload)})
        return self.sheets._write('save_proposed_mapping', payload, key)

    def reconcile(self, key, payload):
        return self.sheets.reconcile_kind(key, payload, 'save_proposed_mapping')
