"""Signed production factory plus an atomic synthetic Firestore protocol fake."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import unquote

import httpx

from line_assistant import state_record_codec as codec
from test_history_snapshot_capacity import CapacityHosted


class RecordsProtocol:
    def __init__(self,*args,**kwargs):
        self.records_enabled = True
        self.evidence_requests = []
        self.record_commit_fault = None
        super().__init__(*args,**kwargs)

    def configuration(self, enabled=True):
        return {**super().configuration(enabled),
                'PILOT_STATE_RECORDS_ENABLED': str(self.records_enabled).lower()}

    def stored_snapshot(self):
        return super().snapshot()

    def snapshot(self):
        value = self.stored_snapshot()
        marker = value.get('state_records_v1', {})
        if marker.get('phase') != 'ready':
            return value
        value = deepcopy(value)
        for doc in self.google.documents.values():
            name = doc['name']
            if '/evidence_sets/v1/groups/' not in name:
                continue
            row = codec.decode(doc, name, 'synthetic-line-channel-secret')
            for kind, record in row['values'].items():
                if kind in {'claims', 'receipts', 'replies', 'actions'} and row['workbook'] != value['workbook_id']:
                    continue
                value[kind][row['key']] = deepcopy(record)
        return value

    def handle(self, request):
        if request.url.host != 'firestore.googleapis.com':
            return super().handle(request)
        path = unquote(request.url.path.removeprefix('/v1/'))
        if '/evidence_sets/v1/' in path or path.endswith(':batchGet'):
            self.evidence_requests.append((request.method, path, json.loads(request.content) if request.content else None))
        if request.method == 'POST' and path.endswith(':batchGet'):
            names = json.loads(request.content)['documents']
            if all('/evidence_sets/v1/' in n for n in names):
                return httpx.Response(200, json=[{'found': deepcopy(self.google.documents[n])} if n in self.google.documents
                                               else {'missing': n} for n in reversed(names)])
        if request.method == 'POST' and '/evidence_sets/v1/' in path and path.endswith((':runQuery', ':runAggregationQuery')):
            body = json.loads(request.content)
            aggregate = path.endswith(':runAggregationQuery')
            q = body['structuredAggregationQuery']['structuredQuery'] if aggregate else body['structuredQuery']
            parent = path.rsplit(':', 1)[0]
            docs = [deepcopy(v) for n, v in self.google.documents.items() if n.startswith(parent + '/records/')]
            f = q['where']['fieldFilter']
            key = f['field']['fieldPath']
            def matches(doc):
                got = doc['fields'][key]
                if f['op'] == 'EQUAL':
                    return got == f['value']
                left=datetime.fromisoformat(got['timestampValue'].replace('Z','+00:00'))
                right=datetime.fromisoformat(f['value']['timestampValue'])
                return left>right if f['op']=='GREATER_THAN' else left<=right
            def order(doc):
                return (datetime.fromisoformat(doc['fields']['retain_until']['timestampValue'].replace('Z','+00:00')),doc['name']) if q.get('orderBy',[{}])[0].get('field',{}).get('fieldPath')=='retain_until' else (doc['name'],)
            docs = sorted((d for d in docs if matches(d)), key=order)
            if aggregate:
                return httpx.Response(200, json=[{'result': {'aggregateFields': {'records': {'integerValue': str(len(docs))}}}}])
            if q.get('startAt'):
                values=q['startAt']['values']
                cursor=((datetime.fromisoformat(values[0]['timestampValue'].replace('Z','+00:00')),values[1]['referenceValue'])
                        if len(values)==2 else (values[0]['referenceValue'],))
                docs=[d for d in docs if order(d)>cursor]
            return httpx.Response(200, json=[{'document': d} for d in docs[:q['limit']]])
        if request.method == 'POST' and path.endswith('/documents:commit'):
            writes = json.loads(request.content)['writes']
            if (len(writes)>1 and '/assistant_pilot_states/' in writes[0].get('update',{}).get('name','')
                or any('/evidence_sets/v1/' in w.get('update', {}).get('name', w.get('delete', '')) for w in writes)):
                self.evidence_requests.append(('COMMIT', path, deepcopy(writes)))
                if self.record_commit_fault == 'before':
                    self.record_commit_fault = None
                    raise httpx.ReadTimeout('Synthetic evidence acknowledgement unavailable before commit', request=request)
                for write in writes:
                    name = write.get('delete') or write['update']['name']
                    previous = self.google.documents.get(name)
                    condition = write['currentDocument']
                    if (condition == {'exists': False} and previous or 'updateTime' in condition and
                            (not previous or previous['updateTime'] != condition['updateTime'])):
                        return httpx.Response(409, json={'error': {'status': 'ABORTED'}})
                self.google.version += 1
                at = (datetime(2026, 10, 11, tzinfo=timezone.utc) + timedelta(microseconds=self.google.version)).isoformat()
                for write in writes:
                    if 'delete' in write:
                        self.google.documents.pop(write['delete'])
                    else:
                        self.google.documents[write['update']['name']] = {**deepcopy(write['update']), 'updateTime': at}
                if self.record_commit_fault == 'after':
                    self.record_commit_fault = None
                    raise httpx.ReadTimeout('Synthetic evidence acknowledgement unavailable after atomic commit', request=request)
                return httpx.Response(200, json={'writeResults': [{'updateTime': at} for _ in writes], 'commitTime': at})
        return super().handle(request)


class RecordsHosted(RecordsProtocol,CapacityHosted):pass
