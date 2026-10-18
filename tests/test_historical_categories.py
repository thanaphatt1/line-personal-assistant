"""Signed historical scans/corrections/consent over synthetic providers only."""
from copy import deepcopy
from datetime import date,timedelta
import json,socket
import httpx,pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from line_assistant.history_cloud_run import create_history_cloud_run_app,HistoryConfig
from line_assistant.runtime_config import ConfigError
from line_assistant.sheet_dates import date_to_serial
from line_assistant.history_context import digest
from line_assistant.pilot_http import rpc_budget
from test_provisional_categories import LabelHosted
from test_deployment_configuration import TARGET,ConfiguredHosted
from test_calendar_factory import values
from test_calendar_mvp import response
from test_saved_record_workflow import SavedHosted,find as find_saved,prepare as prepare_saved

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Historical tests cannot connect to a network')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

class HistoricalHosted(LabelHosted):
    def __init__(self):
        self.historical=True
        super().__init__()
    def app(self,enabled=True):
        flags={'PILOT_HISTORICAL_CATEGORIES_ENABLED':str(self.historical).lower(),'PILOT_PROVISIONAL_CATEGORIES_ENABLED':str(self.labels).lower(),'PILOT_PROVISIONAL_MAPPING_ENABLED':str(self.mapping).lower(),'PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY':'reviewed_single_writer' if self.mapping else 'off','PILOT_RESPONSE_REALIZATION_ENABLED':str(self.voice).lower()}
        return create_history_cloud_run_app({**values(enabled),**TARGET,**flags},transport=httpx.MockTransport(self.handle),secret_loader=lambda name:SecretStr('synthetic-'+name),clock=lambda:self.now)
    def many(self,n=23):
        for k in list(self.google.cells):
            if k[0]=='All Expenses' and k[1]>1:del self.google.cells[k]
        for i in range(n):
            for col,value in enumerate([date_to_serial(date(2024 if i==0 else 2026,11,5)),f'Unmapped transit sample {i:02d}',i+1,'preserved',f'synthetic_historical_{i}','=C'+str(i+2)+'*2'],1):self.google.cells[('All Expenses',i+2,col)]=value

class FullHistoricalHosted(HistoricalHosted,SavedHosted):
    def app(self,enabled=True):
        flags={'PILOT_HISTORICAL_CATEGORIES_ENABLED':str(self.historical).lower(),'PILOT_PROVISIONAL_CATEGORIES_ENABLED':str(self.labels).lower(),'PILOT_PROVISIONAL_MAPPING_ENABLED':str(self.mapping).lower(),'PILOT_SAVED_EXPENSE_EDITS_ENABLED':str(self.edit_flags).lower(),'PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED':str(self.edit_flags).lower(),'PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY':'reviewed_single_writer','PILOT_RESPONSE_REALIZATION_ENABLED':str(self.voice).lower()}
        return create_history_cloud_run_app({**values(enabled),**TARGET,**flags},transport=httpx.MockTransport(self.handle),secret_loader=lambda name:SecretStr('synthetic-'+name),clock=lambda:self.now)
    def handle(self,request):
        if self.configured and request.url.host=='www.googleapis.com' and request.method=='POST':return ConfiguredHosted.handle(self,request)
        if self.configured and request.url.host=='sheets.googleapis.com' and request.method=='POST':
            body=json.loads(request.content)
            if any(r.get('appendCells',{}).get('sheetId')==501 for r in body.get('requests',[])):
                for block in body['requests']:
                    if 'appendCells' in block:block['appendCells']['sheetId']={501:101,502:102}.get(block['appendCells']['sheetId'],block['appendCells']['sheetId'])
                translated=httpx.Request(request.method,request.url,headers=request.headers,json=body)
                return ConfiguredHosted.handle(self,translated)
        return super().handle(request)

def turn(api,text,*answers):
    with TestClient(api.app()) as web:return api.turn(web,text,*answers)
def context(body):return json.loads(body['input'][0]['content'])
def scan(body):
    c=context(body)
    return response('scan_historical_categories',{'mode':'all','start_date':None,'end_date':None,'period_text':None,'relative_offset':None,'source_refs':[c['current_source_ref']]})
def propose(body):
    c=context(body);h=c['historical_categories'];rows=[]
    for g in h['groups']:
        ambiguous=g['item']=='Mystery Token'
        rows.append({'group_ref':g['group_ref'],'status':'ambiguous' if ambiguous else 'proposed','category':None if ambiguous else 'Transportation','subcategory':None if ambiguous else 'Rail','candidates':[{'category':'Transportation','subcategory':'Rail'},{'category':'Food','subcategory':''}] if ambiguous else []})
    return response('propose_historical_categories',{'review_ref':h['review_ref'],'proposals':rows})
def preview(api):return turn(api,'ช่วย scan จัดหมวดย้อนหลังชื่อที่ยังไม่มีหมวดทั้งหมด',scan,propose)
def review(operation='revise',selection='keep',changes=None,selected=None):
    def answer(body):
        c=context(body);h=c['historical_categories'];gs=h.get('groups',[])
        selected_refs=[g['group_ref'] for g in gs if selected and g['item'] in selected]
        rows=[]
        for item,category,subcategory in changes or []:
            g=next(g for g in gs if g['item']==item)
            rows.append({'group_ref':g['group_ref'],'status':'proposed','category':category,'subcategory':subcategory,'candidates':[],'source_text':item})
        return response('review_historical_categories',{'review_ref':h['review_ref'],'operation':operation,'changes':rows,'selection':selection,'selected_refs':selected_refs,'source_refs':[c['current_source_ref']]})
    return answer
def command(api,mode='permanent',explicit=False):
    prefix='บันทึกหมวดย้อนหลัง' if mode=='permanent' else 'ใช้ข้อเสนอย้อนหลังครั้งนี้'
    v=api.snapshot()['pending']['payload']
    return prefix+' '+v['ref']+' '+v['nonce'] if explicit else prefix

def test_complete_scan_groups_all_dates_and_only_page_is_sent_to_model():
    api=HistoricalHosted();api.many();before=deepcopy(api.google.cells)
    r,_=preview(api);out=r.data['tool_result']
    assert r.status=='preview' and out['total_names']==23 and out['shown']==10 and out['remaining_names']==13
    assert out['groups'][0]['first_date']=='2024-11-05' and '23 ชื่อ' in r.message
    assert len(api.model_bodies)==2 and not api.mapping_posts and api.google.cells==before
    assert all('synthetic_historical_' not in json.dumps(b) and 'configured-workbook' not in json.dumps(b) for b in api.model_bodies)

def test_pagination_is_not_a_hidden_ten_name_source_cap_and_previous_is_read_only():
    api=HistoricalHosted();api.many();r,_=preview(api);first=[g['group_ref'] for g in r.data['tool_result']['groups']]
    r,_=turn(api,'ขอหน้าถัดไป',review('next'),propose);out=r.data['tool_result'];second=[g['group_ref'] for g in out['groups']]
    assert out['shown']==10 and out['remaining_names']==3 and not set(first)&set(second)
    r,_=turn(api,'ขอหน้าถัดไป',review('next'),propose);assert r.data['tool_result']['shown']==3 and r.data['tool_result']['remaining_names']==0
    last=[g['group_ref'] for g in r.data['tool_result']['groups']]
    r,_=turn(api,'ขอหน้าถัดไป',review('next'));assert [g['group_ref'] for g in r.data['tool_result']['groups']]==last
    r,_=turn(api,'หน้าก่อน',review('previous'),propose);assert [g['group_ref'] for g in r.data['tool_result']['groups']]==second
    assert api.mapping_posts==api.google.sheet_writes==api.calendar.posts==0
    assert all(b['tool_choice']==({'type':'function','name':'propose_historical_categories'} if len(b['input'])>1 else 'auto') for b in api.model_bodies)

def test_duplicate_names_group_counts_dates_without_rewriting_transactions():
    api=HistoricalHosted();serial=date_to_serial(date(2025,1,2))
    for col,value in enumerate([serial,'unknown metro ticket',8,'original note','synthetic_old'],1):api.google.cells[('All Expenses',5,col)]=value
    before=deepcopy(api.google.cells);r,_=preview(api)
    group=next(g for g in r.data['tool_result']['groups'] if g['count']==2)
    assert group['expense']=='28.00' and group['first_date']=='2025-01-02' and group['last_date']=='2026-11-05'
    assert group['stored_names']==['Unknown Metro Ticket','unknown metro ticket'] and api.google.cells==before

@pytest.mark.parametrize('mode',['calendar_month','range'])
def test_optional_explicit_period_uses_existing_owner_date_validation(mode):
    api=HistoricalHosted();api.many(12)
    def dated(body):
        c=context(body);return response('scan_historical_categories',{'mode':mode,'start_date':'2026-11-05','end_date':'2026-11-05' if mode=='range' else None,'period_text':'5 พ.ย. 2026' if mode=='range' else 'พฤศจิกายน 2026','relative_offset':None,'source_refs':[c['current_source_ref']]})
    r,_=turn(api,'ช่วยจัดหมวดย้อนหลัง 5 พ.ย. 2026' if mode=='range' else 'ช่วยจัดหมวดย้อนหลังพฤศจิกายน 2026',dated,propose)
    assert r.status=='preview',r
    assert r.data['tool_result']['total_names']==11
    assert all(g['first_date'].startswith('2026-11') for g in r.data['tool_result']['groups'])

def test_natural_all_correct_except_revision_is_never_confirmation():
    api=HistoricalHosted();preview(api);old=command(api,explicit=True)
    r,_=turn(api,'ถูกหมด ยกเว้น Unknown Metro Ticket ให้เป็น Food',review(selection='all_clear',changes=[('Unknown Metro Ticket','Food','')]))
    assert r.status=='preview' and not api.mapping_posts
    groups=r.data['tool_result']['groups'];g=next(g for g in groups if g['item']=='Unknown Metro Ticket');assert g['category']=='Food' and g['included']
    assert next(g for g in groups if g['item']=='Mystery Token')['included'] is False
    stale,_=turn(api,old);assert stale.status=='stale' and not api.mapping_posts

def test_partial_selection_persists_only_displayed_selected_clear_names_once():
    api=HistoricalHosted();api.many(12);preview(api);before={k:v for k,v in api.google.cells.items() if k[0]=='All Expenses'}
    name='Unmapped transit sample 02'
    r,_=turn(api,'เอาเฉพาะ '+name,review(selection='only',selected=[name]));assert r.status=='preview'
    r,event=turn(api,command(api));assert r.status=='mapping_saved',r
    assert r.data['mapping_rows']==[{'item':name,'category':'Transportation','subcategory':'Rail'}]
    assert api.mapping_posts==1 and all(api.google.cells[k]==v for k,v in before.items())
    with TestClient(api.app()) as web:assert api.send(web,deepcopy(event)).status_code==200
    assert api.mapping_posts==1
    r,_=turn(api,'ขอหน้าถัดไป',review('next'),propose);assert r.status=='preview'
    assert {g['item'] for g in r.data['tool_result']['groups']}=={'Unmapped transit sample 10','Unmapped transit sample 11'}
    assert r.data['tool_result']['skipped_names']==9

def test_query_only_consent_is_consumed_and_never_persists_mapping():
    api=HistoricalHosted();preview(api);old=command(api,explicit=True);before=deepcopy(api.google.cells)
    r,_=turn(api,command(api,'query'));assert r.status=='summary' and r.data['query_only_consent'] is True
    assert r.data['provisional_summary']==[{'category':'Transportation','subcategory':'Rail','expense':'20.00','count':1}]
    assert api.google.cells==before and not api.mapping_posts
    r,_=turn(api,old);assert r.status=='stale' and not api.mapping_posts

@pytest.mark.parametrize('text',['ยืนยัน','ใช่'])
def test_bare_approval_requires_explicit_scope(text):
    api=HistoricalHosted();preview(api);r,_=turn(api,text)
    assert r.status=='clarification' and api.mapping_posts==0

@pytest.mark.parametrize('mutation',['amount','note','ID','catalog','removed','duplicate_ID','expiry','undelivered','intervening'])
def test_stale_or_undelivered_data_and_reviews_refuse_mapping(mutation):
    api=HistoricalHosted()
    if mutation=='undelivered':api.reject_reply=True
    preview(api);cmd=command(api);api.reject_reply=False
    if mutation=='amount':api.google.cells[('All Expenses',3,3)]=99
    if mutation=='note':api.google.cells[('All Expenses',3,4)]='changed'
    if mutation=='ID':api.google.cells[('All Expenses',3,5)]='changed_id'
    if mutation=='catalog':api.google.cells[('Database',3,3)]='Changed'
    if mutation=='removed':
        for k in list(api.google.cells):
            if k[0]=='All Expenses' and k[1]==3:del api.google.cells[k]
    if mutation=='duplicate_ID':api.google.cells[('All Expenses',4,5)]=api.google.cells[('All Expenses',3,5)]
    if mutation=='expiry':api.now+=timedelta(hours=25)
    if mutation=='intervening':turn(api,'ขอบคุณ',response(text='ครับ'))
    r,_=turn(api,cmd);assert r.status=='stale' and api.mapping_posts==0

def test_row_relocation_before_confirmation_preserves_identity_without_transaction_write():
    api=HistoricalHosted();preview(api)
    for col in range(1,7):
        a=api.google.cells.get(('All Expenses',2,col),'');b=api.google.cells.get(('All Expenses',3,col),'')
        api.google.cells[('All Expenses',2,col)]=b;api.google.cells[('All Expenses',3,col)]=a
    before={k:v for k,v in api.google.cells.items() if k[0]=='All Expenses'}
    r,_=turn(api,command(api));assert r.status=='mapping_saved' and api.mapping_posts==1
    assert all(api.google.cells[k]==v for k,v in before.items())

def test_legacy_blank_ID_participates_in_classification_without_ID_assignment():
    api=HistoricalHosted();api.google.cells[('All Expenses',3,5)]='';preview(api)
    r,_=turn(api,command(api));assert r.status=='mapping_saved'
    assert api.google.cells[('All Expenses',3,5)]==''

@pytest.mark.parametrize('lost',['committed','unknown'])
def test_unknown_mapping_recovery_is_read_only_after_flags_OFF(lost):
    api=HistoricalHosted();preview(api)
    if lost=='committed':api.lost_ack=True
    else:api.before_failure=True
    r,_=turn(api,command(api));assert r.status=='uncertain' and api.mapping_posts==1
    api.historical=False;api.labels=False;api.mapping=False
    r,_=turn(api,'ตรวจผลหมวดย้อนหลัง',response('check_historical_mapping',{}))
    assert api.mapping_posts==1
    action=next(a for a in api.snapshot()['actions'].values() if a['metadata'].get('historical_classification'))
    assert action['status']==('completed' if lost=='committed' else 'uncertain')

def test_cancel_discards_only_review_and_never_undoes_provider_state():
    api=HistoricalHosted();preview(api);before=deepcopy(api.google.cells)
    r,_=turn(api,'ยกเลิก');assert r.status=='cancelled' and not api.snapshot()['pending'] and api.google.cells==before

def test_mixed_confirmation_correction_shows_revised_review_without_write():
    api=HistoricalHosted();preview(api)
    r,_=turn(api,'บันทึกหมวดย้อนหลัง แต่ Unknown Metro Ticket เป็น Food',review(selection='all_clear',changes=[('Unknown Metro Ticket','Food','')]))
    assert r.status=='preview' and not api.mapping_posts

@pytest.mark.parametrize('bad',[{'PILOT_HISTORICAL_CATEGORIES_ENABLED':'yes'},{'PILOT_HISTORICAL_CATEGORIES_ENABLED':'true','PILOT_PROVISIONAL_CATEGORIES_ENABLED':'false'}])
def test_feature_is_explicitly_default_OFF_and_requires_history_categories(bad):
    assert HistoryConfig.from_mapping({**values(),**TARGET}).historical_categories_enabled is False
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**values(),**TARGET,**bad})

@pytest.mark.parametrize('field,value',[(('All Expenses',3,3),'=SUM(C2)'),(('All Expenses',1,5),'wrong_header'),(('All Expenses',3,1),'bad date')])
def test_incomplete_or_formula_source_cannot_be_presented_as_complete(field,value):
    api=HistoricalHosted();api.google.cells[field]=value
    r,_=turn(api,'ตรวจหมวดย้อนหลังทั้งหมด',scan)
    assert r.status!='preview' and api.mapping_posts==0 and not api.snapshot()['pending']

@pytest.mark.parametrize('prefix',['ยกเลิกจัดหมวดย้อนหลัง','บันทึกหมวดย้อนหลังผิด','ใช้ข้อเสนอย้อนหลังครั้งนี้ผิด'])
def test_unknown_or_cancel_control_with_valid_nonce_never_authorizes_mapping(prefix):
    api=HistoricalHosted();preview(api);v=api.snapshot()['pending']['payload']
    r,_=turn(api,prefix+' '+v['ref']+' '+v['nonce'])
    assert r.status=='stale' and not api.mapping_posts

def test_nonASCII_nonce_is_rejected_without_adapter_failure():
    api=HistoricalHosted();preview(api);v=api.snapshot()['pending']['payload']
    r,_=turn(api,'บันทึกหมวดย้อนหลัง '+v['ref']+' ไม่ใช่รหัส')
    assert r.status=='stale' and api.mapping_posts==0

def test_empty_unmapped_scan_reports_complete_read_without_pending_review():
    api=HistoricalHosted()
    for row in [3,4]:api.google.cells[('All Expenses',row,2)]='RailPass'
    r,_=turn(api,'ตรวจชื่อที่ยังไม่มีหมวดทั้งหมด',scan)
    assert r.status=='answer' and r.data['tool_result']['total_names']==0
    assert not api.snapshot()['pending'] and not api.mapping_posts

def test_query_only_still_works_when_persistent_mapping_is_disabled():
    api=HistoricalHosted();api.mapping=False;preview(api)
    r,_=turn(api,command(api));assert r.status=='setup_required' and not api.mapping_posts
    # A setup reply is no longer the delivered review; owner asks to review again.
    r,_=turn(api,'ตรวจใหม่',review('refresh'),propose)
    r,_=turn(api,command(api,'query'));assert r.status=='summary' and not api.mapping_posts

def test_many_pages_cover_every_group_and_do_not_expose_unseen_names_to_model():
    api=HistoricalHosted();api.many(104);r,_=preview(api);seen=set()
    while True:
        out=r.data['tool_result'];names={g['item'] for g in out['groups']}
        assert len(names)<=10 and not seen&names;seen|=names
        if not out['remaining_names']:break
        r,_=turn(api,'หน้าถัดไป',review('next'),propose)
    assert len(seen)==104 and api.mapping_posts==0

@pytest.mark.parametrize('tamper',['owner','workbook','seal','nonce'])
def test_foreign_or_changed_restored_review_cannot_confirm(tamper):
    api=HistoricalHosted();preview(api)
    app=api.app()
    with rpc_budget(40),app.state.assistant.state.session():
        v=app.state.assistant.state.pending.payload
        v[tamper]='foreign-or-modified'
        app.state.assistant.state.checkpoint()
    app.state.pilot_http.close()
    r,_=turn(api,'บันทึกหมวดย้อนหลัง');assert r.status=='stale' and not api.mapping_posts

def test_possible_alias_and_existing_blank_category_remain_open_without_append():
    api=HistoricalHosted();api.google.cells[('All Expenses',3,2)]='RailPas'
    api.google.cells[('Database',2,2)]=''
    api.google.cells[('All Expenses',4,2)]=api.google.cells[('Database',2,1)]
    r,_=preview(api);out=r.data['tool_result']
    assert all(not g['included'] for g in out['groups'])
    r,_=turn(api,'บันทึกหมวดย้อนหลัง');assert r.status=='clarification' and not api.mapping_posts

@pytest.mark.parametrize('bad',['foreign_group','invented_category','missing_group','duplicate_group'])
def test_model_cannot_classify_unowned_groups_or_create_taxonomy(bad):
    api=HistoricalHosted()
    def invalid(body):
        result=propose(body);args=json.loads(result['output'][0]['arguments'])
        if bad=='foreign_group':args['proposals'][0]['group_ref']='hg_foreign'
        elif bad=='invented_category':args['proposals'][0].update(status='proposed',category='Invented',subcategory='',candidates=[])
        elif bad=='missing_group':args['proposals']=args['proposals'][:1]
        else:args['proposals'].append(deepcopy(args['proposals'][0]))
        return response('propose_historical_categories',args)
    r,_=turn(api,'จัดหมวดย้อนหลังทั้งหมด',scan,invalid)
    assert r.status!='preview' and not api.mapping_posts

@pytest.mark.parametrize('domain',['expense','calendar'])
def test_saved_record_workflow_remains_separate_when_all_features_are_enabled(domain):
    api=FullHistoricalHosted()
    text='แก้รายจ่าย RailPass วันที่ 5 พ.ย. เป็น 35 บาท' if domain=='expense' else 'เลื่อนนัดวันที่ 5 พ.ย. ไปวันที่ 7 พ.ย.'
    changes={'amount':'35','amount_text':'35'} if domain=='expense' else {'date':'2026-11-07','date_text':'7 พ.ย.'}
    answers=[find_saved(domain,'RailPass' if domain=='expense' else None),prepare_saved(domain,**changes)]
    r,_=turn(api,text,*answers);assert r.status=='preview',r
    assert r.data.get('saved_record_edit') is True and not r.data.get('historical_categories')
    r,_=turn(api,'ยืนยัน');assert r.status=='updated',r
    assert api.expense_posts==(1 if domain=='expense' else 0)
    assert api.saved_calendar.committed==(1 if domain=='calendar' else 0)
    assert api.mapping_posts==0

def test_plain_expense_and_calendar_queries_do_not_start_classification():
    api=FullHistoricalHosted()
    def expense(body):
        c=context(body);return response('query_expenses',{'date_mode':'range','start_date':'2026-11-05','end_date':'2026-11-05','period_text':'5 พ.ย.','relative_offset':None,'source_refs':[c['current_source_ref']],'filter':None,'view':'total','measure':'expense'})
    r,_=turn(api,'รายจ่ายวันที่ 5 พ.ย. เท่าไร',expense)
    assert r.status=='summary' and r.data['tool_result']['expense']=='37.00' and not api.snapshot()['pending']
    def calendar(body):
        c=context(body);return response('query_calendar',{'date_mode':'range','start_date':'2026-11-05','end_date':'2026-11-05','period_text':'5 พ.ย.','relative_offset':None,'source_refs':[c['current_source_ref']],'upcoming':False,'filter_text':None})
    r,_=turn(api,'มีนัดวันที่ 5 พ.ย. อะไรบ้าง',calendar);assert r.data['tool_result']['complete'] is True and not api.snapshot()['pending']
    assert api.mapping_posts==api.expense_posts==api.saved_calendar.committed==api.calendar.posts==0

def test_oversized_review_cannot_confirm_truncated_unseen_labels_but_partial_review_can():
    api=HistoricalHosted();api.many(10)
    for row in range(2,12):api.google.cells[('All Expenses',row,2)]=f'{row:02d} '+('Long synthetic label '*10)[:180]
    category='Canonical category '+('c'*170);subcategory='Canonical subcategory '+('s'*165)
    for col,value in enumerate(['Known long-label anchor',category,subcategory],1):api.google.cells[('Database',4,col)]=value
    def long_proposal(body):
        c=context(body);h=c['historical_categories'];return response('propose_historical_categories',{'review_ref':h['review_ref'],'proposals':[{'group_ref':g['group_ref'],'status':'proposed','category':category,'subcategory':subcategory,'candidates':[]} for g in h['groups']]})
    r,_=turn(api,'จัดหมวดย้อนหลังทั้งหมด',scan,long_proposal)
    assert r.data['tool_result']['confirmation_available'] is False
    assert len(r.message.encode('utf-16-le'))<10000
    r,_=turn(api,'บันทึกหมวดย้อนหลัง');assert r.status=='stale' and not api.mapping_posts
    r,_=turn(api,'ตรวจใหม่',review('refresh'),long_proposal)
    selected=r.data['tool_result']['groups'][0]['item']
    r,_=turn(api,'เอาเฉพาะ '+selected,review(selection='only',selected=[selected]))
    assert r.data['tool_result']['confirmation_available'] is True
    r,_=turn(api,'บันทึกหมวดย้อนหลัง');assert r.status=='mapping_saved' and len(r.data['mapping_rows'])==1

def test_existing_write_permission_failure_is_conflict_without_access_setup():
    class Forbidden(HistoricalHosted):
        def handle(self,request):
            if self.configured and request.url.host=='sheets.googleapis.com' and request.method=='POST':return httpx.Response(403,json={'error':{'message':'Synthetic existing permission denied'}})
            return super().handle(request)
    api=Forbidden();preview(api);r,_=turn(api,'บันทึกหมวดย้อนหลัง')
    assert r.status=='conflict' and api.mapping_posts==0

def test_optional_wording_preserves_historical_review_and_mapping_receipt():
    api=FullHistoricalHosted();api.voice=True;preview(api)
    r,_=turn(api,'บันทึกหมวดย้อนหลัง')
    assert r.status=='mapping_saved' and len(r.data['mapping_rows'])==1
    assert 'ย้อนหลัง' in r.message and api.mapping_posts==1 and api.expense_posts==api.saved_calendar.committed==0

@pytest.mark.parametrize('changed',[False,True])
def test_prepared_confirmed_action_revalidates_source_after_crash_before_first_attempt(monkeypatch,changed):
    from line_assistant.historical_categories import HistoricalCategories
    api=HistoricalHosted();preview(api)
    def crash(*a):raise RuntimeError('Synthetic crash after durable confirmation before first attempt')
    with monkeypatch.context() as patch:
        patch.setattr(HistoricalCategories,'resume',crash)
        with TestClient(api.app()) as web:
            event=api.event('บันทึกหมวดย้อนหลัง');response=api.send(web,event)
            assert response.status_code==503
    before=next(a for a in api.snapshot()['actions'].values() if a['metadata'].get('historical_classification'))
    assert before['status']=='prepared' and api.mapping_posts==0
    if changed:api.google.cells[('All Expenses',3,3)]=99
    with TestClient(api.app()) as web:assert api.send(web,deepcopy(event)).status_code==200
    action=next(a for a in api.snapshot()['actions'].values() if a['metadata'].get('historical_classification'))
    assert action['status']=='completed' and action['result']['status']==('stale' if changed else 'mapping_saved')
    assert api.mapping_posts==(0 if changed else 1)
    with TestClient(api.app()) as web:assert api.send(web,deepcopy(event)).status_code==200
    assert api.mapping_posts==(0 if changed else 1)
