"""Synthetic owner dialogues; deterministic server regressions, no model claims."""
from copy import deepcopy
from datetime import datetime,timedelta
import json,socket
import pytest
from fastapi.testclient import TestClient
from test_production_history import Hosted
from test_raw_history import Harness,context
from test_catalog_names_v09 import entry,prepare
from test_history_first import response
from line_assistant.history_dates import cues
from line_assistant.history_context import source_ref
from line_assistant.taxonomy import Taxonomy,identifier

CATEGORIES=['Transportation','Entertainment','Utilities','Gadget','Food/Grocery','Work/Education','Gift','Financial']
KNOWN=['ร้านทดสอบโคเมต','เครื่องดื่มทดสอบดาว','ค่าดูแลทดสอบ']
NEW='ขนมทดสอบดารา'

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No paid calls, production access or real expense replay')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def harness():
    h=Harness();h.now=datetime.fromisoformat('2026-10-16T10:00:00+00:00')
    h.sheets.rows=[(f'ชื่อทดสอบหมวด{i}',category,'') for i,category in enumerate(CATEGORIES)]
    h.sheets.rows.extend((name,'Food/Grocery' if i<2 else 'Utilities','') for i,name in enumerate(KNOWN))
    return h

def active(h):return next(d for d in h.state.drafts.values() if d['status']!='recorded')

def enroll(h,text,*,step='begin_new_item',category=None,subcategory=None,version=None,entry_ref=None):
    d=deepcopy(active(h))
    def call(body):
        c=context(body);assert c['available_drafts']
        schema=next(t for t in body['tools'] if t['name']=='prepare_item_enrollment')['parameters']['properties']
        assert 'Food/Grocery' in schema['category']['enum']
        return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':version if version is not None else d['version'],
            'entry_ref':entry_ref or d['entries'][0]['entry_ref'],'category':category,'subcategory':subcategory,'existing_item':None,'resolution_step':step})
    return h.send(text,call,response(text='synthetic fallback'))

def confirm(h,r):
    assert r.status=='preview' and not active(h)['missing_fields']
    count=len(h.sheets.entries);calls=len(h.port.requests);event=h.event(r.data['confirm_command'])
    saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==count+1 and len(h.port.requests)==calls
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==count+1
    return saved

@pytest.mark.parametrize('weekday,expected',[('จันทร์','2026-10-12'),('อังคาร','2026-10-13'),('พุธ','2026-10-14'),('พฤหัส','2026-10-15'),('พฤหัสบดี','2026-10-15'),('ศุกร์','2026-10-09'),('เสาร์','2026-10-10'),('อาทิตย์','2026-10-11')])
@pytest.mark.parametrize('suffix',['ที่ผ่านมา','ที่แล้ว','ก่อน'])
def test_past_weekdays_are_calendar_dates_from_original_bangkok_turn(weekday,expected,suffix):
    h=harness();word='วัน'+weekday+suffix;text=f'{KNOWN[0]} {word} 47 บาท'
    r=prepare(h,text,[entry(item=KNOWN[0],amount=47,date_text=word)])
    assert r.status=='preview',r
    e=active(h)['entries'][0]
    assert (e['item'],e['amount'],e['date'])==(KNOWN[0],'47',expected)
    assert e['source_quote']==text and e['fact_sources']['date']==[source_ref('history-1')]
    assert e['date_basis']['defaulted_parts']=={} and not h.sheets.entries

@pytest.mark.parametrize('at,text,expected',[
    ('2026-01-01T00:00:00+00:00','วันพุธที่ผ่านมา','2025-12-31'),
    ('2026-10-14T16:59:00+00:00','เมื่อวันพุธที่ผ่านมา','2026-10-07'),
    ('2026-10-14T17:00:00+00:00','เมื่อวันพุธที่ผ่านมา','2026-10-14'),
    ('2024-03-01T00:00:00+00:00','วันพฤหัสบดีที่ผ่านมา','2024-02-29')])
def test_weekday_calendar_edges_and_original_span(at,text,expected):
    at=datetime.fromisoformat(at);parts,spans,issue=cues(text,at)
    assert issue is None and datetime(**parts).date().isoformat()==expected
    assert [text[a:b] for a,b in spans]==[text]

@pytest.mark.parametrize('text',['วันพุธหน้า','วันพุธ','วันพุธของสัปดาห์ที่แล้ว','วันนี้ วันพุธที่ผ่านมา','วันที่ 13 วันพุธที่ผ่านมา'])
def test_ambiguous_unsupported_or_conflicting_weekday_never_gets_review(text):
    h=harness();r=prepare(h,f'{KNOWN[0]} 47 บาท {text}',[entry(item=KNOWN[0],amount=47,date_text=text)])
    assert r.status!='preview' and not h.sheets.entries and not h.state.drafts

@pytest.mark.parametrize('name',['ร้านวันพุธที่ผ่านมา','ช่วยด้วย','ขนมวันศุกร์ก่อน','วันพุธที่ผ่านมา'])
def test_date_and_additive_words_inside_full_catalog_name_are_not_removed(name):
    h=harness();h.sheets.rows.append((name,'Food/Grocery',''))
    r=prepare(h,name+' 43 บาท',[entry(item=name,amount=43)])
    assert r.status=='preview',r
    e=active(h)['entries'][0];assert e['item']==name and e['date']=='2026-10-16' and not h.sheets.entries

@pytest.mark.parametrize('prior',['none','yesterday','today','other_topic','midnight'])
@pytest.mark.parametrize('tail',['ด้วย','ด้วยครับ','ด้วย ค่ะ'])
def test_additive_complete_expense_defaults_today_without_reusing_old_date(prior,tail):
    h=harness()
    if prior!='none':
        date='วันนี้' if prior=='today' else 'เมื่อวาน'
        r=prepare(h,f'{KNOWN[0]} 47 บาท {date}',[entry(item=KNOWN[0],amount=47,date_text=date)]);confirm(h,r)
        if prior=='other_topic':h.send('เรื่องอื่นชั่วคราว',response(text='synthetic scope reply'))
        if prior=='midnight':h.now+=timedelta(hours=8)
    r=prepare(h,f'{KNOWN[2]} 31 บาท{tail}',[entry(item=KNOWN[2],amount=31)])
    assert r.status=='preview',r
    e=active(h)['entries'][0]
    assert e['date']==('2026-10-17' if prior=='midnight' else '2026-10-16')
    assert e['date_basis']['policy']=='bangkok-date-defaults-v02' and 'continued_date' not in e['date_basis']
    confirm(h,r)

@pytest.mark.parametrize('word',['เมื่อวาน','วันพุธที่ผ่านมา','วันที่ 13'])
def test_explicit_date_still_wins_over_additive_word(word):
    h=harness();r=prepare(h,f'{KNOWN[2]} 31 บาท {word} ด้วย',[entry(item=KNOWN[2],amount=31,date_text=word)])
    assert r.status=='preview' and active(h)['entries'][0]['date']=={'เมื่อวาน':'2026-10-15','วันพุธที่ผ่านมา':'2026-10-14','วันที่ 13':'2026-10-13'}[word]

def test_explicit_same_date_continuation_still_requires_verified_confirmed_context():
    h=harness();r=prepare(h,f'{KNOWN[0]} 47 บาท เมื่อวาน',[entry(item=KNOWN[0],amount=47,date_text='เมื่อวาน')]);confirm(h,r)
    r=prepare(h,f'{KNOWN[2]} 31 บาท วันเดิมด้วย',[entry(item=KNOWN[2],amount=31)])
    assert r.status=='preview' and active(h)['entries'][0]['date']=='2026-10-15'
    assert active(h)['entries'][0]['date_basis']['policy']=='bangkok-date-continuation-v01'
    h=harness();r=prepare(h,f'{KNOWN[2]} 31 บาท วันเดิมด้วย',[entry(item=KNOWN[2],amount=31)])
    assert r.status=='clarification' and active(h)['entries'][0]['date'] is None and not h.sheets.entries

@pytest.mark.parametrize('text',['เพิ่มในหมวดอาหาร','รายการใหม่ เพิ่มใน Food','เพิ่มใน Food/Grocery ครับ','ใส่ในหมวด Food','รายการใหม่, add in Food'])
@pytest.mark.parametrize('step,category',[('begin_new_item',None),('begin_new_item','Food/Grocery'),('choose_category','Food/Grocery')])
def test_compound_owner_category_answer_goes_directly_to_review(text,step,category):
    h=harness();r=prepare(h,f'{NEW} 67 บาท วันที่ 13',[entry(item=NEW,amount=67,date_text='วันที่ 13')])
    assert r.status=='clarification';old=deepcopy(active(h));e=old['entries'][0]
    r=enroll(h,text,step=step,category=category)
    assert r.status=='preview',r
    d=active(h);current=d['entries'][0]
    assert d['version']==old['version']+1 and d['review'] and not d['missing_fields']
    assert current['mapping']=={'item':NEW,'category':'Food/Grocery','subcategory':''}
    assert all(current.get(k)==e.get(k) for k in ['item','amount','date','note','source_quote','source_refs','fact_sources','amount_occurrence'])
    assert not h.sheets.entries and not any(row[0]==NEW for row in h.sheets.rows)
    confirm(h,r);assert (NEW,'Food/Grocery','') in h.sheets.rows

@pytest.mark.parametrize('text',['รายการใหม่','ไม่เอาหมวดอาหาร','รายการใหม่ ไม่เพิ่มใน Food','เขาบอกว่าเพิ่มในหมวดอาหาร','"เพิ่มในหมวดอาหาร"','สวัสดี','เพิ่มในหมวดที่ไม่มี','รายการใหม่ เพิ่มใน Food หรือ Gift'])
def test_unselected_quoted_negated_or_ambiguous_category_cannot_be_invented(text):
    h=harness();prepare(h,f'{NEW} 67 บาท',[entry(item=NEW,amount=67)]);before=deepcopy(h.state.drafts)
    r=enroll(h,text,category='Food/Grocery')
    assert r.status!='preview' and h.state.drafts==before and not h.sheets.entries

def test_ambiguous_translated_food_alias_asks_real_category_without_guessing():
    h=harness();h.sheets.rows.append(('ชื่อหมวดทดสอบอีกชื่อ','Food/Drink',''))
    prepare(h,f'{NEW} 67 บาท',[entry(item=NEW,amount=67)])
    r=enroll(h,'เพิ่มในหมวดอาหาร')
    assert r.status=='clarification' and active(h)['entries'][0]['enrollment']['category_id'] is None
    assert active(h)['review'] is None and not h.sheets.entries

def test_owner_category_cannot_be_changed_to_another_offered_model_proposal():
    h=harness();prepare(h,f'{NEW} 67 บาท',[entry(item=NEW,amount=67)]);before=deepcopy(h.state.drafts)
    r=enroll(h,'เพิ่มในหมวดอาหาร',category='Gift')
    assert r.status!='preview' and h.state.drafts==before and not h.sheets.entries

def test_subcategory_is_asked_only_when_the_selected_real_category_has_multiple_pairs():
    h=harness();h.sheets.rows=[r for r in h.sheets.rows if r[1]!='Food/Grocery']+[(KNOWN[0],'Food/Grocery','Bakery'),(KNOWN[1],'Food/Grocery','Dessert')]
    prepare(h,f'{NEW} 67 บาท',[entry(item=NEW,amount=67)])
    r=enroll(h,'รายการใหม่ เพิ่มใน Food')
    assert r.status=='clarification' and active(h)['entries'][0]['enrollment']['category_id']==identifier('cat','Food/Grocery')
    assert active(h)['entries'][0]['enrollment']['subcategory_id'] is None and active(h)['review'] is None
    r=enroll(h,'Bakery',step='choose_subcategory',subcategory='Bakery');assert r.status=='preview'

def test_stale_version_and_foreign_entry_cannot_apply_a_valid_compound_answer():
    for changes in [{'version':0},{'entry_ref':'entry_foreign'}]:
        h=harness();prepare(h,f'{NEW} 67 บาท',[entry(item=NEW,amount=67)]);before=deepcopy(h.state.drafts)
        r=enroll(h,'รายการใหม่ เพิ่มใน Food',**changes)
        assert r.status!='preview' and h.state.drafts==before and not h.sheets.entries

def test_continuous_five_expense_dialogue_preserves_prior_saves_and_no_redundant_category_turn():
    h=harness();history=[]
    for name,amount,word,reply in [(KNOWN[0],47,'วันพุธที่ผ่านมา',None),(NEW,67,'วันที่ 13','เพิ่มในหมวดอาหาร'),
        (KNOWN[1],39,'วันนี้',None),(KNOWN[2],31,'ด้วย',None),('อาหารทดสอบไลรา',41,'','รายการใหม่ เพิ่มใน Food')]:
        text=f'{name} {word} {amount} บาท' if word=='วันพุธที่ผ่านมา' else f'{name} {amount} บาท {word}'.strip()
        r=prepare(h,text,[entry(item=name,amount=amount,date_text=word if word not in {'','ด้วย'} else None)])
        if reply:
            assert r.status=='clarification';r=enroll(h,reply)
        assert r.status=='preview' and len(h.sheets.entries)==len(history)
        history.append(deepcopy(active(h)['entries'][0]));confirm(h,r)
        assert len(h.sheets.entries)==len(history)
        for old in history:
            certified=[e for d in h.state.drafts.values() for e in d['entries'] if e['entry_ref']==old['entry_ref']]
            assert len(certified)==1 and certified[0]==old
    assert [e['date'] for e in history]==['2026-10-14','2026-10-13','2026-10-16','2026-10-16','2026-10-16']
    assert len(h.port.requests)==7 and h.sheets.atomic_batches==2

def test_signed_factory_continuous_dialogue_survives_recreation_and_confirm_replay():
    api=Hosted();api.now=datetime.fromisoformat('2026-10-16T10:00:00+00:00')
    api.google.cells={k:v for k,v in api.google.cells.items() if k[0]!='Database' or k[1]==1}
    for row,values in enumerate(harness().sheets.rows,2):
        for col,value in enumerate(values,1):api.google.cells[('Database',row,col)]=value
    expected_dates=[];replays=[]
    turns=[(KNOWN[0],47,'วันพุธที่ผ่านมา',None),(NEW,67,'วันที่ 13','เพิ่มในหมวดอาหาร'),
           (KNOWN[1],39,'วันนี้',None),(KNOWN[2],31,'ด้วย',None),('อาหารทดสอบไลรา',41,'','รายการใหม่ เพิ่มใน Food')]
    for index,(name,amount,word,answer) in enumerate(turns):
        def proposed(body):
            ref=context(body)['current_source_ref']
            return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,
                'entries':[entry(item=name,amount=amount,date_text=word if word not in {'','ด้วย'} else None,source_refs=[ref])]})
        with TestClient(api.app(raw_history_enabled=True)) as client:
            r,_=api.send_text(client,f'{name} {word} {amount} บาท' if index==0 else f'{name} {amount} บาท {word}'.strip(),proposed,response(text='synthetic fallback'))
        if answer:
            def chosen(body):
                d=context(body)['available_drafts'][0]
                return response(name='prepare_item_enrollment',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],
                    'entry_ref':d['entries'][0]['entry_ref'],'resolution_step':'begin_new_item','category':None,'subcategory':None,'existing_item':None})
            with TestClient(api.app(raw_history_enabled=True)) as client:r,_=api.send_text(client,answer,chosen)
        assert r.status=='preview' and api.google.sheet_writes==index
        expected_dates.append(r.data['tool_result']['entries'][0]['date'])
        calls=api.models
        with TestClient(api.app(raw_history_enabled=True)) as client:
            saved,event=api.send_text(client,r.data['confirm_command']);replays.append(event)
            assert saved.status=='recorded' and api.models==calls and api.google.sheet_writes==index+1
        with TestClient(api.app(raw_history_enabled=True)) as client:
            for old in replays:assert api.send(client,deepcopy(old)).status_code==200
            assert api.google.sheet_writes==index+1 and api.models==calls
    assert api.models==7 and expected_dates==['2026-10-14','2026-10-13','2026-10-16','2026-10-16','2026-10-16']
    bodies=api.model_bodies
    assert all(body['store'] is False and body['parallel_tool_calls'] is False for body in bodies)
    assert any(row.get('text')=='เพิ่มในหมวดอาหาร' for row in context(bodies[-1])['history'])
