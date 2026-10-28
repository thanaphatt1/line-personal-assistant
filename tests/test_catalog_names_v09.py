"""Catalog identity proposals with immutable owner mentions; synthetic only."""
from copy import deepcopy
from datetime import datetime
import json,socket
import pytest
from test_raw_history import Harness,context
from test_history_first import response
from line_assistant.history_context import HistoryState,source_ref
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.catalog import ItemCatalog

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No real provider or network access')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def harness(names=('grab bike','รถรับส่ง'),**kw):
    h=Harness(**kw);h.now=datetime.fromisoformat('2026-10-16T06:30:00+00:00')
    h.sheets.rows.extend((name,'Transportation','') for name in names);return h

def entry(item='grab bike',amount=37,item_text=None,**kw):
    return {'entry_ref':None,'item':item,'item_text':item_text,'amount':amount,'date_text':None,'date_ref':None,'note':None,'source_refs':[],'field_sources':None,**kw}

def prepare(h,text,entries,refs=None):
    def call(body):
        c=context(body);values=deepcopy(entries)
        for e in values:
            e['source_refs']=refs or [c['current_source_ref']]
            # This helper deliberately exercises the legacy wire handler.
            # Current strict model proposals are tested separately.
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':values})
    return h.send(text,call,response(text='synthetic fallback; never a live model'))

def choose(h,index=0,item='grab bike',text='ใช่ครับ',version=None):
    d=h.draft()
    def call(body):
        return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':version or d['version'],
            'entry_ref':d['entries'][index]['entry_ref'],'category':None,'subcategory':None,'existing_item':item,'resolution_step':'use_existing_item'})
    return h.send(text,call,response(text='synthetic fallback'))

@pytest.mark.parametrize('surface',['grabbike','GrabBike','GRAB BIKE','grab  bike','grab\tbike','grab\u00a0bike','g r a b b i k e'])
@pytest.mark.parametrize('proposal',['grab bike','grabbike'])
def test_unique_case_whitespace_equivalence_reviews_actual_catalog_identity(surface,proposal):
    h=harness();text=surface+' 37 บาท';r=prepare(h,text,[entry(item=proposal)])
    assert r.status=='preview',r
    e=h.draft()['entries'][0];assert (e['item'],e['amount'],e['date'])==('grab bike','37','2026-10-16')
    assert e['fact_sources']['item']==[source_ref('history-1')] and e['source_quote']==text
    assert 'grab bike 37' in r.message and not h.sheets.entries and len(h.port.requests)==1

@pytest.mark.parametrize('canonical,surface',[('Café Shop','CAFE\u0301SHOP'),('Straße Cafe','STRASSECAFE'),('Bus 56','BUS56')])
def test_whitespace_unicode_and_numeric_names_keep_distinct_original_amount_spans(canonical,surface):
    h=harness((canonical,));text=f'{surface} 37 บาท กับ {canonical} 39 บาท เมื่อวาน'
    r=prepare(h,text,[entry(item=canonical,amount=n,date_text='เมื่อวาน') for n in [37,39]])
    assert r.status=='preview',r
    rows=h.draft()['entries'];assert [e['item'] for e in rows]==[canonical,canonical]
    assert all(text[slice(*e['amount_occurrence']['span'])]==e['amount'] for e in rows)
    assert len({e['amount_occurrence']['occurrence_ref'] for e in rows})==2

@pytest.mark.parametrize('surface,expected',[('grabbike','GrabBike'),('GRAB BIKE','grab bike')])
def test_exact_catalog_identity_wins_over_a_different_whitespace_equivalent_name(surface,expected):
    h=harness(('grab bike','GrabBike'));r=prepare(h,surface+' 37 บาท',[entry()])
    assert r.status=='preview' and h.draft()['entries'][0]['item']==expected

@pytest.mark.parametrize('proposal',['grab bike','GrabBike'])
def test_ambiguous_spacing_never_auto_merges_distinct_real_names(proposal):
    h=harness(('grab bike','GrabBike'));r=prepare(h,'grab  bike 37 บาท',[entry(item=proposal)])
    assert r.status=='clarification' and h.draft()['review'] is None
    e=h.draft()['entries'][0];assert (e['item'],e['amount'],e['date'])==('grab  bike','37','2026-10-16')
    assert {v['item'] for v in r.data['tool_result']['catalog_matches']}=={'grab bike','GrabBike'}
    assert 'ชื่อไหน' in r.message and not h.sheets.entries
    selected=choose(h,item='GrabBike',text='เลือก GrabBike');assert selected.status=='preview' and h.draft()['entries'][0]['item']=='GrabBike'

def test_fresh_model_failure_cannot_move_a_name_word_into_an_unsupported_note():
    h=harness(('Polar Ride','PolarRide'))
    r=prepare(h,'polar  ride 43 บาท',[entry(item='polar',amount=43,note='ride')])
    assert r.status!='preview' and not h.sheets.entries and not h.state.drafts
    assert any('entries[0].note_source' in step['result'].get('invalid_fields',[]) for step in h.service.trace)

def test_full_ambiguous_name_preserves_an_explicit_owner_note_through_choice():
    h=harness(('Polar Ride','PolarRide'))
    r=prepare(h,'polar  ride 43 บาท หมายเหตุ กะเย็น',[entry(item='polar  ride',amount=43,note='กะเย็น')])
    assert r.status=='clarification' and h.draft()['review'] is None and not h.sheets.entries
    assert {v['item'] for v in r.data['tool_result']['catalog_matches']}=={'Polar Ride','PolarRide'}
    selected=choose(h,item='PolarRide',text='เลือก PolarRide')
    assert selected.status=='preview' and h.draft()['entries'][0]['note']=='กะเย็น'
    assert (h.draft()['entries'][0]['item'],h.draft()['entries'][0]['amount'])==('PolarRide','43')

@pytest.mark.parametrize('quote',[None,'gabr bike'])
def test_real_catalog_typo_proposal_uses_original_name_and_short_followup_without_reentry(quote):
    h=harness();text='gabr bike 37 บาท เมื่อวาน';r=prepare(h,text,[entry(item_text=quote,date_text='เมื่อวาน')])
    assert r.status=='clarification',r
    e=deepcopy(h.draft()['entries'][0]);assert (e['item'],e['amount'],e['date'])==('gabr bike','37','2026-10-15')
    assert 'หมายถึง grab bike ใช่ไหม' in r.message and h.draft()['review'] is None and not h.sheets.entries
    selected=choose(h);assert selected.status=='preview',selected
    after=h.draft()['entries'][0];assert (after['item'],after['amount'],after['date'])==('grab bike','37','2026-10-15')
    assert after['fact_sources']==e['fact_sources'] and after['source_quote']==text
    assert after['item_resolution']=={'from':'gabr bike','to':'grab bike'}

@pytest.mark.parametrize('proposed',['gabr bike','grab bike'])
def test_multiple_plausible_typo_matches_require_choice(proposed):
    h=harness(('grab bike','grab bite'));r=prepare(h,'gabr bike 37 บาท',[entry(item=proposed,item_text='gabr bike')])
    assert r.status=='clarification' and h.draft()['review'] is None
    assert {v['item'] for v in r.data['tool_result']['catalog_matches']}=={'grab bike','grab bite'}
    assert 'ชื่อไหน' in r.message and not h.sheets.entries

def test_nonexistent_model_catalog_id_is_not_used_as_identity_or_choice():
    h=harness();r=prepare(h,'gabr bike 37 บาท',[entry(item='catalog_hallucinated_42',item_text='gabr bike')])
    assert r.status=='clarification';e=h.draft()['entries'][0]
    assert e['item']=='gabr bike' and e['amount']=='37' and 'catalog_hallucinated_42' not in e.get('name_candidates',[])
    assert all(v['item'] in {'grab bike','รถรับส่ง'} for v in r.data['tool_result']['catalog_matches'])
    before=deepcopy(h.state.drafts);bad=choose(h,item='catalog_hallucinated_42')
    assert bad.status!='preview' and h.state.drafts==before and not h.sheets.entries

@pytest.mark.parametrize('surface',['grabbike','gabr bike','ของใหม่'])
def test_mixed_known_and_unresolved_names_keep_both_entries_and_all_other_facts(surface):
    h=harness();r=prepare(h,f'{surface} 37 บาท กับ รถรับส่ง 26 บาท',[entry(item=surface),entry(item='รถรับส่ง',amount=26)])
    d=h.draft();assert len(d['entries'])==2
    assert [(e['amount'],e['date']) for e in d['entries']]==[('37','2026-10-16'),('26','2026-10-16')]
    if surface=='grabbike':assert r.status=='preview' and d['entries'][0]['item']=='grab bike'
    else:assert r.status=='clarification' and d['review'] is None and d['entries'][1]['item']=='รถรับส่ง'
    assert not h.sheets.entries

def test_real_model_catalog_proposal_without_quote_can_recover_one_unambiguous_batch_mention():
    h=harness();r=prepare(h,'gabr bike 37 บาท กับ รถรับส่ง 26 บาท',[entry(),entry(item='รถรับส่ง',amount=26)])
    assert r.status=='clarification' and [e['amount'] for e in h.draft()['entries']]==['37','26']
    assert h.draft()['entries'][0]['item']=='gabr bike' and 'หมายถึง grab bike ใช่ไหม' in r.message
    assert choose(h).status=='preview' and h.draft()['entries'][1]['item']=='รถรับส่ง'

def test_owner_note_text_cannot_supply_a_catalog_correction_mention():
    h=harness();r=prepare(h,'รถรับส่ง 26 บาท หมายเหตุ gabr bike 37 บาท',[entry(item_text='gabr bike')])
    assert r.status!='preview' and not h.sheets.entries

@pytest.mark.parametrize('quote',['grabbike','grab  bike'])
def test_literal_quote_spacing_is_mapped_back_to_the_actual_original_surface(quote):
    h=harness();r=prepare(h,'GRAB BIKE 37 บาท',[entry(item_text=quote)])
    assert r.status=='preview',r
    assert h.draft()['entries'][0]['source_quote']=='GRAB BIKE 37 บาท'

def test_owner_can_inspect_and_resolve_the_second_pending_name_without_losing_first():
    h=harness(('grab bike','shuttle'));prepare(h,'gabr bike 37 บาท กับ shuttl 26 บาท',[entry(item_text='gabr bike'),entry(item='shuttle',item_text='shuttl',amount=26)])
    d=h.draft();before=deepcopy(d['entries'][0])
    def show(body):
        return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':d['entries'][1]['entry_ref'],
            'category':None,'subcategory':None,'existing_item':None,'resolution_step':'show_choices'})
    r=h.send('ขอดูชื่อ shuttl',show)
    assert r.status=='clarification' and 'shuttl หมายถึง shuttle ใช่ไหม' in r.message
    assert r.data['tool_result']['catalog_entry_ref']==d['entries'][1]['entry_ref']
    assert choose(h,index=1,item='shuttle').status=='clarification'
    assert h.draft()['entries'][0]==before and h.draft()['entries'][1]['item']=='shuttle' and not h.sheets.entries

def test_sparse_known_sibling_update_then_typo_yes_preserves_every_row():
    h=harness();r=prepare(h,'gabr bike 37 บาท กับ รถรับส่ง 26 บาท',[entry(item_text='gabr bike'),entry(item='รถรับส่ง',amount=26)])
    assert r.status=='clarification';d=h.draft();first=deepcopy(d['entries'][0])
    def update(body):
        e=entry(item=None,amount=28,entry_ref=d['entries'][1]['entry_ref']);e['source_refs']=[context(body)['current_source_ref']]
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[e]})
    r=h.send('แก้รถรับส่งเป็น 28 บาท',update,response(text='synthetic fallback'))
    assert r.status=='clarification' and h.draft()['entries'][0]==first
    assert h.draft()['entries'][1]['amount']=='28'
    assert choose(h).status=='preview';assert [e['amount'] for e in h.draft()['entries']]==['37','28']
    assert not h.sheets.entries

def test_same_unresolved_labels_have_entry_bound_choices_and_no_partial_commit():
    h=harness();r=prepare(h,'gabr bike 37 บาท\ngabr bike 39 บาท',[entry(item_text='gabr bike',amount=n) for n in [37,39]])
    assert r.status=='clarification';d=h.draft();before=deepcopy(d['entries'])
    assert r.data['tool_result']['catalog_entry_ref']==before[0]['entry_ref']
    assert choose(h,index=1).status!='preview' and h.draft()['entries']==before
    assert choose(h,index=0).status=='clarification'
    assert h.draft()['entries'][0]['item']=='grab bike' and h.draft()['entries'][1]==before[1]
    assert not h.sheets.entries and h.draft()['review'] is None
    r=choose(h,index=1);assert r.status=='preview' and not h.sheets.entries
    e=h.event(r.data['confirm_command']);saved=h.service.handle(e)
    assert saved.status=='recorded' and len(h.sheets.entries)==2
    assert h.service.handle(deepcopy(e))==saved and len(h.sheets.entries)==2

@pytest.mark.parametrize('damage',['missing_source','fabricated_quote','reused_amount'])
def test_tolerance_does_not_weaken_source_or_occurrence_guards(damage):
    h=harness()
    if damage=='missing_source':r=prepare(h,'grabbike 37 บาท',[entry()],refs=['msg_foreign'])
    elif damage=='fabricated_quote':r=prepare(h,'รถรับส่ง 26 บาท',[entry(item_text='gabr bike')])
    else:r=prepare(h,'grabbike 37 บาท กับ grab bike 39 บาท',[entry(amount=37),entry(amount=37)])
    assert r.status!='preview' and not h.sheets.entries

def test_whitespace_variants_do_not_borrow_historical_amounts():
    h=harness();h.send('grabbike 98 บาท',response(text='synthetic history'))
    refs={'item':[],'amount':[source_ref('history-1')],'date':[],'note':[]}
    r=prepare(h,'grabbike 37 บาท',[entry(amount=98,field_sources=refs)],refs=[source_ref('history-1'),source_ref('history-2')])
    assert r.status!='preview' and not h.sheets.entries

def test_catalog_removal_and_stale_version_cannot_resolve_pending_name():
    h=harness();prepare(h,'gabr bike 37 บาท',[entry(item_text='gabr bike')]);d=h.draft();version=d['version']
    h.sheets.rows=[r for r in h.sheets.rows if r[0]!='grab bike'];before=deepcopy(d)
    assert choose(h).status!='preview' and h.draft()==before
    h.sheets.rows.append(('grab bike','Transportation',''))
    assert choose(h,version=version+1).status!='preview' and h.draft()==before

def test_pending_name_choices_and_partial_batch_roundtrip_without_losing_provenance():
    h=harness();prepare(h,'gabr bike 37 บาท กับ รถรับส่ง 26 บาท',[entry(item_text='gabr bike'),entry(item='รถรับส่ง',amount=26)])
    snapshot=history_snapshot(h.state);fresh=HistoryState();fresh.clock=lambda:h.now;fresh.enable_raw_history(journal=deepcopy(h.state.raw_journal))
    restore_history(fresh,snapshot,'mock-owner');assert fresh.drafts==h.state.drafts
    restored=harness(state=fresh);restored.now=h.now;restored.counter=h.counter
    assert choose(restored).status=='preview' and [e['amount'] for e in restored.draft()['entries']]==['37','26']

@pytest.mark.parametrize('surface',['grab\nbike','grab-bike','grab_car'])
def test_non_whitespace_name_changes_are_not_automatically_merged(surface):
    h=harness();r=prepare(h,surface+' 37 บาท',[entry()])
    assert r.status!='preview' and not h.sheets.entries

def test_two_exact_whitespace_colliding_identities_stay_distinct_in_one_batch():
    h=harness(('grab bike','GrabBike'))
    r=prepare(h,'GrabBike 37 บาท กับ grab bike 39 บาท',[entry(item='GrabBike'),entry(amount=39)])
    assert r.status=='preview',r
    assert [(e['item'],e['amount']) for e in h.draft()['entries']]==[('GrabBike','37'),('grab bike','39')]

@pytest.mark.parametrize('delivery',['missing','pending','wrong_content','sent'])
def test_live_name_choice_requires_the_actual_delivered_question(delivery):
    from datetime import timedelta
    from line_assistant.state import Reply
    from line_assistant.semantic_tools import message_hash,rendered_message
    h=harness();r=prepare(h,'gabr bike 37 บาท',[entry(item_text='gabr bike')]);h.service.production=True
    if delivery!='missing':h.state.replies['history-1']=Reply(status='pending' if delivery=='pending' else 'sent',content_hash='wrong' if delivery=='wrong_content' else message_hash(rendered_message(r)),expires_at=h.now+timedelta(hours=1))
    selected=choose(h)
    if delivery=='sent':assert selected.status=='preview',selected
    else:assert selected.status!='preview' and h.draft()['entries'][0]['item']=='gabr bike'
    assert not h.sheets.entries

@pytest.mark.parametrize('text',['BTS 2 ขา 71 กับ 73','รถรับส่ง 26 บาท กับ grab bike 37 บาท เมื่อวาน'])
def test_combined_followup_fixes_count_role_while_ambiguous_distinct_suffix_still_asks(text):
    h=harness(('BTS','grab bike','รถรับส่ง'))
    values=[entry(item='BTS',amount=n) for n in [71,73]] if 'ขา' in text else [entry(item='รถรับส่ง',amount=26,date_text='เมื่อวาน'),entry(date_text='เมื่อวาน')]
    r=prepare(h,text,values)
    assert (r.status=='preview')==('ขา' in text)
    assert not h.sheets.entries
