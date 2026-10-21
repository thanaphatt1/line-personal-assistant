"""Original owner text, canonical labels and source offsets; no live inference."""
from copy import deepcopy
from datetime import datetime
import socket
import pytest
from test_expense_occurrences_v07 import entry,prepare
from test_history_first import response
from test_raw_history import Harness,context
from line_assistant.history_context import source_ref
from line_assistant.source_spans import finditer,contains

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No network or paid provider access')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def harness(*names):
    h=Harness();h.now=datetime.fromisoformat('2026-10-16T03:04:00+00:00')
    h.sheets.rows.extend((n,'Transportation','') for n in (names or ('MRT',)));return h

@pytest.mark.parametrize('surface',['mrt','Mrt','MRT','mRt'])
@pytest.mark.parametrize('proposal',['mrt','Mrt','MRT'])
@pytest.mark.parametrize('fields',[False,True])
def test_original_mrt_sentence_previews_canonical_label_and_yesterday(surface,proposal,fields):
    h=harness();text=f'เพิ่ม {surface} 56 บาท เมื่อวาน'
    refs={f:([source_ref('history-1')] if f!='note' else []) for f in ['item','amount','date','note']}
    r=prepare(h,text,[entry(item=proposal,amount=56,date_text='เมื่อวาน',**({'field_sources':refs} if fields else {}))])
    assert r.status=='preview',r
    e=h.draft()['entries'][0];assert (e['item'],e['amount'],e['date'])==('MRT','56','2026-10-15')
    assert all(e['fact_sources'][f]==[source_ref('history-1')] for f in ['item','amount','date'])
    assert e['source_quote']==text and next(v['text'] for v in h.state.history if v['role']=='user')==text
    assert not h.sheets.entries and len(h.port.requests)==1

@pytest.mark.parametrize('canonical,surface',[
    ('Straße','STRASSE'),('Café','Cafe\u0301'),('Cafe\u0301','CAFÉ'),('İ','i\u0307'),('각','각'),('Bus 56','BUS 56')])
def test_unicode_catalog_names_keep_original_amount_coordinates_and_replay(canonical,surface):
    h=harness(canonical);text=f'เพิ่ม {surface} 30 บาท กับ {surface} 32 บาท เมื่อวาน'
    r=prepare(h,text,[entry(item=canonical,amount=n,date_text='เมื่อวาน') for n in [30,32]])
    assert r.status=='preview',r
    rows=h.draft()['entries'];assert [e['item'] for e in rows]==[canonical,canonical]
    for e in rows:
        a,b=e['amount_occurrence']['span'];assert text[a:b]==e['amount']
        assert e['amount_occurrence']['source_ref']==source_ref('history-1')
        assert e['date']=='2026-10-15'
    assert len({e['amount_occurrence']['occurrence_ref'] for e in rows})==2
    event=h.event(r.data['confirm_command']);saved=h.service.handle(event)
    assert saved.status=='recorded' and h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==2

@pytest.mark.parametrize('text,name',[
    ('XMRT 56','MRT'),('MRTT 56','MRT'),('MRT_Express 56','MRT_Expresss'),
    ('İ 56','i'),('Straße 56','s'),('Cafe\u0301 56','Cafe'),('ＭＲＴ 56','MRT'),('mrt 560 56','MRT 56'),
])
def test_longer_names_partial_expansions_and_compatibility_forms_do_not_match(text,name):
    assert not contains(text,name)
    h=harness(name);r=prepare(h,'เพิ่ม '+text,[entry(item=name,amount=56)])
    assert r.status!='preview' and not h.sheets.entries

@pytest.mark.parametrize('canonical,surface',[('MRT','mrt'),('Straße','STRASSE'),('Café','Cafe\u0301')])
def test_historical_name_and_amount_use_original_ids_with_separate_current_date(canonical,surface):
    h=harness(canonical);h.send(f'{surface} 56 บาท',response(text='รอวันที่'))
    refs={'item':[source_ref('history-1')],'amount':[source_ref('history-1')],'date':[source_ref('history-2')],'note':[]}
    r=prepare(h,'เมื่อวาน',[entry(item=canonical,amount=56,date_text='เมื่อวาน',field_sources=refs)],refs=[source_ref('history-1'),source_ref('history-2')])
    assert r.status=='preview',r
    e=h.draft()['entries'][0];assert (e['item'],e['amount'],e['date'])==(canonical,'56','2026-10-15')
    assert e['fact_sources']['item']==[source_ref('history-1')] and e['fact_sources']['date']==[source_ref('history-2')]

@pytest.mark.parametrize('ref',['1','msg_invented',source_ref('history-1').upper()])
def test_case_normalization_never_applies_to_source_ids(ref):
    h=harness();r=prepare(h,'เพิ่ม mrt 56 เมื่อวาน',[entry(item='MRT',amount=56,date_text='เมื่อวาน')],refs=[ref])
    assert r.status!='preview' and not h.state.drafts and not h.sheets.entries

@pytest.mark.parametrize('values',[[30,30],[30,56]])
def test_casefold_does_not_permit_reused_or_invented_amounts(values):
    h=harness();r=prepare(h,'mrt 30 บาท\nMRT 32 บาท',[entry(item='MRT',amount=n) for n in values])
    assert r.status!='preview' and not h.sheets.entries and not h.state.drafts

@pytest.mark.parametrize('values',[[35,36],[36,35],[35,35]])
def test_normalized_same_names_keep_ordinal_update_amounts_bound_to_the_selected_rows(values):
    h=harness();assert prepare(h,'mrt 30 บาท\nMRT 32 บาท',[entry(item='mrt',amount=30),entry(item='MRT',amount=32)]).status=='preview'
    d=h.draft();before=deepcopy(d['entries'])
    def call(body):
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
            entry(item='mrt',amount=n,entry_ref=before[i]['entry_ref'],source_refs=[context(body)['current_source_ref']]) for i,n in enumerate(values)]})
    r=h.send('แก้รอบหนึ่ง mrt 35 บาท และรอบสอง MRT 36 บาท',call,response(text='ขอข้อมูลเพิ่ม'))
    if values==[35,36]:assert r.status=='preview' and [e['amount'] for e in h.draft()['entries']]==['35','36']
    else:assert r.status!='preview' and h.draft()['entries']==before
    assert not h.sheets.entries

def test_case_only_canonicalization_of_existing_certified_label_keeps_old_item_proof():
    h=harness();assert prepare(h,'mrt 56 บาท เมื่อวาน',[entry(item='MRT',amount=56,date_text='เมื่อวาน')]).status=='preview'
    d=h.draft();d['entries'][0]['item']='mrt';d['review']=None;h.service._seal(d)
    proof=deepcopy(d['entries'][0]['fact_sources']['item'])
    def call(body):
        return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[
            entry(item=None,amount=60,entry_ref=d['entries'][0]['entry_ref'],source_refs=[context(body)['current_source_ref']])]})
    r=h.send('แก้ยอดเป็น 60 บาท',call,response(text='ขอข้อมูลเพิ่ม'))
    assert r.status=='preview',r
    assert h.draft()['entries'][0]['item']=='MRT' and h.draft()['entries'][0]['fact_sources']['item']==proof

def test_canonical_catalog_casing_is_retained_when_catalog_itself_is_lowercase():
    h=harness('mrt');r=prepare(h,'เพิ่ม MRT 56 บาท เมื่อวาน',[entry(item='MRT',amount=56,date_text='เมื่อวาน')])
    assert r.status=='preview' and h.draft()['entries'][0]['item']=='mrt'

def test_known_label_with_number_and_relative_date_word_does_not_supply_amount_or_date():
    h=harness('MRT 56 วันนี้');r=prepare(h,'mrt 56 วันนี้ 30 บาท เมื่อวาน',[entry(item='MRT 56 วันนี้',amount=30,date_text='เมื่อวาน')])
    assert r.status=='preview',r
    e=h.draft()['entries'][0];assert (e['amount'],e['date'])==('30','2026-10-15')

def test_failed_item_source_retains_independently_cited_date_and_amount():
    h=harness();refs={f:([source_ref('history-1')] if f!='note' else []) for f in ['item','amount','date','note']}
    r=prepare(h,'เครื่องดื่ม 56 บาท เมื่อวาน',[entry(item='Tea',amount=56,date_text='เมื่อวาน',field_sources=refs)])
    assert r.status=='clarification';e=h.draft()['entries'][0]
    assert (e['item'],e['amount'],e['date'])==(None,'56','2026-10-15')
    assert 'จำนวนเงิน' not in r.message and 'ไม่ต้องส่งประโยคเดิมซ้ำ' not in r.message
    assert '56' in r.message and '2026-10-15' in r.message

def test_internal_failure_cannot_echo_model_advice_to_repeat_the_same_sentence():
    h=harness()
    def call(body):
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[entry(item='MRT',amount=56,source_refs=['invented'])]})
    r=h.send('เพิ่ม mrt 56 บาท เมื่อวาน',call,response(text='กรุณาส่ง เพิ่ม MRT 56 บาท เมื่อวาน อีกครั้ง'))
    assert r.status=='clarification' and 'ไม่ต้องส่งประโยคเดิมซ้ำ' not in r.message and 'กรุณาส่ง' not in r.message
    assert 'อ้างอิงข้อความต้นทาง' in r.message
    assert not h.state.drafts and not h.sheets.entries

def test_partial_validation_reply_shows_verified_facts_and_only_requests_failed_field():
    h=harness()
    def call(body):
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[entry(item='MRT',amount=56,date_text='วันที่ 14',source_refs=[context(body)['current_source_ref']])]})
    r=h.send('เพิ่ม mrt 56 บาท เมื่อวาน',call,response(text='กรุณาส่ง เพิ่ม MRT 56 บาท เมื่อวาน อีกครั้ง'))
    assert r.status=='clarification' and 'MRT 56 บาท' in r.message and 'เฉพาะข้อมูลที่ยังตรวจสอบไม่ได้: วันที่' in r.message
    assert 'กรุณาส่ง' not in r.message and h.draft()['review'] is None and not h.sheets.entries

def test_source_spans_cover_whole_original_unicode_sequences():
    text='เพิ่ม STRASSE 30 และ Cafe\u0301 32'
    for name,original in [('Straße','STRASSE'),('Café','Cafe\u0301')]:
        m,=finditer(text,name);assert text[m.start():m.end()]==original

def test_distinct_normalized_labels_keep_their_own_inline_amounts():
    h=harness('Café','MRT')
    r=prepare(h,'เมื่อวาน Cafe\u0301 30 บาท และ mrt 56 บาท',[entry(item='Café',amount=30,date_text='เมื่อวาน'),entry(item='MRT',amount=56,date_text='เมื่อวาน')])
    assert r.status=='preview',r
    assert [(e['item'],e['amount'],e['date']) for e in h.draft()['entries']]==[('Café','30','2026-10-15'),('MRT','56','2026-10-15')]

def test_note_boundary_cannot_start_inside_a_normalized_catalog_name():
    from line_assistant.domain_notes import section
    assert section('galaxy note 56 หมายเหตุ: เก็บ Note ไว้','Galaxy Note 56')==('เก็บ Note ไว้',True)
