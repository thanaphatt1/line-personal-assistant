"""Signed provisional classification and distinct consent, synthetic HTTP only."""
from copy import deepcopy
from datetime import date,timedelta
import json,socket
import httpx,pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from line_assistant.history_cloud_run import create_history_cloud_run_app,HistoryConfig
from line_assistant.runtime_config import ConfigError
from line_assistant.sheet_dates import date_to_serial
from line_assistant.pilot_http import rpc_budget
from line_assistant.transport import LiveDisabled
from test_deployment_configuration import ConfiguredHosted,TARGET
from test_calendar_factory import values
from test_calendar_mvp import response

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Provisional category tests must be offline')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

class LabelHosted(ConfiguredHosted):
    def __init__(self):
        self.labels=True;self.mapping=True;self.mapping_posts=0;self.lost_ack=False;self.before_failure=False;self.voice=False;self.voice_failure=False
        super().__init__()
        for col,value in enumerate(['RailPass','Transportation','Rail'],1):self.google.cells[('Database',3,col)]=value
        for row,(item,amount) in enumerate([('RailPass',10),('Unknown Metro Ticket',20),('Mystery Token',7)],2):
            for col,value in enumerate([date_to_serial(date(2026,11,5)),item,amount,'kept','synthetic_label_'+str(row),'=C'+str(row)+'*2'],1):self.google.cells[('All Expenses',row,col)]=value
    def app(self,enabled=True):
        flags={'PILOT_PROVISIONAL_CATEGORIES_ENABLED':str(self.labels).lower(),'PILOT_PROVISIONAL_MAPPING_ENABLED':str(self.mapping).lower(),'PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY':'reviewed_single_writer' if self.mapping else 'off','PILOT_RESPONSE_REALIZATION_ENABLED':str(self.voice).lower()}
        return create_history_cloud_run_app({**values(enabled),**TARGET,**flags},transport=httpx.MockTransport(self.handle),secret_loader=lambda name:SecretStr('synthetic-'+name),clock=lambda:self.now)
    def handle(self,request):
        if self.configured and self.voice and request.url.host=='api.openai.com':
            from line_assistant.response_realization import INSTRUCTIONS
            body=json.loads(request.content)
            if body.get('instructions')==INSTRUCTIONS:
                self.model_bodies.append(deepcopy(body))
                if self.voice_failure:raise httpx.ReadTimeout('Synthetic optional wording failure',request=request)
                from test_response_realization import generated
                return httpx.Response(200,json=generated(body))
        if self.configured and request.url.host=='sheets.googleapis.com' and request.method=='POST':
            body=json.loads(request.content);blocks=body.get('requests',[])
            if len(blocks)==1 and blocks[0].get('appendCells',{}).get('sheetId')==502:
                self.mapping_posts+=1
                if self.before_failure:raise httpx.ReadTimeout('Synthetic unknown before mapping',request=request)
                block=blocks[0]['appendCells'];assert block['fields']=='userEnteredValue'
                last=max(row for (tab,row,col),value in self.google.cells.items() if tab=='Database' and value not in ('',None))
                for offset,row in enumerate(block['rows'],1):
                    assert len(row['values'])==3
                    for col,cell in enumerate(row['values'],1):self.google.cells[('Database',last+offset,col)]=cell['userEnteredValue']['stringValue']
                if self.lost_ack:raise httpx.ReadTimeout('Synthetic mapping committed, lost acknowledgement',request=request)
                return httpx.Response(200,json={'spreadsheetId':TARGET['PILOT_SPREADSHEET_ID'],'replies':[{}]})
        return super().handle(request)

def turn(api,text,*answers):
    with TestClient(api.app()) as client:return api.turn(client,text,*answers)
def query(body):
    c=json.loads(body['input'][0]['content'])
    return response('query_expenses',{'start_date':'2026-11-05','end_date':'2026-11-05','date_mode':'range','relative_offset':None,'period_text':'5 พ.ย.','source_refs':[c['current_source_ref']],'filter':{'kind':'category','value':'Transportation'},'view':'total','measure':'expense'})
def proposals(body):
    c=json.loads(body['input'][0]['content']);ref=c['provisional_categories']['query_ref']
    return response('propose_expense_categories',{'query_ref':ref,'proposals':[{'item':'Unknown Metro Ticket','status':'proposed','category':'Transportation','subcategory':'Rail','candidates':[]},{'item':'Mystery Token','status':'ambiguous','category':None,'subcategory':None,'candidates':[{'category':'Transportation','subcategory':'Rail'},{'category':'Food','subcategory':''}]}]})
def preview(api):return turn(api,'ค่าเดินทางวันที่ 5 พ.ย. เท่าไร ถ้าไม่พบหมวดช่วยเสนอให้ตรวจด้วย',query,proposals)
def command(api,mode='query'):
    v=api.snapshot()['pending']['payload'];return ('รวมข้อเสนอครั้งนี้' if mode=='query' else 'บันทึกหมวด')+' '+v['ref']+' '+v['nonce']

def test_confirmed_proposed_ambiguous_are_separate_and_model_cannot_persist():
    api=LabelHosted();r,_=preview(api);out=r.data['tool_result'];assert r.status=='preview',r
    assert out['confirmed_partial']['expense']=='10.00' and out['confirmed_partial']['count']==1
    assert out['proposed_partial']=={'expense':'20.00','count':1,'items':['Unknown Metro Ticket']}
    assert out['proposals'][1]['status']=='ambiguous' and 'ยังไม่ได้แก้ Database' in r.message
    assert api.mapping_posts==api.google.sheet_writes==0 and api.calendar.posts==0 and len(api.model_bodies)==2
    assert not any('configured-workbook' in json.dumps(body) for body in api.model_bodies)

def test_query_only_consent_does_not_authorize_later_permanent_save():
    api=LabelHosted();preview(api);permanent=command(api,'permanent');q=command(api);before=deepcopy(api.google.cells)
    r,event=turn(api,q);assert r.status=='summary' and r.data['query_only_consent'] is True
    assert r.data['combined_partial']=={'expense':'30.00','count':2,'complete':False} and r.data['tool_result']['partial']['expense']=='10.00'
    assert 'expense' not in r.data['tool_result'] and api.google.cells==before and not api.mapping_posts
    stale,_=turn(api,permanent);assert stale.status=='stale' and not api.mapping_posts
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert not api.mapping_posts

def test_permanent_mapping_only_explicit_proposed_names_and_replay_once():
    api=LabelHosted();review,_=preview(api);before={k:v for k,v in api.google.cells.items() if k[0]=='All Expenses'}
    r,event=turn(api,command(api,'permanent'));assert r.status=='mapping_saved',r
    assert r.data['mapping_rows']==[{'item':'Unknown Metro Ticket','category':'Transportation','subcategory':'Rail'}]
    assert api.mapping_posts==1 and all(api.google.cells[k]==v for k,v in before.items())
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.mapping_posts==1

@pytest.mark.parametrize('mutation',['catalog','data','expiry','undelivered'])
def test_stale_or_undelivered_review_cannot_include_or_persist(mutation):
    api=LabelHosted()
    if mutation=='undelivered':api.reject_reply=True
    preview(api);cmd=command(api,'permanent');api.reject_reply=False
    if mutation=='catalog':api.google.cells[('Database',3,3)]='Changed'
    if mutation=='data':api.google.cells[('All Expenses',3,3)]=99
    if mutation=='expiry':api.now+=timedelta(hours=2)
    r,_=turn(api,cmd);assert r.status=='stale' and not api.mapping_posts

def test_bare_confirmation_requires_distinct_consent_choice():
    api=LabelHosted();preview(api);r,_=turn(api,'ยืนยัน');assert r.status=='clarification' and not api.mapping_posts

def test_query_flag_and_mapping_flag_are_independent():
    api=LabelHosted();api.mapping=False;r,_=preview(api);assert r.status=='preview' and 'การบันทึก mapping ถาวรยังปิดอยู่' in r.message
    r,_=turn(api,command(api));assert r.status=='summary' and not api.mapping_posts

@pytest.mark.parametrize('lost',['committed','unknown'])
def test_unknown_mapping_effect_is_read_only_on_resume_even_off(lost):
    api=LabelHosted();preview(api)
    if lost=='committed':api.lost_ack=True
    else:api.before_failure=True
    r,_=turn(api,command(api,'permanent'));assert r.status=='uncertain',r
    api.labels=False;api.mapping=False
    r,_=turn(api,'ตรวจผล mapping',response('check_provisional_mapping',{}));assert api.mapping_posts==1
    statuses=[a['status'] for a in api.snapshot()['actions'].values()]
    assert ('completed' in statuses) if lost=='committed' else ('uncertain' in statuses)

def test_possible_mistaken_alias_stays_ambiguous_even_if_model_proposes_category():
    api=LabelHosted();api.google.cells[('All Expenses',3,2)]='RailPas'
    def alias(body):
        c=json.loads(body['input'][0]['content']);return response('propose_expense_categories',{'query_ref':c['provisional_categories']['query_ref'],'proposals':[{'item':'RailPas','status':'proposed','category':'Transportation','subcategory':'Rail','candidates':[]}]})
    r,_=turn(api,'ค่าเดินทางวันที่ 5 พ.ย.',query,alias);assert r.status=='preview'
    assert r.data['tool_result']['proposals'][0]['status']=='ambiguous' and r.data['tool_result']['proposed_partial']['expense']=='0.00'
    r,_=turn(api,command(api,'permanent'));assert r.status=='clarification' and not api.mapping_posts

def test_partial_provider_data_never_yields_proposals_or_a_zero_total():
    api=LabelHosted();api.google.cells[('All Expenses',3,3)]='invalid'
    r,_=turn(api,'ค่าเดินทางวันที่ 5 พ.ย.',query);assert r.status!='preview'
    assert r.data['tool_result']['error_code']=='schema_failure' and not api.snapshot()['pending'] and not api.mapping_posts

def test_new_review_revokes_previous_query_and_permanent_commands():
    api=LabelHosted();preview(api);old=command(api,'permanent')
    fresh,_=turn(api,'ขอตรวจข้อเสนอใหม่',response('review_provisional_categories',{}));assert fresh.status=='preview'
    r,_=turn(api,old);assert r.status=='stale' and not api.mapping_posts

def test_query_only_then_separate_fresh_permanent_review_and_confirmation():
    api=LabelHosted();preview(api);turn(api,command(api))
    review,_=turn(api,'อยากจำหมวดถาวร ขอตรวจข้อเสนออีกครั้ง',response('review_provisional_categories',{}));assert review.status=='preview' and not api.mapping_posts
    saved,_=turn(api,command(api,'permanent'));assert saved.status=='mapping_saved' and api.mapping_posts==1

@pytest.mark.parametrize('mutation',['unknown_name','duplicate','bad_pair','confidence','wrong_ref'])
def test_fabricated_proposal_fields_names_pairs_or_basis_cannot_write(mutation):
    api=LabelHosted()
    def bad(body):
        r=proposals(body);call=r['output'][0];a=json.loads(call['arguments'])
        if mutation=='unknown_name':a['proposals'][0]['item']='Unowned Item'
        if mutation=='duplicate':a['proposals'].append(deepcopy(a['proposals'][0]))
        if mutation=='bad_pair':a['proposals'][0]['category']='Unknown Category'
        if mutation=='confidence':a['proposals'][0]['confidence']=99
        if mutation=='wrong_ref':a['query_ref']='pq_foreign'
        call['arguments']=json.dumps(a);return r
    r,_=turn(api,'ค่าเดินทางวันที่ 5 พ.ย.',query,bad);assert r.status!='preview' and not api.mapping_posts

@pytest.mark.parametrize('mutation',['owner','workbook','seal'])
def test_foreign_or_modified_review_is_not_consent(mutation):
    api=LabelHosted();preview(api);cmd=command(api,'permanent');app=api.app()
    with rpc_budget(20),app.state.assistant.state.session():
        s=app.state.assistant.state;s.pending.payload[mutation]='foreign';s.checkpoint()
    app.state.pilot_http.close();r,_=turn(api,cmd);assert r.status=='stale' and not api.mapping_posts

@pytest.mark.parametrize('bad',[{'PILOT_PROVISIONAL_MAPPING_ENABLED':'true'},{'PILOT_PROVISIONAL_CATEGORIES_ENABLED':'yes'},
    {'PILOT_PROVISIONAL_CATEGORIES_ENABLED':'true','PILOT_PROVISIONAL_MAPPING_ENABLED':'true'}])
def test_configuration_requires_explicit_writer_policy(bad):
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**values(),**TARGET,**bad})

def test_no_mapping_action_means_no_transport_write_before_tokens():
    api=LabelHosted();app=api.app();http=app.state.pilot_http
    with rpc_budget(20),app.state.assistant.state.session():
        with pytest.raises(LiveDisabled):http.request('sheets','POST','/v4/spreadsheets/'+TARGET['PILOT_SPREADSHEET_ID']+':batchUpdate',body={'requests':[{'appendCells':{'sheetId':502,'rows':[],'fields':'userEnteredValue'}}]})
    assert not api.mapping_posts;http.close()

def test_pending_proposal_owner_sources_and_review_are_protected_from_summary():
    from line_assistant.raw_history import speakers,protected_rows,key
    api=LabelHosted();preview(api);app=api.app()
    with rpc_budget(20),app.state.assistant.state.session():
        s=app.state.assistant.state;rows=speakers(s,api.now,production=True);keep=protected_rows(s,rows);refs=s.pending.payload['basis']['args']['source_refs']
        assert all(key(r) in keep for r in rows if r.get('source_ref') in refs or r['event']==s.pending.origin_event_id)
    app.state.pilot_http.close()

@pytest.mark.parametrize('text,status,writes',[('รวมข้อเสนอครั้งนี้','summary',0),('บันทึกหมวดถาวร','mapping_saved',1)])
def test_explicit_short_scope_confirmation_binds_only_latest_delivered_review(text,status,writes):
    api=LabelHosted();preview(api);r,_=turn(api,text);assert r.status==status and api.mapping_posts==writes
    stale,_=turn(api,text);assert stale.status=='stale' and api.mapping_posts==writes

def test_realized_query_only_receipt_preserves_all_amounts_names_and_scope():
    api=LabelHosted();api.voice=True;r,_=preview(api)
    assert r.data['response_realization']['reason']=='core_step_budget'
    r,_=turn(api,'รวมข้อเสนอครั้งนี้');assert r.status=='summary' and r.data['query_only_consent'] is True
    assert r.data['response_realization']['status']=='llm' and all(s in r.message for s in ['10.00','20.00','30.00','Unknown Metro Ticket','ยังไม่ได้บันทึก mapping ถาวร'])
    assert not api.mapping_posts

@pytest.mark.parametrize('failure',[False,True])
def test_optional_wording_after_mapping_never_repeats_or_changes_effect(failure):
    api=LabelHosted();api.voice=True;preview(api);api.voice_failure=failure
    r,event=turn(api,'บันทึกหมวดถาวร');assert r.status=='mapping_saved' and api.mapping_posts==1
    assert r.data['response_realization']['status']==('fallback' if failure else 'llm')
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.mapping_posts==1
