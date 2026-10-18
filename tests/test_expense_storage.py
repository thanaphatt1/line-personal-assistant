"""Contract, transaction failure and owner isolation checks over synthetic REST."""
from copy import deepcopy
from datetime import date
import json

import httpx
import pytest

from line_assistant.adapters import Conflict
from line_assistant.contracts import Transaction
from line_assistant.firestore_expenses import FirestoreExpenseStore, FirestoreExpenseTarget, FirestoreSavedExpenseEditor, FirestoreMappingEditor, unfields
from line_assistant.history_context import digest
from line_assistant.transport import ApiError, LiveDisabled, NotReady, OutcomeUnknown, WriteBlocked
from firestore_expense_fake import BusinessFirestore

CATALOG = [{'item': 'Rail ticket', 'category': 'Transportation', 'subcategory': 'Rail'},
           {'item': 'Spotify', 'category': 'Subscriptions', 'subcategory': 'Monthly'}]


class BusinessPort:
    service = 'firestore'
    mocked = True

    def __init__(self, fake):
        self.http = httpx.Client(transport=httpx.MockTransport(fake.handle), base_url='https://firestore.googleapis.com')

    def request(self, method, path, *, body=None, params=None):
        try:
            result = self.http.request(method, path, json=body, params=params)
        except httpx.TransportError:
            raise OutcomeUnknown('Synthetic acknowledgement loss') from None
        if not result.is_success:
            raise ApiError(result.status_code)
        return result.json()


def setup(*, owner='synthetic-owner', dataset='default', max_records=1000):
    fake = BusinessFirestore()
    target = FirestoreExpenseTarget('synthetic-project', owner, dataset, max_records=max_records, writes_enabled=True)
    fake.seed(target, CATALOG)
    return fake, FirestoreExpenseStore(BusinessPort(fake), target)


def transaction(item='Rail ticket', amount='35.00', day='2026-10-18', note=''):
    return Transaction(item=item, amount=amount, date=day, kind='expense', note=note)


def test_append_query_summary_stable_id_and_exact_receipt_after_restart():
    fake, store = setup(); entry = transaction(note='preserved')
    receipt = store.add([entry], 'synthetic-key')
    fresh = FirestoreExpenseStore(BusinessPort(fake), store.target)
    assert fresh.reconcile('synthetic-key', {'entries': [entry.model_dump(mode='json')]}).receipt == receipt
    assert fresh.add([entry], 'synthetic-key') == receipt and fake.commits == 1
    assert fresh.saved_entries(date(2026, 10, 18), date(2026, 10, 18)) == [
        {'date': '2026-10-18', 'item': 'Rail ticket', 'amount': '35.00', 'note': 'preserved'}]
    assert fresh.summary(date(2026, 10, 18), date(2026, 10, 18)) == {'income': None, 'income_supported': False, 'expense': '35.00'}
    assert fresh.classification_entries()[0]['transaction_id'] == receipt[0]
    with pytest.raises(Conflict):fresh.add([transaction(amount='36')], 'synthetic-key')
    assert fake.commits == 1


@pytest.mark.parametrize('mode', ['lost_ack', 'before_ack', 'corrupt_ack'])
def test_unknown_commit_never_retries_and_receipt_proves_only_actual_commit(mode):
    fake, store = setup(); setattr(fake, mode, True); entry = transaction(); payload = {'entries': [entry.model_dump(mode='json')]}
    with pytest.raises(OutcomeUnknown):store.add([entry], 'unknown-key')
    assert store.reconcile('unknown-key', payload).outcome == ('unknown' if mode == 'before_ack' else 'committed')
    count = fake.commits
    if mode == 'before_ack':
        with pytest.raises(OutcomeUnknown):store.add([entry], 'unknown-key')
    else:
        assert store.add([entry], 'unknown-key') == store.reconcile('unknown-key', payload).receipt
    assert fake.commits == count


def test_enrollment_and_expense_are_one_atomic_operation_and_no_partial_write():
    fake, store = setup()
    payload = {'entries': [transaction('Metro pass').model_dump(mode='json')],
               'catalog': [{'item': 'Metro pass', 'category': 'Transportation', 'subcategory': 'Rail'}]}
    fake.abort = True
    with pytest.raises(Conflict):store.enroll_and_add(payload, 'enroll-key')
    assert len(store.taxonomy().rows) == 2 and not store.classification_entries() and fake.commits == 0
    fake.abort = False
    fresh = FirestoreExpenseStore(BusinessPort(fake), store.target)
    fresh.enroll_and_add(payload, 'new-reviewed-key')
    assert fresh.catalog().identity('Metro pass').canonical == 'Metro pass'
    assert fresh.classification_entries()[0]['item'] == 'Metro pass' and fake.commits == 1


def test_saved_edit_compares_provider_version_and_preserves_note():
    fake, store = setup(); identifier = store.add([transaction(note='keep')], 'append')[0]
    editor = FirestoreSavedExpenseEditor(store, writes_enabled=True); before = editor.get(identifier)
    payload = {'domain': 'expense', 'id': identifier, 'before': before, 'changes': {'amount': '45.00'},
               'after': {'item': before['item'], 'date': before['date'], 'amount': '45.00'}}
    result = editor.apply(payload, 'edit', bind_request=lambda *args: None)
    assert result['after']['amount'] == '45.00' and editor.get(identifier)['note'] == 'keep'
    assert editor.reconcile('edit', payload).outcome == 'committed'
    with pytest.raises(Conflict):editor.apply(payload, 'stale-edit', bind_request=lambda *args: None)


def test_mapping_changes_future_classification_without_rewriting_old_expenses():
    fake, store = setup(); editor = FirestoreMappingEditor(store, enabled=True)
    tax = store.taxonomy(); payload = {'catalog_hash': digest([[r.item, r.category, r.subcategory] for r in tax.rows]),
        'rows': [{'item': 'Unlisted subway', 'category': 'Transportation', 'subcategory': 'Rail'}]}
    before = {k: deepcopy(v) for k, v in fake.documents.items() if '/expenses/' in k}
    assert editor.apply(payload, 'map', bind=lambda *args: None)['proof'] == 'transaction_receipt'
    assert editor.reconcile('map', payload).outcome == 'committed'
    assert store.catalog().identity('Unlisted subway').canonical == 'Unlisted subway'
    assert {k: v for k, v in fake.documents.items() if '/expenses/' in k} == before


@pytest.mark.parametrize('field', ['catalog', 'expense', 'meta'])
def test_concurrent_modification_aborts_the_entire_transaction(field):
    fake, store = setup(); identifier = store.add([transaction()], 'append')[0]
    def change(server):
        name = (store.target.parent + '/catalog/' + store.catalog_id('Rail ticket') if field == 'catalog'
                else store.target.parent + '/expenses/' + identifier if field == 'expense' else store.target.parent)
        server.documents[name]['updateTime'] = server.stamp()
    fake.before_commit = change
    with pytest.raises(Conflict):store.add([transaction()], 'racing')
    assert len(store.classification_entries()) == 1 and fake.commits == 1


def test_catalog_change_since_current_read_blocks_write_without_mutating():
    fake, store = setup(); store.taxonomy()
    meta = unfields(fake.documents[store.target.parent]); meta['catalog_revision'] += 1
    fake.put(store.target.parent, meta)
    with pytest.raises(Conflict):store.add([transaction()], 'stale')
    assert fake.commits == 0


def test_complete_paginated_read_includes_every_record_and_rejects_partial_bounds():
    fake, store = setup()
    entries = [(f'synthetic_{i:04d}', transaction(amount='1').model_dump(mode='json')) for i in range(205)]
    for _, row in entries:row.pop('kind')
    fake.seed(store.target, CATALOG, entries)
    assert len(store.classification_entries()) == 205
    assert store.summary(date(2026, 1, 1), date(2026, 12, 31))['expense'] == '205.00'
    small = FirestoreExpenseStore(BusinessPort(fake), FirestoreExpenseTarget('synthetic-project', 'synthetic-owner', max_records=200))
    with pytest.raises(NotReady):small.classification_entries()


@pytest.mark.parametrize('damage', ['owner', 'amount', 'date', 'count', 'catalog_id'])
def test_invalid_business_data_fails_instead_of_returning_partial_totals(damage):
    fake, store = setup(); identifier = store.add([transaction()], 'append')[0]
    name = store.target.parent + '/expenses/' + identifier
    if damage == 'count':
        v = unfields(fake.documents[store.target.parent]); v['expense_count'] = 0; fake.put(store.target.parent, v)
    elif damage == 'catalog_id':
        old = store.target.parent + '/catalog/' + store.catalog_id('Rail ticket'); row = fake.documents.pop(old)
        row['name'] = store.target.parent + '/catalog/wrong'; fake.documents[row['name']] = row
    else:
        value = unfields(fake.documents[name]); value[{'owner': 'owner_hash', 'amount': 'amount_minor', 'date': 'date'}[damage]] = {'owner': 'foreign', 'amount': -1, 'date': 'not-a-date'}[damage]
        fake.put(name, value)
    with pytest.raises(NotReady):store.summary(date(2026, 1, 1), date(2026, 12, 31))


def test_owner_and_dataset_namespaces_are_independent():
    fake, store = setup(); store.add([transaction()], 'same-key')
    for owner, dataset in [('another-owner', 'default'), ('synthetic-owner', 'another')]:
        target = FirestoreExpenseTarget('synthetic-project', owner, dataset, writes_enabled=True); fake.seed(target, CATALOG)
        other = FirestoreExpenseStore(BusinessPort(fake), target)
        assert other.scope_id != store.scope_id and other.classification_entries() == []
        assert other.reconcile('same-key', {'entries': [transaction().model_dump(mode='json')]}).outcome == 'unknown'
    with pytest.raises(LiveDisabled):store._get(store.target.parent.replace(store.target.owner_hash, 'foreign'))


@pytest.mark.parametrize('value', ['../x', 'a/b', '', 'x' * 65])
def test_path_injection_is_rejected_before_network(value):
    with pytest.raises(ValueError):FirestoreExpenseTarget('synthetic-project', 'owner', value)


def test_disabled_writes_and_missing_setup_do_not_mutate():
    fake, store = setup()
    disabled = FirestoreExpenseStore(BusinessPort(fake), FirestoreExpenseTarget('synthetic-project', 'synthetic-owner'))
    with pytest.raises(WriteBlocked):disabled.add([transaction()], 'disabled')
    fake.documents.clear()
    with pytest.raises(NotReady):store.catalog()
    assert fake.commits == 0
