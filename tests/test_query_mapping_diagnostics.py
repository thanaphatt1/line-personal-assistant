"""Synthetic identity/data-quality regressions; no owner records or network."""
from copy import deepcopy
from datetime import date
import json
import pytest
from line_assistant.catalog import ItemCatalog
from line_assistant.contracts import Transaction
from line_assistant.transport import ApiError,NotReady
from test_calendar_mvp import Harness
from test_read_queries import plan

def setup():
    h=Harness();h.sheets.rows=[('Shuttle Pass','Transportation','Rail'),('Lunch Kit','Food','Meals')]
    def add(item,amount):h.sheets.entries.append(Transaction(item=item,amount=amount,date=date(2026,9,8),kind='expense'))
    add('shuttlepass','12.10');add('  SHUTTLE PASS  ','0.20');add('Lunch Kit','18.00')
    return h,add

def query(h,**kw):return h.send('หมวดเดินทางเดือนกันยายน',plan(start='2026-09-01',end='2026-09-30',kind='category',value='Transportation',**kw))

def test_read_uses_same_unique_spacing_identity_as_writes_without_changing_records():
    h,_=setup();before=deepcopy(h.sheets.entries);r=query(h,listing=True)
    assert r.data['tool_result']['complete'] and r.data['tool_result']['expense']=='12.30'
    assert [e['item'] for e in r.data['tool_result']['entries']]==['SHUTTLE PASS','shuttlepass']
    assert h.sheets.entries==before and not h.sheets.write_log and not h.state.actions and not h.state.drafts

def test_missing_names_are_actionable_and_subtotal_is_never_a_complete_total():
    h,add=setup();add('Unlisted Token','7.10');add('Unlisted Token','2.20');r=query(h)
    tool=r.data['tool_result'];d=tool['mapping_diagnostics']
    assert tool['error_code']=='mapping_incomplete' and tool['input_read_complete'] and not tool['complete']
    assert 'expense' not in tool and tool['partial']['expense']=='12.30' and tool['partial']['count']==2
    assert d['unresolved_item_count']==1 and d['unresolved_record_count']==2 and d['unresolved_expense']=='9.30'
    assert d['items']==[{'item':'Unlisted Token','reason':'missing_catalog_item','count':2,'expense':'9.30','candidate_items':[],'candidate_count':0,'may_affect_filter':True}]
    assert 'ยอดบางส่วน' in r.message and 'Unlisted Token' in r.message and '9.30 บาท' in r.message and 'Database' in r.message
    assert 'อาจอยู่ในหมวด' in r.message and 'ยังไม่ได้แก้ข้อมูล' in r.message
    assert not h.sheets.write_log and not h.state.actions and not h.state.drafts and not h.provider.posts

def test_zero_known_subtotal_does_not_claim_zero_category_expenses():
    h,add=setup();h.sheets.entries=[];add('Unlisted Ticket','31.00');r=query(h)
    assert r.data['tool_result']['partial']['expense']=='0.00' and not r.data['tool_result']['complete']
    assert 'ยอดบางส่วน' in r.message and 'ยังยืนยันยอดครบไม่ได้' in r.message
    assert 'ไม่มีรายจ่าย' not in r.message and 'Unlisted Ticket' in r.message

def test_likely_typo_is_not_a_stored_identity_or_inferred_category():
    h,add=setup();add('Shutle Pass','13.00');r=query(h)
    assert r.data['tool_result']['partial']['expense']=='12.30'
    assert r.data['tool_result']['mapping_diagnostics']['items'][0]['reason']=='missing_catalog_item'
    assert ItemCatalog(['Shuttle Pass'],{}).resolve('Shutle Pass').status=='possible_typo'
    assert ItemCatalog(['Shuttle Pass'],{}).identity('Shutle Pass').status=='unknown'

def test_ambiguous_spacing_remains_unresolved_but_exact_identity_has_priority():
    h,add=setup();h.sheets.rows.append(('ShuttlePass','Food','Snacks'));h.sheets.entries=[]
    add('ShuttlePass','5.00');add('Shuttle Pass','6.00');add('Shuttle  Pass','7.00');r=query(h)
    tool=r.data['tool_result'];issue=tool['mapping_diagnostics']['items'][0]
    assert tool['partial']['expense']=='6.00' and issue['reason']=='ambiguous_item_identity'
    assert issue['candidate_items']==['Shuttle Pass','ShuttlePass'] and issue['expense']=='7.00'
    assert 'ตัวเลือกที่ต้องตรวจ' in r.message and not h.sheets.write_log

def test_item_filter_uses_safe_identity_without_fuzzy_merging():
    h,add=setup();add('Shutle Pass','13.00')
    r=h.send('รายการชื่อนี้เดือนกันยายน',plan(start='2026-09-01',end='2026-09-30',kind='item',value='Shuttle Pass'))
    assert r.data['tool_result']['complete'] and r.data['tool_result']['expense']=='12.30'

def test_blank_category_is_actionable_but_optional_blank_subcategory_is_not_schema_failure():
    h,add=setup();h.sheets.rows.append(('Unassigned Entry','',''));add('Unassigned Entry','8.00')
    r=query(h);assert r.data['tool_result']['mapping_diagnostics']['items'][0]['reason']=='unassigned_category'
    h.sheets.rows[-1]=('Unassigned Entry','Food','');r=query(h)
    assert r.data['tool_result']['complete'] and r.data['tool_result']['expense']=='12.30'

def test_unfiltered_total_does_not_depend_on_catalog_completeness():
    h,add=setup();add('Unlisted Token','1.50')
    r=h.send('รายจ่ายทั้งหมดเดือนกันยายน',plan(start='2026-09-01',end='2026-09-30'))
    assert r.data['tool_result']['complete'] and r.data['tool_result']['expense']=='31.80'

@pytest.mark.parametrize('failure,code',[(ApiError(403),'provider_failure'),(NotReady('synthetic malformed row'),'schema_failure')])
def test_incomplete_provider_or_schema_read_never_returns_a_partial_subtotal(monkeypatch,failure,code):
    h,_=setup()
    def fail(*a):raise failure
    monkeypatch.setattr(h.sheets,'saved_entries',fail);r=query(h)
    assert r.data['tool_result']['error_code']==code and 'partial' not in r.data['tool_result']

def test_diagnostics_are_bounded_and_disclose_omitted_names():
    h,add=setup()
    for i in range(23):add('Synthetic unresolved '+str(i),'1.00')
    r=query(h);d=r.data['tool_result']['mapping_diagnostics']
    assert d['unresolved_item_count']==23 and d['unresolved_record_count']==23 and len(d['items'])==20
    assert len([k for k in r.data['query_facts'] if k.startswith('row')])==10
    assert '10 ชื่อแรก' in r.message and '23 รายการ' in r.message
    assert len(r.message.encode('utf-16-le'))<=9000

def test_line_breaks_are_not_joined_into_a_stored_identity():
    h,add=setup();add('Shuttle\nPass','5.00');r=query(h)
    assert r.data['tool_result']['mapping_diagnostics']['items'][0]['item']=='Shuttle\nPass'
    assert 'Shuttle\nPass' not in r.message and 'Shuttle Pass' in r.message

def test_partial_qualifiers_and_all_diagnostic_rows_are_immutable_style_facts():
    from line_assistant.response_realization import body_for,parse
    h,add=setup();add('Unlisted Token','7.00');r=query(h);h._active_event_id='synthetic-style'
    body=body_for(h,r);payload=json.loads(body['input'][0]['content'])
    assert 'mapping_partial' in payload['facts'] and payload['locked_rows']
    parts={k:{'before':v['properties']['before']['enum'][0],'after':v['properties']['after']['enum'][0]} for k,v in body['text']['format']['schema']['properties']['parts']['properties'].items()}
    def reply(document):
        return {'status':'completed','model':body['model'],'usage':{'input_tokens':1,'output_tokens':1},'output':[{'type':'message','status':'completed','content':[{'type':'output_text','text':json.dumps(document,ensure_ascii=False)}]}]}
    text=parse(reply({'opening':'','parts':parts}),body)
    assert 'ยอดบางส่วน' in text and 'Unlisted Token' in text and '\nโปรดตรวจ' in text
    del parts['mapping_partial']
    with pytest.raises(ValueError):parse(reply({'opening':'','parts':parts}),body)
