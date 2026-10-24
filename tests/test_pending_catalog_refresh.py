"""Catalog drift revalidates pending proposals without losing original facts."""
from copy import deepcopy
import socket
import pytest
from line_assistant.pending_catalog import revalidate
from line_assistant.taxonomy import Taxonomy,identifier
from test_pending_records import patch
from test_weekday_enrollment_v11 import harness,active,KNOWN
from test_catalog_names_v09 import prepare,entry
from test_history_first import response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No real account or providers')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)


def waiting():
    h=harness()
    r=prepare(h,f'{KNOWN[0]} 30 บาท วันที่ 20',[entry(item=KNOWN[0],amount=30,date_text='วันที่ 20')])
    assert r.status=='clarification' and active(h)['entries'][0]['date'] is None
    return h


def test_unrelated_catalog_addition_allows_date_completion_and_fresh_confirmation_once():
    h=waiting();before=deepcopy(active(h));old=deepcopy(before['entries'][0])
    h.sheets.rows.append(('ชื่อทดสอบเพิ่มภายหลัง','Food/Grocery',''))
    r=h.send('เมื่อวาน',patch({0:[('date_text','เมื่อวาน','เมื่อวาน')]}))
    assert r.status=='preview',r
    d=active(h);now=d['entries'][0]
    assert d['version']==before['version']+1 and d['catalog_hash']!=before['catalog_hash'] and d['review']
    assert now['date']=='2026-10-15' and not h.sheets.entries
    for field in ['item','amount','note','amount_occurrence','entry_ref']:
        assert now.get(field)==old.get(field)
    for field in ['item','amount','note']:
        assert now['fact_sources'].get(field)==old['fact_sources'].get(field)
    event=h.event(r.data['confirm_command']);saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==1
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==1


def test_sparse_patch_rechecks_untouched_sibling_and_rejects_old_confirmation():
    h=harness()
    review=prepare(h,f'{KNOWN[0]} 30 บาท {KNOWN[1]} 50 บาท',[
        entry(item=KNOWN[0],amount=30),entry(item=KNOWN[1],amount=50)])
    assert review.status=='preview'
    before=deepcopy(active(h));h.sheets.rows=[r for r in h.sheets.rows if r[0]!=KNOWN[1]]
    r=h.send('ตัวแรกเป็นเมื่อวาน',patch({0:[('date_text','เมื่อวาน','เมื่อวาน')]}))
    assert r.status=='clarification',r
    d=active(h)
    assert 'entries[1].item_resolution' in d['missing_fields'] and not d['review']
    for field in ['item','amount','date','note','fact_sources','amount_occurrence']:
        assert d['entries'][1].get(field)==before['entries'][1].get(field)
    assert h.send(review.data['confirm_command']).status=='stale' and not h.sheets.entries


def test_invalid_patch_after_catalog_drift_preserves_entire_owned_snapshot():
    h=waiting();before=deepcopy(h.state.drafts)
    h.sheets.rows.append(('ชื่อทดสอบเพิ่มภายหลัง','Gift',''))
    def corrupt(args):args['records'][0]['changes'][0]['source_text']='absent owner evidence'
    r=h.send('เมื่อวาน',patch({0:[('date_text','เมื่อวาน','เมื่อวาน')]},mutate=corrupt),response(text='ขอวันที่ครับ'))
    assert r.status=='clarification' and h.state.drafts==before and not h.sheets.entries


def test_catalog_change_after_replacement_review_still_blocks_commit():
    h=waiting();h.sheets.rows.append(('ชื่อทดสอบเพิ่มภายหลัง','Gift',''))
    r=h.send('เมื่อวาน',patch({0:[('date_text','เมื่อวาน','เมื่อวาน')]}))
    assert r.status=='preview'
    h.sheets.rows.append(('ชื่อทดสอบเพิ่มอีกรอบ','Gift',''))
    assert h.send(r.data['confirm_command']).status=='stale' and not h.sheets.entries


def test_removed_selected_category_is_exposed_as_missing_and_can_be_completed_again():
    h=harness();prepare(h,'ชื่อทดสอบใหม่ 30 บาท',[entry(item='ชื่อทดสอบใหม่',amount=30)])
    review=h.send('ของขวัญ',patch({0:[('category','Gift','ของขวัญ')]}))
    assert review.status=='preview'
    h.sheets.rows=[r for r in h.sheets.rows if r[1]!='Gift']
    result=h.send('เมื่อวาน',patch({0:[('date_text','เมื่อวาน','เมื่อวาน')]}))
    assert result.status=='clarification' and 'entries[0].category' in active(h)['missing_fields']
    assert active(h)['entries'][0]['mapping'] is None and not h.sheets.entries
    updated=h.send('ของกิน',patch({0:[('category','Food/Grocery','ของกิน')]}))
    assert updated.status=='preview' and active(h)['entries'][0]['mapping']['category']=='Food/Grocery'


def test_review_refresh_revalidates_a_newly_existing_catalog_name_before_proposing_enrollment():
    h=harness();prepare(h,'ชื่อทดสอบใหม่ 30 บาท',[entry(item='ชื่อทดสอบใหม่',amount=30)])
    review=h.send('ของขวัญ',patch({0:[('category','Gift','ของขวัญ')]}))
    assert review.status=='preview'
    old=deepcopy(active(h));h.sheets.rows.append(('ชื่อทดสอบใหม่','Food/Grocery',''))
    def refresh(body):
        return response(name='prepare_expense_review',args={'draft_ref':old['draft_ref'],'draft_version':old['version']})
    result=h.send('ขอตรวจรายการก่อนบันทึก',refresh)
    assert result.status=='preview',result
    assert active(h)['version']==old['version']+1
    assert active(h)['entries'][0]['mapping'] is None and not h.sheets.entries


def test_new_catalog_identity_removes_duplicate_enrollment_from_the_replacement():
    h=waiting();draft=deepcopy(active(h))
    e=draft['entries'][0];e['item']='ชื่อทดสอบใหม่';e['mapping']={'item':e['item'],'category':'Gift','subcategory':''}
    e['enrollment']={'category_id':identifier('cat','Gift'),'subcategory_id':identifier('sub','Gift\0')}
    tax=Taxonomy([*h.sheets.rows,(e['item'],'Food/Grocery','')])
    fresh=revalidate(draft,tax)
    assert fresh['entries'][0]['mapping'] is None and 'enrollment' not in fresh['entries'][0]
    assert draft['entries'][0]['mapping'] and fresh['review'] is None


@pytest.mark.parametrize('change',['unrelated','category_removed','subcategory_removed'])
def test_enrollment_choices_are_revalidated_without_guessing_a_replacement(change):
    h=waiting();draft=deepcopy(active(h));e=draft['entries'][0]
    e.update(item='ชื่อทดสอบใหม่',mapping={'item':'ชื่อทดสอบใหม่','category':'Gift','subcategory':'Cards'},
        enrollment={'category_id':identifier('cat','Gift'),'subcategory_id':identifier('sub','Gift\0Cards')})
    rows=[*h.sheets.rows,('หมวดทดสอบใหม่','Gift','Cards'),('เพิ่มทดสอบ','Gift','Other')]
    if change=='category_removed':rows=[r for r in rows if r[1]!='Gift']
    if change=='subcategory_removed':rows=[r for r in rows if r[2]!='Cards']
    fresh=revalidate(draft,Taxonomy(rows));current=fresh['entries'][0]
    if change=='unrelated':assert current['mapping']==e['mapping']
    else:
        assert current['mapping'] is None
        field='category' if change=='category_removed' else 'subcategory'
        assert 'entries[0].'+field in fresh['missing_fields']
    for field in ['item','amount','date','note','fact_sources','amount_occurrence']:
        assert current.get(field)==e.get(field)
