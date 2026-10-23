"""Versioned, owner-authenticated records; no aggregate history-size bound."""
import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import hmac
import json
import zlib

from .persistence import compact_record, jsonable, restore_workspace
from .snapshot_codec import MAX_EXPANDED_BYTES
from .state import MemoryState, ModelBudget, ModelRun
from .transport import OutcomeUnknown

KINDS = ('claims', 'receipts', 'replies', 'actions', 'model_runs', 'model_budgets', 'event_workbooks')
EVENT_KINDS = frozenset(('claims', 'receipts', 'replies', 'event_workbooks'))
GROUPS = ('events', 'actions', 'models', 'budgets')
FOREVER = datetime(9999, 1, 1, tzinfo=timezone.utc)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_RECORD_BYTES = 200000


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def group(kind):
    return 'events' if kind in EVENT_KINDS else {'actions': 'actions', 'model_runs': 'models', 'model_budgets': 'budgets'}[kind]


def dump(kind, value):
    if kind == 'event_workbooks':
        return value
    row = jsonable(asdict(value))
    return compact_record(row) if kind in {'receipts', 'actions'} else row


def load(kind, value):
    value = deepcopy(value)
    if kind == 'event_workbooks':
        return value
    if kind == 'model_runs':
        return ModelRun(**{**value, 'expires_at': datetime.fromisoformat(value['expires_at'])})
    if kind == 'model_budgets':
        return ModelBudget(**value)
    state = MemoryState()
    row = {'pending': None, 'last': None, 'recent_turns': [],
           **{name: {} for name in ('claims', 'receipts', 'replies', 'actions')}}
    row[kind] = {'record': value}
    restore_workspace(state, row)
    return getattr(state, kind)['record']


def metadata(record):
    rows = record['values']
    active = any((k == 'actions' and v['status'] != 'completed')
                 or (k == 'model_runs' and v['status'] != 'completed')
                 or (k == 'replies' and v['status'] in {'executing', 'uncertain'})
                 for k, v in rows.items() if k != 'event_workbooks')
    expiry = max((datetime.fromisoformat(v['expires_at']) for k, v in rows.items()
                  if isinstance(v, dict) and 'expires_at' in v), default=FOREVER)
    return active, FOREVER if active else expiry


def fields(name, record, secret):
    if not isinstance(secret, str) or not secret:
        raise OutcomeUnknown('State record authentication is unavailable')
    raw = encoded(record).encode()
    if len(raw) > MAX_EXPANDED_BYTES:
        raise OutcomeUnknown('Single expanded state record exceeds capacity')
    payload = base64.b64encode(zlib.compress(raw)).decode('ascii')
    if len(payload) > MAX_RECORD_BYTES:
        raise OutcomeUnknown('Single encoded state record exceeds capacity')
    active, until = metadata(record)
    receipt=record['values'].get('receipts',{})
    record_ref=((receipt.get('result') or {}).get('data',{}).get('record_actions') or {}).get('ref','')
    tag = hmac.new(secret.encode(), (name + '\n' + payload).encode(), hashlib.sha256).hexdigest()
    return {'payload': {'stringValue': payload}, 'auth_mac': {'stringValue': tag}, 'record_ref':{'stringValue':record_ref},
            'active': {'booleanValue': active}, 'retain_until': {'timestampValue': until.isoformat()}}


def decode(document, name, secret):
    try:
        if document['name'] != name or not document.get('updateTime'):
            raise ValueError()
        f = document['fields']
        payload = f['payload']['stringValue']
        if set(f) != {'payload', 'auth_mac', 'active', 'retain_until','record_ref'} or len(payload) > MAX_RECORD_BYTES:
            raise ValueError()
        tag = hmac.new(secret.encode(), (name + '\n' + payload).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(tag, f['auth_mac']['stringValue']):
            raise ValueError()
        inflater = zlib.decompressobj()
        raw = inflater.decompress(base64.b64decode(payload, validate=True), MAX_EXPANDED_BYTES + 1)
        if len(raw) > MAX_EXPANDED_BYTES or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise ValueError()
        row = json.loads(raw)
        expected = fields(name, row, secret)
        if (f['active'] != expected['active'] or f['record_ref']!=expected['record_ref'] or
                datetime.fromisoformat(f['retain_until']['timestampValue'].replace('Z', '+00:00')) !=
                datetime.fromisoformat(expected['retain_until']['timestampValue'])):
            raise ValueError()
        return row
    except (ValueError, TypeError, KeyError, AttributeError, zlib.error, UnicodeError):
        raise OutcomeUnknown('State record authentication or encoding is invalid') from None
