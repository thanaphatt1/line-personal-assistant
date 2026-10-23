"""Owner-scoped durable evidence and a materialized, read-through working set.

Iteration covers this session's conversation and unresolved operations. Historical
replay uses exact keys; it does not enumerate a month's completed evidence.
Every changed record commits atomically with the owner lease/version fence.
"""
from collections.abc import MutableMapping
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re

from . import state_record_codec as codec
from .firestore_state import LeaseLost, same_commit_fields
from .history_context import digest
from .transport import ApiError, OutcomeUnknown, LiveDisabled

PERMIT = ContextVar('state_records_request', default=None)
FORMAT = 'owner-state-records-v1'
PAGE_SIZE = 4
MAX_COMMIT_BYTES = 900000


class WorkingRecords(MutableMapping):
    def __init__(self, store, kind):
        self.store, self.kind = store, kind
    def __getitem__(self, key):
        value = self.store.get(self.kind, key)
        if value is None:
            raise KeyError(key)
        return value
    def __setitem__(self, key, value):
        self.store.set(self.kind, key, value)
    def __delitem__(self, key):
        self[key]
        self.store.set(self.kind, key, None)
    def __iter__(self):
        return iter(self.store.keys(self.kind))
    def __len__(self):
        return len(self.store.keys(self.kind))


class StateRecords:
    def __init__(self, state):
        self.state = state
        self.root = state.name + '/evidence_sets/v1'
        self.mode = None
        self.marker = None
        self.reset()

    @property
    def secret(self):
        return self.state.raw_journal.auth_secret

    def reset(self):
        self.entries = {}
        self.reading = False
        self.protected_actions = set()
        self.count_cache = None

    def name(self, group, key):
        if group not in codec.GROUPS or not isinstance(key, str) or not 1 <= len(key) <= 512:
            raise OutcomeUnknown('Invalid state record identity')
        return self.root + '/groups/' + group + '/records/' + digest([key])

    def parent(self, group):
        if group not in codec.GROUPS:
            raise OutcomeUnknown('Invalid state record group')
        return self.root + '/groups/' + group

    def _request(self, method, path, body=None):
        if not self.reading and not self.state.holder:
            raise LiveDisabled('Evidence access requires an owner session')
        token = PERMIT.set((self, method, path, digest(body)))
        try:
            return self.state.client.request(method, path, body=body)
        finally:
            PERMIT.reset(token)

    def authorize(self, method, path, body, params):
        expected = (self, method, path, digest(body))
        if params is not None or PERMIT.get()!=expected or not (self.reading or self.state.holder):return False
        def owned(name):
            return isinstance(name,str) and bool(re.fullmatch(re.escape(self.root)+r'/(?:groups/(?:events|actions|models|budgets)/records|migrations)/[a-f0-9]{64}',name))
        if method=='GET':return body is None and path.startswith('/v1/') and owned(path[4:])
        if method!='POST' or not isinstance(body,dict):return False
        if path=='/v1/'+self.state.database+'/documents:batchGet':
            names=body.get('documents')
            return (set(body)=={'documents'} and isinstance(names,list) and 1<=len(names)<=PAGE_SIZE
                    and len(set(names))==len(names) and all(owned(n) and '/groups/' in n for n in names))
        if path=='/v1/'+self.state.database+'/documents:commit':
            if not self.state.holder or set(body)!={'writes'}:return False
            writes=body['writes']
            if not isinstance(writes,list) or not 1<=len(writes)<=500 or len(json.dumps(body).encode())>MAX_COMMIT_BYTES:return False
            owner=writes[0].get('update',{})
            fields=owner.get('fields',{})
            condition=writes[0].get('currentDocument')
            if (set(writes[0])!={'update','currentDocument'} or owner.get('name')!=self.state.name
                or set(fields)!={'owner_hash','snapshot','lease_owner','fence','lease_until','commit_nonce'}
                or fields.get('owner_hash')!={'stringValue':self.state.owner_hash}
                or fields.get('fence')!={'integerValue':str(self.state.fence)}
                or fields.get('lease_owner',{}).get('stringValue') not in {self.state.holder,''}
                or not isinstance(condition,dict) or set(condition)!={'updateTime'} or not isinstance(condition['updateTime'],str)
                or not isinstance(fields.get('snapshot',{}).get('stringValue'),str)
                or len(fields['snapshot']['stringValue'].encode())>self.state.max_snapshot_bytes):return False
            for write in writes[1:]:
                if set(write) not in ({'update','currentDocument'},{'delete','currentDocument'}):return False
                name=write.get('delete') or write.get('update',{}).get('name')
                condition=write['currentDocument']
                if not owned(name) or not (condition=={'exists':False} or isinstance(condition,dict) and set(condition)=={'updateTime'} and isinstance(condition['updateTime'],str)):return False
            return True
        groups=[g for g in codec.GROUPS if path=='/v1/'+self.parent(g)+':runQuery']
        if groups:
            q=body.get('structuredQuery',{})
            if (set(body)!={'structuredQuery'} or not isinstance(q,dict) or set(q)-{'from','where','orderBy','limit','startAt'}
                or q.get('from')!=[{'collectionId':'records'}] or q.get('limit')!=PAGE_SIZE):return False
            f=q.get('where',{}).get('fieldFilter',{})
            return (f.get('field',{}).get('fieldPath') in {'active','record_ref','retain_until'}
                    and f.get('op') in {'EQUAL','LESS_THAN_OR_EQUAL'})
        if path=='/v1/'+self.parent('models')+':runAggregationQuery':
            q=body.get('structuredAggregationQuery',{})
            return (set(body)=={'structuredAggregationQuery'} and isinstance(q,dict)
                    and q.get('aggregations')==[{'alias':'records','count':{}}]
                    and q.get('structuredQuery',{}).get('from')==[{'collectionId':'records'}]
                    and q.get('structuredQuery',{}).get('where',{}).get('fieldFilter',{}).get('field')=={'fieldPath':'retain_until'})
        return False

    def _accept(self, document, group, key=None):
        name = document.get('name', '')
        if not name.startswith(self.parent(group) + '/records/'):
            raise OutcomeUnknown('Foreign state record')
        row = codec.decode(document, name, self.secret)
        if (set(row) != {'format', 'group', 'key', 'workbook', 'values'} or row['format'] != FORMAT
                or row['group'] != group or self.name(group, row['key']) != name
                or key is not None and row['key'] != key
                or row['workbook'] not in self.state.approved_workbooks
                or not isinstance(row['values'], dict)
                or not row['values'] or set(row['values']) - set(codec.KINDS)
                or any(codec.group(k) != group for k in row['values'])
                or 'event_workbooks' in row['values'] and row['values']['event_workbooks']!=row['workbook']):
            raise OutcomeUnknown('Invalid state record scope/schema')
        identity = (group, row['key'])
        old = self.entries.get(identity)
        if old:
            if old['version'] != document['updateTime'] or old['original']!=codec.encoded(row):
                raise OutcomeUnknown('Evidence changed during an owner session')
            return old
        self.entries[identity] = {'row': row, 'version': document['updateTime'], 'objects': {},
                                  'original': codec.encoded(row)}
        return self.entries[identity]

    def _read(self, group, key):
        identity = (group, key)
        if identity in self.entries:
            return self.entries[identity]
        if not self.reading and not self.state.holder:
            return None
        if group=='events':
            context=[r['event'] for r in self.state.history]
            if key in context:
                nearby=[key]+[k for k in reversed(context) if k!=key and ('events',k) not in self.entries]
                self.batch(group,list(dict.fromkeys(nearby))[:PAGE_SIZE])
                return self.entries[(group,key)]
        try:
            doc = self._request('GET', '/v1/' + self.name(group, key))
        except ApiError as error:
            if error.status != 404:
                raise
            self.entries[identity] = None
            return None
        return self._accept(doc, group, key)

    def batch(self, group, keys):
        keys = [k for k in dict.fromkeys(keys) if (group, k) not in self.entries]
        for start in range(0, len(keys), PAGE_SIZE):
            page = keys[start:start + PAGE_SIZE]
            names = {self.name(group, k): k for k in page}
            rows = self._request('POST', '/v1/' + self.state.database + '/documents:batchGet', {'documents': list(names)})
            if not isinstance(rows, list):
                raise OutcomeUnknown('Invalid evidence batch acknowledgement')
            seen = set()
            for row in rows:
                name = row.get('found', {}).get('name') or row.get('missing')
                if name not in names or name in seen:
                    raise OutcomeUnknown('Unrequested or repeated evidence batch result')
                seen.add(name)
                if 'found' in row:
                    self._accept(row['found'], group, names[name])
                else:
                    self.entries[(group, names[name])] = None
            if seen != set(names):
                raise OutcomeUnknown('Incomplete evidence batch acknowledgement')

    def query(self, group, field, value):
        cursor = None
        seen = set()
        while True:
            q = {'from': [{'collectionId': 'records'}],
                 'where': {'fieldFilter': {'field': {'fieldPath': field}, 'op': 'EQUAL', 'value': value}},
                 'orderBy': [{'field': {'fieldPath': '__name__'}, 'direction': 'ASCENDING'}], 'limit': PAGE_SIZE}
            if cursor:
                q['startAt'] = {'values': [{'referenceValue': cursor}], 'before': False}
            rows = self._request('POST', '/v1/' + self.parent(group) + ':runQuery', {'structuredQuery': q})
            if not isinstance(rows, list):
                raise OutcomeUnknown('Invalid evidence query response')
            docs = [r['document'] for r in rows if 'document' in r]
            for doc in docs:
                if doc.get('name') in seen or cursor and doc.get('name', '') <= cursor:
                    raise OutcomeUnknown('Evidence pagination did not advance')
                seen.add(doc['name'])
                self._accept(doc, group)
            if len(docs) < PAGE_SIZE:
                return
            cursor = docs[-1]['name']

    def install(self):
        for kind in codec.KINDS:
            setattr(self.state, kind, WorkingRecords(self, kind))

    def validation_actions(self,core):
        """Restore validators still require exact Calendar parent proofs."""
        history=core.get('history24',{})
        views=[(core['workbook_id'],core,history.get('current',{}))]
        for book,parts in history.get('workspaces',{}).items():
            views.append((book,core.get('workspaces',{}).get(book,{}),parts))
        refs=[]
        for _,_,parts in views:
            refs.extend(b['action_key'] for b in parts.get('calendar_batches_v1',{}).get('batches',{}).values() if b.get('action_key'))
        self.batch('actions',refs)
        for book,target,parts in views:
            for batch in parts.get('calendar_batches_v1',{}).get('batches',{}).values():
                key=batch.get('action_key')
                if not key:continue
                entry=self.entries.get(('actions',key))
                if not entry or entry['row']['workbook']!=book:
                    raise OutcomeUnknown('Calendar batch parent evidence is missing or foreign')
                target.setdefault('actions',{})[key]=deepcopy(entry['row']['values']['actions'])

    def hydrate(self):
        self.install()
        # Only unfinished business effects need a global blocking view. Model
        # and LINE retries are checked by exact key; their old reservations do
        # not have to be downloaded on every new message.
        self.query('actions', 'active', {'booleanValue': True})
        events = list(dict.fromkeys(r['event'] for r in self.state.history))[-2:]
        if self.state.pending:
            events.append(self.state.pending.origin_event_id)
        self.batch('events', events)
        keys = [c.action_key for c in self.state.claims.values() if c.action_key]
        last=self.marker.get('last_action_keys',{}).get(self.state.workbook_id)
        if last:keys.append(last)
        self.batch('actions', keys)
        children = []
        for action in self.state.actions.values():
            if action.metadata.get('calendar_batch'):
                children.extend(v['key'] for v in action.payload['members'])
                if action.status != 'completed':
                    self.protected_actions.update(v['key'] for v in action.payload['members'])
        self.batch('actions', children)
        month = self.marker.get('budget_month')
        if month:
            self._read('budgets', month)

    def prefetch_receipt_ref(self,ref):
        self.query('events','record_ref',{'stringValue':ref})

    def get(self, kind, key):
        if not isinstance(key, str) or not key:
            return None
        entry = self._read(codec.group(kind), key)
        if not entry:
            return None
        row = entry['row']
        if kind in {'claims', 'receipts', 'replies', 'actions'} and row['workbook'] != self.state.workbook_id:
            return None
        if kind not in entry['objects']:
            raw = row['values'].get(kind)
            entry['objects'][kind] = codec.load(kind, raw) if raw is not None else None
        value = entry['objects'][kind]
        if kind=='event_workbooks' and value is not None:
            _,until=codec.metadata(entry['row'])
            if until<=self.state.clock():
                claim=entry['row']['values'].get('claims') or {}
                action=self._read('actions',claim['action_key']) if claim.get('action_key') else None
                original=action['row']['values'].get('actions') if action else None
                if not original or original['status']=='completed' and datetime.fromisoformat(original['expires_at'])<=self.state.clock():return None
        if value is None or not hasattr(value, 'expires_at') or value.expires_at > self.state.clock():
            return value
        if kind == 'actions' and (value.status != 'completed' or key in self.protected_actions):
            return value
        if kind == 'model_runs' and value.status != 'completed':
            return value
        if kind == 'replies' and value.status in {'executing', 'uncertain'}:
            return value
        if kind == 'claims' and value.action_key and self.get('actions', value.action_key):
            return value
        return None

    def set(self, kind, key, value):
        if not self.state.holder:
            raise LiveDisabled('Evidence changes require the owner lease')
        group = codec.group(kind)
        entry = self._read(group, key)
        if entry is None:
            entry = {'row': {'format': FORMAT, 'group': group, 'key': key,
                            'workbook': self.state.workbook_id, 'values': {}},
                     'version': None, 'objects': {}, 'original': None}
            self.entries[(group, key)] = entry
        if kind in codec.EVENT_KINDS | {'actions'} and entry['row']['workbook'] != self.state.workbook_id:
            raise LiveDisabled('Existing evidence belongs to another workbook')
        if kind == 'event_workbooks' and value != self.state.workbook_id:
            raise LiveDisabled('Event binding cannot move to another workbook')
        entry['objects'][kind] = value

    def keys(self, kind):
        group = codec.group(kind)
        return [key for (g, key), entry in list(self.entries.items())
                if g == group and entry is not None and self.get(kind, key) is not None]

    def planned(self):
        result = []
        for (group, key), entry in self.entries.items():
            if entry is None:
                continue
            row = deepcopy(entry['row'])
            for kind, value in entry['objects'].items():
                if value is None:
                    row['values'].pop(kind, None)
                else:
                    row['values'][kind] = codec.dump(kind, value)
            if codec.encoded(row) == entry['original']:
                continue
            if not row['values']:
                raise OutcomeUnknown('Evidence cannot be erased by a working-set eviction')
            name = self.name(group, key)
            write = {'update': {'name': name, 'fields': codec.fields(name, row, self.secret)},
                     'currentDocument': {'updateTime': entry['version']} if entry['version'] else {'exists': False}}
            result.append((entry, row, write))
        return result

    def commit(self, previous, fields, extra=None):
        condition = {'updateTime': previous['updateTime']} if previous else {'exists': False}
        plans = self.planned() if self.mode == 'ready' and extra is None else []
        writes = [{'update': {'name': self.state.name, 'fields': fields}, 'currentDocument': condition}]
        writes += [p[2] for p in plans] if extra is None else extra
        body = {'writes': writes}
        if len(writes) > 500 or len(json.dumps(body).encode()) > MAX_COMMIT_BYTES:
            raise OutcomeUnknown('Single atomic evidence checkpoint exceeds capacity')
        try:
            response = self._request('POST', '/v1/' + self.state.database + '/documents:commit', body)
            at = response['writeResults'][0]['updateTime']
        except ApiError as error:
            if error.status in {409, 412} or error.code in {'FAILED_PRECONDITION', 'ABORTED', 'ALREADY_EXISTS'}:
                raise LeaseLost('Conditional evidence checkpoint rejected') from None
            raise
        except (OutcomeUnknown, KeyError, IndexError, TypeError):
            fresh = self.state._read()
            if not fresh or not same_commit_fields(fresh['fields'], fields):
                raise OutcomeUnknown('Evidence checkpoint outcome is unverified') from None
            at = fresh['updateTime']
        for entry, row, _ in plans:
            if self.count_cache is not None and row['group'] == 'models' and entry['version'] is None:
                self.count_cache += 1
            entry.update(row=deepcopy(row), version=at, original=codec.encoded(row))
        from .snapshot_codec import decode
        marker=decode(fields['snapshot']['stringValue'],self.state.max_snapshot_bytes).get('state_records_v1')
        if marker is not None:
            self.marker=deepcopy(marker);self.mode=marker['phase']
        return {'name': self.state.name, 'fields': fields, 'updateTime': at}

    def count_models(self):
        if self.count_cache is None:
            query = {'from': [{'collectionId': 'records'}],
                'where': {'fieldFilter': {'field': {'fieldPath': 'retain_until'}, 'op': 'GREATER_THAN',
                          'value': {'timestampValue': self.state.clock().isoformat()}}}}
            rows = self._request('POST', '/v1/' + self.parent('models') + ':runAggregationQuery',
                {'structuredAggregationQuery': {'structuredQuery': query,
                    'aggregations': [{'alias': 'records', 'count': {}}]}})
            try:
                values = [r['result']['aggregateFields']['records']['integerValue'] for r in rows if 'result' in r]
                if len(values) != 1 or not str(values[0]).isdigit():
                    raise ValueError()
                self.count_cache = int(values[0])
            except (KeyError, TypeError, ValueError):
                raise OutcomeUnknown('Invalid model evidence count') from None
        return self.count_cache


def authorized_response(http, url):
    permit = PERMIT.get()
    return bool(permit and permit[0].state.client.http is http and
                url.startswith('https://firestore.googleapis.com/'))
