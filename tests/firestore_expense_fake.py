"""Synthetic Firestore transaction REST server, including read/write conflicts."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import base64
import json

import httpx

from line_assistant.firestore_expenses import fields, FirestoreExpenseStore, SCHEMA
from line_assistant.contracts import Transaction


class BusinessFirestore:
    def __init__(self, documents=None):
        self.documents = documents if documents is not None else {}
        self.transactions = {}
        self.sequence = 0
        self.commits = 0
        self.requests = []
        self.before_commit = None
        self.lost_ack = self.before_ack = self.abort = False
        self.corrupt_ack = False

    def stamp(self):
        self.sequence += 1
        return f'2026-10-18T00:00:00.{self.sequence:09d}Z'

    def put(self, name, value):
        self.documents[name] = {'name': name, 'fields': fields(value), 'updateTime': self.stamp()}

    def seed(self, target, catalog, entries=()):
        self.put(target.parent, {'schema_version': SCHEMA, 'owner_hash': target.owner_hash, 'dataset_id': target.dataset_id,
            'revision': 0, 'catalog_revision': 0, 'expense_count': len(entries), 'catalog_count': len(catalog)})
        for row in catalog:
            self.put(target.parent + '/catalog/' + FirestoreExpenseStore.catalog_id(row['item']),
                {'schema_version': SCHEMA, 'owner_hash': target.owner_hash, **row})
        for identifier, row in entries:
            entry = Transaction.model_validate({**row, 'kind': 'expense'})
            self.put(target.parent + '/expenses/' + identifier, {'schema_version': SCHEMA, 'owner_hash': target.owner_hash,
                'date': entry.date.isoformat(), 'item': entry.item, 'amount_minor': int(Decimal(entry.amount) * 100),
                'currency': 'THB', 'note': entry.note, 'action_hash': 'synthetic-bootstrap'})

    def matches(self, request):
        path = request.url.path
        if request.url.host != 'firestore.googleapis.com':
            return False
        body = json.loads(request.content) if request.content else None
        return ('/assistant_expense_owners/' in path or path.endswith((':beginTransaction', ':rollback'))
                or request.url.params.get('transaction') in self.transactions
                or isinstance(body, dict) and (body.get('transaction') in self.transactions
                    or any('/assistant_expense_owners/' in w.get('update', {}).get('name', '') for w in body.get('writes', []))))

    def handle(self, request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path.removeprefix('/v1/')
        body = json.loads(request.content) if request.content else None
        if path.endswith(':beginTransaction'):
            token = base64.b64encode(self.stamp().encode()).decode()
            self.transactions[token] = {'snapshot': deepcopy(self.documents), 'reads': {}, 'queries': {}}
            return httpx.Response(200, json={'transaction': token})
        tx = (body or {}).get('transaction') or request.url.params.get('transaction')
        snapshot = self.transactions[tx]['snapshot'] if tx in self.transactions else self.documents
        if path.endswith(':rollback'):
            self.transactions.pop(tx, None)
            return httpx.Response(200, json={})
        if path.endswith(':batchGet'):
            result=[]
            for name in body['documents']:
                document=snapshot.get(name)
                if tx in self.transactions:
                    self.transactions[tx]['reads'][name]=document.get('updateTime') if document else None
                result.append({'found':deepcopy(document)} if document else {'missing':name})
            return httpx.Response(200,json=result)
        if request.method == 'GET':
            document = snapshot.get(path)
            if tx in self.transactions:
                self.transactions[tx]['reads'][path] = document.get('updateTime') if document else None
            return httpx.Response(200, json=document) if document else httpx.Response(404, json={'error': {'status': 'NOT_FOUND'}})
        if path.endswith(':runQuery'):
            query = body['structuredQuery']; collection = query['from'][0]['collectionId']
            prefix = path.removesuffix(':runQuery') + '/' + collection + '/'
            rows = [deepcopy(v) for k, v in sorted(snapshot.items()) if k.startswith(prefix) and '/' not in k[len(prefix):]]
            if tx in self.transactions:
                self.transactions[tx]['queries'][prefix] = {v['name']: v['updateTime'] for v in rows}
            start = query.get('startAt', {}).get('values', [{}])[0].get('referenceValue')
            if start:
                rows = [r for r in rows if r['name'] > start]
            return httpx.Response(200, json=[{'document': v} for v in rows[:query['limit']]])
        if path.endswith(':commit'):
            if self.before_commit:
                hook, self.before_commit = self.before_commit, None
                hook(self)
            if self.before_ack:
                raise httpx.ReadTimeout('Synthetic no-commit acknowledgement loss', request=request)
            if self.abort:
                return httpx.Response(409, json={'error': {'status': 'ABORTED'}})
            if tx in self.transactions:
                for name, version in self.transactions[tx]['reads'].items():
                    if self.documents.get(name, {}).get('updateTime') != version:
                        return httpx.Response(409, json={'error': {'status': 'ABORTED'}})
                for prefix, expected in self.transactions[tx]['queries'].items():
                    actual = {k: v['updateTime'] for k, v in self.documents.items() if k.startswith(prefix) and '/' not in k[len(prefix):]}
                    if actual != expected:
                        return httpx.Response(409, json={'error': {'status': 'ABORTED'}})
            writes = body['writes']; proposed = deepcopy(self.documents)
            for write in writes:
                value = write['update']; name = value['name']; condition = write['currentDocument']
                current = proposed.get(name)
                if condition == {'exists': False} and current is not None or 'updateTime' in condition and (current or {}).get('updateTime') != condition['updateTime']:
                    return httpx.Response(409, json={'error': {'status': 'FAILED_PRECONDITION'}})
                proposed[name] = {**deepcopy(value), 'updateTime': self.stamp()}
            self.documents.clear(); self.documents.update(proposed); self.commits += 1
            self.transactions.pop(tx, None)
            if self.lost_ack:
                raise httpx.ReadTimeout('Synthetic committed acknowledgement loss', request=request)
            return httpx.Response(200, json={} if self.corrupt_ack else {'writeResults': [{'updateTime': self.stamp()} for _ in writes], 'commitTime': self.stamp()})
        return httpx.Response(400, json={'error': {'status': 'INVALID_ARGUMENT'}})
