"""Shared-role/date-scope metamorphic regressions; every provider is synthetic."""
from copy import deepcopy
from datetime import datetime
import json,socket
import pytest
from test_catalog_names_v09 import harness as names_harness,entry,prepare
from test_history_first import response
from test_raw_history import context
from line_assistant.history_context import HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.language import DIGITS

@pytest.fixture(autouse=True)
def offline(monkeypatch):
 def deny(*a,**kw):raise AssertionError('Synthetic evidence tests must never use network')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def harness():
 h=names_harness(('BTS','MRT','รถตู้','grab bike','รถรับส่ง'))
 h.now=datetime.fromisoformat('2026-10-20T03:00:00+00:00');return h

def rows(h):return next(iter(h.state.drafts.values()))['entries']

def save_replay(h,result,size):
 assert result.status=='preview' and not h.sheets.entries
 before=len(h.port.requests);event=h.event(result.data['confirm_command']);saved=h.service.handle(event)
 assert saved.status=='recorded' and len(h.sheets.entries)==size
 assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==size and len(h.port.requests)==before

@pytest.mark.parametrize('unit',['ขา','เที่ยว','รอบ','ครั้ง'])
@pytest.mark.parametrize('space',['',' '])
@pytest.mark.parametrize('date_style',['implicit','leading','trailing'])
@pytest.mark.parametrize('thai_digits',[False,True])
def test_equivalent_count_and_date_phrasings_have_same_two_distinct_money_occurrences(unit,space,date_style,thai_digits):
 h=harness();text=f'bts{space}2{space}{unit}{space}59กับ65'
 if date_style=='leading':text='เมื่อวาน '+text
 if date_style=='trailing':text+=' เมื่อวาน'
 if thai_digits:text=text.translate(str.maketrans('0123456789','๐๑๒๓๔๕๖๗๘๙'))
 r=prepare(h,text,[entry(item='BTS',amount=n) for n in [59,65]])
 assert r.status=='preview',r
 es=rows(h);assert [e['date'] for e in es]==['2026-10-20' if date_style=='implicit' else '2026-10-19']*2
 assert [e['amount'] for e in es]==['59','65']
 assert len({e['amount_occurrence']['occurrence_ref'] for e in es})==2
 assert all(text[slice(*e['amount_occurrence']['span'])].translate(DIGITS)==e['amount'] for e in es)
 save_replay(h,r,2)

@pytest.mark.parametrize('unit',['ขา','เที่ยว','รอบ','ครั้ง'])
def test_explicit_per_unit_rate_has_distinct_ordinals_at_one_original_price_span(unit):
 h=harness();text=f'BTS 2 {unit} {unit}ละ 59 บาท เมื่อวาน'
 r=prepare(h,text,[entry(item='BTS',amount=59),entry(item='BTS',amount=59)])
 assert r.status=='preview',r
 assert {e['amount_occurrence']['ordinal'] for e in rows(h)}=={0,1}
 save_replay(h,r,2)

@pytest.mark.parametrize('text,values',[
 ('BTS 3 ขา 59 กับ 65',[59,65]),('BTS 2 ขา 124 บาท',[124]),
 ('BTS 2 ขา รวม 124 บาท',[124,124]),('BTS 9 ขา 59 กับ 65',[59,65]),
 ('BTS 0 ขา 59 กับ 65',[59,65]),('BTS -2 ขา 59 กับ 65',[59,65]),
 ('BTS 2.5 ขา 59 กับ 65',[59,65]), ('BTS 2 ขา 59 กับ 65',[59,59]),('Bus 2 59 กับ 65',[59,65]),
])
def test_ambiguous_counts_never_become_money_multipliers_or_partial_financial_commits(text,values):
 h=harness();r=prepare(h,text,[entry(item='BTS',amount=v) for v in values])
 assert r.status!='preview' and not h.sheets.entries
 assert all(d['review'] is None for d in h.state.drafts.values())

@pytest.mark.parametrize('text,expected',[
 ('เมื่อวาน BTS 59 บาท กับ MRT 65 บาท',['2026-10-19','2026-10-19']),
 ('BTS เมื่อวาน 59 บาท กับ MRT วันนี้ 65 บาท',['2026-10-19','2026-10-20']),
 ('BTS 59 บาท เมื่อวาน กับ MRT 65 บาท วันนี้',['2026-10-19','2026-10-20']),
 ('เมื่อวาน BTS 59 บาท และวันนี้ MRT 65 บาท',['2026-10-19','2026-10-20']),
 ('BTS 59 บาทเมื่อวาน กับ MRT 65 บาท',['2026-10-19','2026-10-20']),
 ('BTS 59 บาท กับ MRT 65 บาท ทั้งสองรายการเมื่อวาน',['2026-10-19','2026-10-19']),
 ('BTS 59 บาท กับ MRT 65 บาท เมื่อวานทั้งหมด',['2026-10-19','2026-10-19']),
 ('BTS 59 บาท\nMRT 65 บาท เมื่อวาน',['2026-10-20','2026-10-19']),
])
def test_explicit_date_scope_preserves_per_item_fact_sources_and_implicit_today(text,expected):
 h=harness();r=prepare(h,text,[entry(item='BTS',amount=59),entry(item='MRT',amount=65)])
 assert r.status=='preview',r
 assert [e['date'] for e in rows(h)]==expected
 assert all(e['fact_sources']['date'] and e['source_quote']==text for e in rows(h))
 save_replay(h,r,2)

def test_ambiguous_distinct_item_suffix_retains_facts_and_asks_only_scope_then_cold_resolves():
 h=harness();r=prepare(h,'BTS 59 บาท กับ MRT 65 บาท เมื่อวาน',[entry(item='BTS',amount=59,date_text='เมื่อวาน'),entry(item='MRT',amount=65,date_text='เมื่อวาน')])
 assert r.status=='clarification' and 'เฉพาะรายการสุดท้าย' in r.message,r
 assert [(e['item'],e['amount']) for e in rows(h)]==[('BTS','59'),('MRT','65')]
 assert rows(h)[0]['date'] is None and rows(h)[0]['date_issue']=='ambiguous_date_scope'
 assert rows(h)[1]['date']=='2026-10-19' and next(iter(h.state.drafts.values()))['review'] is None
 snapshot=history_snapshot(h.state);state=HistoryState();state.clock=lambda:h.now
 state.enable_raw_history(journal=deepcopy(h.state.raw_journal));restore_history(state,snapshot,'mock-owner')
 h2=names_harness(('BTS','MRT','รถตู้','grab bike','รถรับส่ง'),state=state);h2.now=h.now;h2.counter=h.counter
 def choose(body):
  c=context(body);d=c['available_drafts'][0]
  return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],
   'entries':[entry(item=None,amount=None,entry_ref=e['entry_ref'],date_text='เมื่อวาน',source_refs=[c['current_source_ref']]) for e in d['entries']]})
 resolved=h2.send('ทั้งสองรายการเมื่อวาน',choose)
 assert resolved.status=='preview' and [e['date'] for e in rows(h2)]==['2026-10-19']*2,resolved
 save_replay(h2,resolved,2)

@pytest.mark.parametrize('why',['ทำไม','ทำไมถึงบันทึกไม่ได้ครับ','เมื่อกี้ผิดตรงไหน','อธิบายข้อผิดพลาด'])
def test_error_explanation_uses_prior_receipt_without_paid_call_or_invented_date_cause(why):
 h=harness();r=prepare(h,'BTS 3 ขา 59 กับ 65',[entry(item='BTS',amount=59),entry(item='BTS',amount=65)])
 assert r.status=='clarification' and 'count_amount_mismatch' in r.data['validation_receipt']['codes']
 calls=len(h.port.requests);again=h.send(why,response(text='แต่งสาเหตุผิดว่าขาดวันที่'))
 assert again.status=='answer' and again.data['explains_server_receipt'] and len(h.port.requests)==calls
 assert 'ไม่ตรงกับจำนวนยอดเงิน' in again.message and 'ใช้วันนี้' in again.message
 assert 'ขาดวันที่' not in again.message and not h.sheets.entries

def test_unrelated_why_question_cannot_reuse_a_previous_error_as_its_cause():
 h=harness();prepare(h,'BTS 3 ขา 59 กับ 65',[entry(item='BTS',amount=59),entry(item='BTS',amount=65)])
 before=len(h.port.requests);r=h.send('ทำไมท้องฟ้าสีฟ้า',response(text='ช่วยเรื่องรายจ่ายครับ'))
 assert not r.data.get('explains_server_receipt') and len(h.port.requests)==before+1

@pytest.mark.parametrize('fields',[False,True])
def test_explicit_field_citations_share_same_per_item_date_scope(fields):
 h=harness()
 def proposed(body):
  c=context(body);refs={'item':[],'amount':[c['current_source_ref']],'date':[c['current_source_ref']],'note':[]}
  return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[
   entry(item=name,amount=amount,date_text=day,source_refs=[c['current_source_ref']],field_sources=refs if fields else None)
   for name,amount,day in [('BTS',59,'เมื่อวาน'),('MRT',65,'วันนี้')]]})
 r=h.send('BTS 59 เมื่อวาน กับ MRT 65 วันนี้',proposed)
 assert r.status=='preview' and [e['date'] for e in rows(h)]==['2026-10-19','2026-10-20'],r

@pytest.mark.parametrize('omitted',['BTS','MRT'])
def test_model_cannot_omit_a_known_priced_sibling_after_source_scoping(omitted):
 h=harness();name='MRT' if omitted=='BTS' else 'BTS';amount=65 if name=='MRT' else 59
 r=prepare(h,'BTS 59 บาท กับ MRT 65 บาท',[entry(item=name,amount=amount)])
 assert r.status=='clarification' and 'expense_batch_incomplete' in r.data['validation_receipt']['codes']
 assert not h.state.drafts and not h.sheets.entries

def test_date_scope_answer_after_explanation_still_binds_same_delivered_version():
 h=harness();r=prepare(h,'BTS 59 กับ MRT 65 เมื่อวาน',[entry(item='BTS',amount=59),entry(item='MRT',amount=65)])
 assert r.status=='clarification'
 before=len(h.port.requests);why=h.send('ทำไม',response(text='should not call'))
 assert why.data['explains_server_receipt'] and len(h.port.requests)==before
 def choose(body):
  c=context(body);d=c['available_drafts'][0]
  return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
   entry(item=None,amount=None,entry_ref=e['entry_ref'],date_text='เมื่อวาน',source_refs=[c['current_source_ref']]) for e in d['entries']]})
 r=h.send('ทั้งสองรายการเมื่อวาน',choose)
 assert r.status=='preview' and [e['date'] for e in rows(h)]==['2026-10-19']*2,r

@pytest.mark.parametrize('text',['BTS 59 วันนี้ กับ MRT 65 ทั้งหมดเมื่อวาน','เมื่อวาน BTS 59 วันนี้ กับ MRT 65'])
def test_conflicting_scopes_or_dates_never_yield_an_authorized_review(text):
 h=harness();r=prepare(h,text,[entry(item='BTS',amount=59),entry(item='MRT',amount=65)])
 assert r.status!='preview' and not h.sheets.entries
 assert all(d['review'] is None for d in h.state.drafts.values())

@pytest.mark.parametrize('unit',['ขา','เที่ยว','รอบ','ครั้ง'])
def test_single_model_unit_rate_is_expanded_only_by_explicit_original_count_evidence(unit):
 h=harness();r=prepare(h,f'BTS 2 {unit} {unit}ละ 41 บาท เมื่อวาน',[entry(item='BTS',amount=41)])
 assert r.status=='preview' and [e['amount'] for e in rows(h)]==['41','41'],r
 assert {e['amount_occurrence']['ordinal'] for e in rows(h)}=={0,1}
 save_replay(h,r,2)

def test_exact_fresh_sparse_scope_reply_updates_only_the_still_unresolved_row():
 h=harness();prepare(h,'BTS 59 กับ MRT 65 เมื่อวาน',[entry(item='BTS',amount=59),entry(item='MRT',amount=65)])
 untouched=deepcopy(rows(h)[1])
 def choose(body):
  c=context(body);d=c['available_drafts'][0]
  return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
   entry(item=None,amount=None,entry_ref=d['entries'][0]['entry_ref'],date_text='ทั้งสองรายการเมื่อวาน',source_refs=[c['current_source_ref']],
    field_sources={'item':[],'amount':[],'date':[d['entries'][0]['fact_sources']['date'][0],c['current_source_ref']],'note':[]})]})
 r=h.send('ทั้งสองรายการเมื่อวาน',choose)
 assert r.status=='preview' and rows(h)[1]==untouched,r
 save_replay(h,r,2)

@pytest.mark.parametrize('text',['BTS 2 ขา รวม 82 บาท','BTS 2 ขา 41 บาท'])
def test_rate_expansion_never_infers_a_unit_price_from_total_or_multiple_original_spans(text):
 h=harness();r=prepare(h,text,[entry(item='BTS',amount=41 if 'รวม' not in text else 82)])
 assert r.status!='preview' and not h.sheets.entries

def test_false_missing_date_question_is_answered_from_validation_receipt_without_model():
 h=harness();prepare(h,'BTS 3 ขา 59 กับ 65',[entry(item='BTS',amount=59),entry(item='BTS',amount=65)])
 before=len(h.port.requests);r=h.send('เพราะไม่มีวันที่เหรอ',response(text='ใช่เพราะขาดวันที่'))
 assert r.data['explains_server_receipt'] and len(h.port.requests)==before
 assert 'ไม่ตรงกับจำนวนยอดเงิน' in r.message and 'ใช้วันนี้' in r.message and 'ขาดวันที่' not in r.message

@pytest.mark.parametrize('label',['bts2ขา','BTS 2 ขา','btsสองขา'])
def test_model_count_suffix_is_separated_from_unique_known_name_using_matching_original_roles(label):
 h=harness();r=prepare(h,label+'59กับ65',[entry(item=label,amount=59),entry(item=label,amount=65)])
 assert r.status=='preview' and [e['item'] for e in rows(h)]==['BTS','BTS'],r
 save_replay(h,r,2)

def test_actual_full_catalog_name_containing_a_count_suffix_is_never_trimmed():
 h=harness();h.sheets.rows.append(('BTS2ขา','Transportation',''))
 r=prepare(h,'BTS2ขา 59',[entry(item='BTS2ขา',amount=59)])
 assert r.status=='preview' and rows(h)[0]['item']=='BTS2ขา'

def test_bound_scope_reply_uses_actual_current_owner_date_even_if_model_cites_only_old_source():
 h=harness();prepare(h,'BTS 59 กับ MRT 65 เมื่อวาน',[entry(item='BTS',amount=59),entry(item='MRT',amount=65)])
 oldref=rows(h)[0]['source_refs'][0]
 def choose(body):
  c=context(body);d=c['available_drafts'][0]
  return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
   entry(item=None,amount=None,entry_ref=e['entry_ref'],date_text='เมื่อวาน',source_refs=[oldref],field_sources={'item':[],'amount':[],'date':[oldref],'note':[]}) for e in d['entries']]})
 r=h.send('ทั้งสองรายการเมื่อวาน',choose)
 assert r.status=='preview' and [e['date'] for e in rows(h)]==['2026-10-19']*2,r
 assert rows(h)[0]['fact_sources']['date']==[context(h.port.requests[-1])['current_source_ref']]
 save_replay(h,r,2)

def test_model_old_date_citation_without_delivered_scope_question_cannot_change_current_fact():
 h=harness();prepare(h,'BTS 59 เมื่อวาน',[entry(item='BTS',amount=59)])
 old=deepcopy(rows(h));oldref=old[0]['source_refs'][0]
 def wrong(body):
  c=context(body);d=c['available_drafts'][0]
  return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
   entry(item=None,amount=None,entry_ref=old[0]['entry_ref'],date_text='วันนี้',source_refs=[oldref],field_sources={'item':[],'amount':[],'date':[oldref],'note':[]})]})
 r=h.send('แก้วันที่ BTS เป็นวันนี้',wrong)
 assert r.status!='preview' and rows(h)==old and not h.sheets.entries

@pytest.mark.parametrize('label',['BTS3ขา','BTS-2ขา','BTS2.5ขา'])
def test_absorbing_an_invalid_or_mismatched_count_into_model_name_cannot_hide_quantity_validation(label):
 h=harness();r=prepare(h,label+'59กับ65',[entry(item=label,amount=59),entry(item=label,amount=65)])
 assert r.status!='preview' and not h.sheets.entries and not h.state.drafts

@pytest.mark.parametrize('delivery',['missing','pending','wrong_content','sent'])
def test_scope_answer_binds_only_the_actual_delivered_versioned_question(delivery):
 from datetime import timedelta
 from line_assistant.state import Reply
 from line_assistant.semantic_tools import message_hash,rendered_message
 h=harness();question=prepare(h,'BTS 59 กับ MRT 65 เมื่อวาน',[entry(item='BTS',amount=59),entry(item='MRT',amount=65)])
 h.service.production=True
 if delivery!='missing':h.state.replies['history-1']=Reply(status='pending' if delivery=='pending' else 'sent',content_hash='wrong' if delivery=='wrong_content' else message_hash(rendered_message(question)),expires_at=h.now+timedelta(hours=1))
 def choose(body):
  c=context(body);d=c['available_drafts'][0]
  return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
   entry(item=None,amount=None,entry_ref=d['entries'][0]['entry_ref'],date_text='เมื่อวาน',source_refs=[c['current_source_ref']])]})
 r=h.send('ทั้งสองรายการเมื่อวาน',choose)
 assert (r.status=='preview')==(delivery=='sent')
 assert not h.sheets.entries

@pytest.mark.parametrize('surface,quote',[('bts2ขา59กับ65','bts2'),('BTS 2 ขา 59 กับ 65','BTS 2'),('btsสองขา59กับ65','btsสอง')])
def test_partial_count_number_in_item_text_uses_original_shared_role_boundary(surface,quote):
 h=harness();r=prepare(h,surface,[entry(item='BTS',item_text=quote,amount=59),entry(item='BTS',item_text=quote,amount=65)])
 assert r.status=='preview' and [e['item'] for e in rows(h)]==['BTS','BTS'],r
 save_replay(h,r,2)

@pytest.mark.parametrize('delivery',['missing','pending','wrong_content','sent'])
def test_pure_name_patch_tool_shape_reuses_existing_delivered_choice_guard(delivery):
 from datetime import timedelta
 from line_assistant.state import Reply
 from line_assistant.semantic_tools import message_hash,rendered_message
 h=harness();question=prepare(h,'grabb b ike 47 บาท กับ MRT 31 บาท',[entry(item='grab bike',item_text='grabb b ike',amount=47),entry(item='MRT',amount=31)])
 assert question.status=='clarification';sibling=deepcopy(rows(h)[1]);h.service.production=True
 if delivery!='missing':h.state.replies['history-1']=Reply(status='pending' if delivery=='pending' else 'sent',content_hash='wrong' if delivery=='wrong_content' else message_hash(rendered_message(question)),expires_at=h.now+timedelta(hours=1))
 def patch(body):
  c=context(body);d=c['available_drafts'][0]
  return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
   entry(item='grab bike',item_text='grabb b ike',amount=None,entry_ref=d['entries'][0]['entry_ref'],source_refs=[c['current_source_ref']],field_sources={'item':[c['current_source_ref']],'amount':[],'date':[],'note':[]})]})
 r=h.send('ใช่ หมายถึง grab bike',patch)
 assert (r.status=='preview')==(delivery=='sent'),r
 assert rows(h)[1]==sibling and not h.sheets.entries

@pytest.mark.parametrize('extra',[{'amount':99},{'date_text':'วันนี้'},{'note':'invented'}])
def test_name_tool_normalization_never_admits_new_financial_or_note_changes(extra):
 h=harness();prepare(h,'grabb b ike 47 บาท กับ MRT 31 บาท',[entry(item='grab bike',item_text='grabb b ike',amount=47),entry(item='MRT',amount=31)])
 before=deepcopy(rows(h))
 def patch(body):
  c=context(body);d=c['available_drafts'][0];e=entry(item='grab bike',item_text='grabb b ike',amount=None,entry_ref=d['entries'][0]['entry_ref'],source_refs=[c['current_source_ref']]);e.update(extra)
  return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[e]})
 r=h.send('ใช่ หมายถึง grab bike',patch)
 assert r.status!='preview' and rows(h)==before and not h.sheets.entries

@pytest.mark.parametrize('text,quoted',[('bts2ขา59กับ65','bts2'),('BTS 2 รอบ 59 กับ 65','BTS'),('BTS 2 ขา ขาละ 41 บาท','BTS')])
def test_declared_count_and_complete_original_prices_recover_model_null_amount_without_guessing(text,quoted):
 h=harness();r=prepare(h,text,[entry(item=quoted,amount=None)])
 expected=['41','41'] if 'ขาละ' in text else ['59','65']
 assert r.status=='preview' and [e['amount'] for e in rows(h)]==expected,r
 assert all(e['item']=='BTS' and e['amount_occurrence']['span'] for e in rows(h))
 save_replay(h,r,2)

@pytest.mark.parametrize('text',['BTS 3 ขา 59 กับ 65','BTS 2 ขา 59 บาท','BTS 2 ขา รวม 124 บาท','BTS 2 ขา 59 กับ -65'])
def test_missing_model_amount_cannot_infer_absent_prices_from_ambiguous_original_count(text):
 h=harness();r=prepare(h,text,[entry(item='BTS',amount=None)])
 assert r.status!='preview' and not h.sheets.entries


@pytest.mark.parametrize('proposed',[None,59,65])
def test_complete_explicit_counted_source_preserves_all_original_prices_from_one_consistent_model_row(proposed):
 h=harness();r=prepare(h,'BTS2ขา59กับ65',[entry(item='BTS',amount=proposed)])
 assert r.status=='preview' and [e['amount'] for e in rows(h)]==['59','65'],r
 save_replay(h,r,2)

@pytest.mark.parametrize('text,proposed',[('BTS2ขา59กับ65',99),('BTS2ขา59กับ65',124),('BTS59กับ65',59)])
def test_counted_source_completion_never_overwrites_wrong_model_prices_or_assumes_missing_quantity(text,proposed):
 h=harness();r=prepare(h,text,[entry(item='BTS',amount=proposed)])
 assert r.status!='preview' and not h.sheets.entries

@pytest.mark.parametrize('proposed',[2,'2.0'])
def test_model_count_value_in_money_field_uses_only_the_independently_verified_price_roles(proposed):
 h=harness();r=prepare(h,'BTS2ขา59กับ65',[entry(item='BTS',amount=proposed)])
 assert r.status=='preview' and [e['amount'] for e in rows(h)]==['59','65'],r
 assert all(e['amount']!='2' for e in rows(h))
 save_replay(h,r,2)


def test_complete_counted_equal_prices_use_two_original_spans_instead_of_inventing_a_per_unit_rate():
 h=harness();r=prepare(h,'BTS 2 ขา 41 กับ 41 บาท',[entry(item='BTS',amount=41)])
 assert r.status=='preview' and [e['amount'] for e in rows(h)]==['41','41'],r
 assert len({tuple(e['amount_occurrence']['span']) for e in rows(h)})==2
 assert {e['amount_occurrence']['ordinal'] for e in rows(h)}=={0}
 save_replay(h,r,2)
