"""Resumable copy under the owner CAS fence; business handlers run afterwards."""
from copy import deepcopy
import hashlib
import hmac
import json

from . import state_record_codec as codec
from .history_context import digest
from .snapshot_codec import encode, decode
from .state_records import FORMAT, MAX_COMMIT_BYTES
from .transport import OutcomeUnknown


def records(snapshot, approved):
    result = {}
    current = snapshot['workbook_id']
    if current not in approved:
        raise OutcomeUnknown('Migration requires a retained active workbook')
    scopes = {**snapshot.get('workspaces', {}), current: snapshot}
    for book, values in scopes.items():
        if book not in approved:
            raise OutcomeUnknown('Foreign migration workspace')
        for kind in ('claims', 'receipts', 'replies', 'actions'):
            for key, payload in values.get(kind, {}).items():
                identity = (codec.group(kind), key)
                row = result.setdefault(identity, {'format': FORMAT, 'group': identity[0],
                    'key': key, 'workbook': book, 'values': {}})
                if row['workbook'] != book:
                    raise OutcomeUnknown('An event cannot migrate into two workbooks')
                row['values'][kind] = deepcopy(payload)
    for key, book in snapshot.get('event_workbooks', {}).items():
        if book not in approved:
            raise OutcomeUnknown('Foreign migration ingress binding')
        row = result.setdefault(('events', key), {'format': FORMAT, 'group': 'events',
            'key': key, 'workbook': book, 'values': {}})
        if row['workbook'] != book:
            raise OutcomeUnknown('Migration ingress binding conflicts with evidence')
        row['values']['event_workbooks'] = book
    for kind in ('model_runs', 'model_budgets'):
        for key, payload in snapshot.get(kind, {}).items():
            book = result.get(('events', key), {}).get('workbook', current)
            group = codec.group(kind)
            result[(group, key)] = {'format': FORMAT, 'group': group,
                'key': key, 'workbook': book, 'values': {kind: deepcopy(payload)}}
    return [result[k] for k in sorted(result)]


def source_fields(store, name, snapshot):
    payload = encode(snapshot, store.state.max_snapshot_bytes, compact=True)
    tag = hmac.new(store.secret.encode(), (name + '\n' + payload).encode(), hashlib.sha256).hexdigest()
    return {'snapshot': {'stringValue': payload}, 'auth_mac': {'stringValue': tag}}


def source_snapshot(store, marker):
    name = store.root + '/migrations/' + marker['source']
    document = store._request('GET', '/v1/' + name)
    try:
        f = document['fields']
        payload = f['snapshot']['stringValue']
        tag = hmac.new(store.secret.encode(), (name + '\n' + payload).encode(), hashlib.sha256).hexdigest()
        if (document['name'] != name or not document.get('updateTime') or set(f) != {'snapshot', 'auth_mac'}
                or not hmac.compare_digest(tag, f['auth_mac']['stringValue'])):
            raise ValueError()
        snapshot = decode(payload, store.state.max_snapshot_bytes)
        if digest(snapshot) != marker['source']:
            raise ValueError()
        return snapshot, document
    except (KeyError, ValueError, TypeError):
        raise OutcomeUnknown('Migration source authentication failed') from None


def advance(store, snapshot, writes):
    state = store.state
    fresh = state._assert_holder()
    fields = state._fields(snapshot, state.holder, state.fence, state.clock())
    # Renew rather than release the lease during each copy checkpoint.
    from datetime import timedelta
    fields['lease_until'] = {'timestampValue': (state.clock() + timedelta(seconds=state.lease_seconds)).isoformat()}
    state.document = store.commit(fresh, fields, writes)


def migrate(store):
    state = store.state
    if store.mode == 'copy':
        original, source = source_snapshot(store, store.marker)
        rows = records(original, state.approved_workbooks)
    else:
        original = state._legacy_snapshot()
        rows = records(original, state.approved_workbooks)
        identifier = digest(original)
        marker = {'format': FORMAT, 'phase': 'copy', 'source': identifier,
                  'cursor': 0, 'count': len(rows),
                  'budget_month': max(original.get('model_budgets', {}), default=None)}
        name = store.root + '/migrations/' + identifier
        write = {'update': {'name': name, 'fields': source_fields(store, name, original)},
                 'currentDocument': {'exists': False}}
        # Preflight every individual record before detaching the inline source.
        for row in rows:
            codec.fields(store.name(row['group'], row['key']), row, store.secret)
        advance(store, {'version': 2, 'owner_id': state.owner_id, 'state_records_v1': marker}, [write])
        store.mode, store.marker = 'copy', marker
        state.record_phase_hook('record_source_detached', 0)
        _, source = source_snapshot(store, marker)
    if len(rows) != store.marker['count']:
        raise OutcomeUnknown('Migration manifest changed')
    cursor = store.marker['cursor']
    while cursor < len(rows):
        writes = []
        next_cursor = cursor
        while next_cursor < len(rows) and len(writes) < 128:
            row = rows[next_cursor]
            name = store.name(row['group'], row['key'])
            candidate = {'update': {'name': name, 'fields': codec.fields(name, row, store.secret)},
                         'currentDocument': {'exists': False}}
            if writes and len(json.dumps({'writes': writes + [candidate]}).encode()) > MAX_COMMIT_BYTES - 15000:
                break
            writes.append(candidate)
            next_cursor += 1
        marker = {**store.marker, 'cursor': next_cursor}
        advance(store, {'version': 2, 'owner_id': state.owner_id, 'state_records_v1': marker}, writes)
        store.marker = marker
        cursor = next_cursor
        state.record_phase_hook('record_copy_checkpoint', cursor)
    marker = {'format': FORMAT, 'phase': 'ready', 'budget_month': store.marker.get('budget_month')}
    # The original memory state is still intact. Its small representation and
    # removal of the authenticated temporary copy commit together.
    store.mode, store.marker = 'ready', marker
    try:
        final = state._record_snapshot()
        advance(store, final, [{'delete': source['name'], 'currentDocument': {'updateTime': source['updateTime']}}])
    except BaseException:
        store.mode = 'copy'
        store.marker = {'format': FORMAT, 'phase': 'copy', 'source': digest(original),
                        'cursor': cursor, 'count': len(rows), 'budget_month': marker['budget_month']}
        raise
    store.reset()
    store.reading = True
    try:
        store.hydrate()
    finally:
        store.reading = False
    state.record_phase_hook('record_migration_completed', cursor)
