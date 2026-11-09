"""Native enrollment must not turn a display limit into an allowed-value limit."""
from copy import deepcopy
import json
import socket

import pytest

from line_assistant.taxonomy import Taxonomy, identifier
from test_catalog_names_v09 import prepare, entry
from test_history_first import response
from test_pending_records import patch
from test_weekday_enrollment_v11 import harness, active, enroll


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs): raise AssertionError('Synthetic taxonomy only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


def pending(*, subcategories=('',)):
    h = harness()
    h.sheets.rows.append(('Synthetic Household Seed', 'Household', ''))
    h.sheets.rows.extend((f'Synthetic Lifestyle Seed {i}', 'Lifestyle', sub) for i, sub in enumerate(subcategories))
    h.sheets.rows.extend([('Synthetic Sport Seed', 'Sport', ''), ('Synthetic Health Seed', 'Health', '')])
    r = prepare(h, 'เสื้อสมมติอัลบา 490 บาท', [entry(item='เสื้อสมมติอัลบา', amount=490)])
    assert r.status == 'clarification'
    return h


def test_complete_taxonomy_keeps_categories_and_subcategories_beyond_eight():
    tax = Taxonomy([(f'Item {i} {j}', f'Category {i}', f'Sub {j}') for i in range(24) for j in range(12)])
    complete = tax.choices('New unrelated item', complete=True)
    assert len(complete['categories']) == 24
    assert all(len(c['subcategories']) == 12 for c in complete['categories'])
    # The old small-context interface remains an explicitly bounded view.
    legacy = tax.choices('New unrelated item')
    assert len(legacy['categories']) == 8 and all(len(c['subcategories']) == 8 for c in legacy['categories'])


def test_native_context_and_compatibility_schema_offer_the_late_category():
    h = pending()
    def inspect(body):
        c = json.loads(body['input'][0]['content'])
        choices = c['available_drafts'][0]['item_resolutions'][0]['category_choices']
        assert len(choices) == len(h.sheets.taxonomy().categories) == 12
        assert next(i for i, row in enumerate(choices) if row['label'] == 'Lifestyle') >= 8
        fields = c['pending_records'][0]['records'][0]['fields']
        assert 'Lifestyle' in next(f for f in fields if f['name'] == 'category')['choices']
        legacy = next(t for t in body['tools'] if t['name'] == 'prepare_item_enrollment')
        assert 'Lifestyle' in legacy['parameters']['properties']['category']['enum']
        return response(text='ตัวเลือกครบครับ')
    h.send('ดูตัวเลือก', inspect)
    assert not h.sheets.entries


@pytest.mark.parametrize('owner_text', ['Lifestyle', 'lifestyle', 'lifestlye'])
def test_add_then_choose_lifestyle_beyond_eighth_slot_requires_confirmation(owner_text):
    h = pending(); before = deepcopy(active(h)['entries'][0])
    ask = h.send('เพิ่ม', patch({0: [('catalog_action', 'new', 'เพิ่ม')]}))
    assert ask.status == 'clarification' and 'Lifestyle' in ask.message
    r = h.send(owner_text, patch({0: [('category', 'Lifestyle', owner_text)]}))
    assert r.status == 'preview' and active(h)['entries'][0]['mapping']['category'] == 'Lifestyle'
    assert not h.sheets.entries and not h.sheets.atomic_batches
    now = active(h)['entries'][0]
    assert all(now[k] == before[k] for k in ['item', 'amount', 'date', 'note'])
    e = h.event(r.data['confirm_command']); calls = len(h.port.requests)
    saved = h.service.handle(e)
    assert saved.status == 'recorded' and len(h.sheets.entries) == h.sheets.atomic_batches == 1
    assert ('เสื้อสมมติอัลบา', 'Lifestyle', '') in h.sheets.rows
    assert h.service.handle(deepcopy(e)) == saved and len(h.port.requests) == calls
    assert len(h.sheets.entries) == 1


@pytest.mark.parametrize('subcategory,owner_text', [('Style 10', 'Style 10'), ('', 'ไม่มีหมวดย่อย'), (None, 'ไม่มีหมวดย่อย')])
def test_late_subcategory_and_late_empty_choice_are_offered_and_accepted(subcategory, owner_text):
    h = pending(subcategories=tuple(f'Style {i}' for i in range(1, 11)) + ('',))
    ask = h.send('Lifestyle', patch({0: [('category', 'Lifestyle', 'Lifestyle')]}))
    assert ask.status == 'clarification' and 'Style 10' in ask.message and '(ไม่มีหมวดย่อย)' in ask.message
    choices = ask.data['tool_result']['item_enrollment']['choices']
    assert len(choices) == 11 and choices[-1]['label'] == ''
    r = h.send(owner_text, patch({0: [('subcategory', subcategory, owner_text)]}))
    assert r.status == 'preview' and active(h)['entries'][0]['mapping']['subcategory'] == (subcategory or '')
    assert not h.sheets.entries


def test_compatibility_enrollment_can_select_category_after_eighth_slot():
    h = pending(); enroll(h, 'เพิ่ม')
    r = enroll(h, 'Lifestyle', step='choose_category', category=identifier('cat', 'Lifestyle'))
    assert r.status == 'preview' and active(h)['entries'][0]['mapping']['category'] == 'Lifestyle'
    assert not h.sheets.entries


def test_read_only_category_lookup_returns_all_existing_categories():
    h = pending(); before = deepcopy(h.state.drafts)
    output = h.service.tool({'name': 'lookup_catalog', 'arguments': json.dumps({'kind': 'category', 'query': 'Lifestyle'})})
    assert output['ok'] and len(output['category_choices']) == 12
    assert any(c['label'] == 'Lifestyle' for c in output['category_choices'])
    assert h.state.drafts == before and not h.sheets.entries


def test_nonexistent_category_is_still_rejected_without_mutating_the_draft():
    h = pending(); before = deepcopy(h.state.drafts)
    r = h.send('Invented Category', patch({0: [('category', 'Invented Category', 'Invented Category')]}), response(text='เลือกหมวดที่มีอยู่ครับ'))
    assert r.status == 'clarification' and h.state.drafts == before and not h.sheets.entries


def test_removed_category_cannot_commit_a_previously_valid_review():
    h = pending(); r = h.send('Lifestyle', patch({0: [('category', 'Lifestyle', 'Lifestyle')]}))
    assert r.status == 'preview'
    h.sheets.rows = [row for row in h.sheets.rows if row[1] != 'Lifestyle']
    saved = h.send(r.data['confirm_command'])
    assert saved.status in {'stale', 'conflict'} and not h.sheets.entries


def test_complete_choices_do_not_allow_a_subcategory_from_another_category():
    h = pending(subcategories=('Style A', 'Style B'))
    h.sheets.rows.append(('Synthetic Other Pair', 'Food/Grocery', 'Other Category Sub'))
    h.send('Lifestyle', patch({0: [('category', 'Lifestyle', 'Lifestyle')]}))
    before = deepcopy(h.state.drafts)
    r = h.send('Other Category Sub', patch({0: [('subcategory', 'Other Category Sub', 'Other Category Sub')]}), response(text='เลือกหมวดย่อยของหมวดนี้ครับ'))
    assert r.status == 'clarification' and h.state.drafts == before and not h.sheets.entries
