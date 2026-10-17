"""Signed ingress, owner leases and exact HTTP bodies through synthetic transports."""
from copy import deepcopy
from datetime import timedelta
import json
import socket
from urllib.parse import quote

from fastapi.testclient import TestClient
import httpx
import pytest
from line_assistant.cloud_run import create_cloud_run_app
from line_assistant.enrollment import Enrollment
from line_assistant.firestore_state import FirestoreState
from line_assistant.main import signature
from line_assistant.mock_api import MockApiServices
from line_assistant.persistence import result_from
from line_assistant.pilot_config import MAIN_WORKBOOK_ID, PilotConfig
from line_assistant.pilot_http import rpc_budget
from line_assistant.readonly_preflight import PROJECT_ID, SERVICE_ACCOUNT
from line_assistant.runtime_config import ConfigError
from line_assistant.sheet_metadata import TransactionIdMetadata
from line_assistant.sheets_api import SheetsApi, SheetTarget, tab_range
from line_assistant.snapshot_codec import decode
from line_assistant.state import Action, Reply, Receipt, Result
from line_assistant.transport import RestClient, LiveDisabled, OutcomeUnknown, WriteBlocked
from line_assistant.adapters import Conflict
from line_assistant.line_api import message_hash, rendered_message
from test_cloud_run import OWNER, fake_secrets
from test_conversation import NOW, ROWS, decision
from test_model_api import response

@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def fail(*args, **kwargs): raise AssertionError('Synthetic transport tests cannot use sockets')
    monkeypatch.setattr(socket.socket,'connect',fail)
    monkeypatch.setattr(socket.socket,'connect_ex',fail)

def config():
    return {'PILOT_MODE':'production','PILOT_LAUNCH_AUTHORIZED':'true','PILOT_CONVERSATION_ENABLED':'true',
            'PILOT_SPREADSHEET_ID':MAIN_WORKBOOK_ID,'PILOT_LINE_OWNER_ID':OWNER,
            'PILOT_MODEL_MONTHLY_MICRO_USD':'500000','PILOT_MODEL_MAX_CALLS_DAY':'100','PILOT_MODEL_MAX_CALLS_MONTH':'300'}

class HostedConversation:
    def __init__(self):
        self.google=MockApiServices(id_column='E'); self.google.row_count=1001
        self.google.cells={k:v for k,v in self.google.cells.items() if k[0]!='Database' or k[1]==1}
        for i,row in enumerate(ROWS,2):
            for j,v in enumerate(row,1):self.google.cells[('Database',i,j)]=v
        self.now=NOW; self.counter=0; self.models=0; self.model_bodies=[]; self.write_bodies=[]; self.value=None
        state=FirestoreState(RestClient('firestore',httpx.MockTransport(self.handle)),OWNER,project_id=PROJECT_ID,
            collection_id='assistant_pilot_states',clock=lambda:self.now,max_snapshot_bytes=800000,compact_snapshots=True)
        with state.session():
            state.production_ready=True; state.workbook_id=MAIN_WORKBOOK_ID; state.checkpoint()

    def handle(self,request):
        if request.url.host=='metadata.google.internal':
            if request.url.path.endswith('/email'):
                return httpx.Response(200,text=SERVICE_ACCOUNT,headers={'Metadata-Flavor':'Google'})
            return httpx.Response(200,json={'access_token':'synthetic-token','expires_in':3600,'token_type':'Bearer'},headers={'Metadata-Flavor':'Google'})
        if request.url.host=='api.openai.com':
            self.models+=1; self.model_bodies.append(json.loads(request.content))
            assert any(r['status']=='reserved' for r in self.snapshot()['model_runs'].values())
            return httpx.Response(200,json=self.value)
        if request.url.host=='sheets.googleapis.com':
            assert MAIN_WORKBOOK_ID in request.url.path
            if request.method=='POST':self.write_bodies.append(json.loads(request.content))
            result=self.google.handle(request); body=result.json()
            if 'spreadsheetId' in body:body['spreadsheetId']=MAIN_WORKBOOK_ID
            return httpx.Response(result.status_code,json=body)
        return self.google.handle(request)

    def snapshot(self):return decode(next(iter(self.google.documents.values()))['fields']['snapshot']['stringValue'],800000)
    def app(self):return create_cloud_run_app(config(),transport=httpx.MockTransport(self.handle),secret_loader=fake_secrets,clock=lambda:self.now)

    def event(self,text=None,data=None):
        self.counter+=1; self.now+=timedelta(seconds=1)
        e={'webhookEventId':f'hosted-{self.counter}','source':{'type':'user','userId':OWNER},
           'timestamp':int(self.now.timestamp()*1000),'replyToken':'synthetic-reply'}
        if data is None:e.update(type='message',message={'type':'text','text':text,'id':str(self.counter)})
        else:e.update(type='postback',postback={'data':data})
        return e

    def send(self,client,event):
        body=json.dumps({'events':[event]},ensure_ascii=False).encode()
        return client.post('/webhooks/line',content=body,headers={'x-line-signature':signature(body,'synthetic-line-channel-secret')})

    def text(self,client,text,intent,**kwargs):
        self.value=response('interpret_message',decision(intent,**kwargs)); e=self.event(text=text)
        assert self.send(client,e).status_code==200
        return result_from(self.snapshot()['receipts'][e['webhookEventId']]['result'])

    def button(self,result,label):
        buttons=result.data['line_message']['contents']['footer']['contents']
        return self.event(data=next(b['action']['data'] for b in buttons if b['action']['label']==label))

def test_full_signed_medicine_flow_atomic_batch_and_new_controller_replay():
    api=HostedConversation(); original=deepcopy(api.google.cells)
    with TestClient(api.app()) as client:
        r=api.text(client,'วันที่ 10 มีค่ากระดาษ 228 บาท','expense',entries=[{'item':'ค่ากระดาษ','amount':'228'}])
        assert 'เป็นเดือนใด' in r.message
        api.text(client,'ตุลาคม','edit_draft',entries=[{'date':'2026-10-10'}])
        api.text(client,'เพิ่มในหมวด office','enroll_item',category_text='office')
        r=api.text(client,'Supplies','enroll_item',subcategory_text='Supplies')
        assert r.status=='preview' and api.google.sheet_writes==0 and api.models==4
        ev=api.button(r,'บันทึก'); assert api.send(client,ev).status_code==200
        assert api.snapshot()['receipts'][ev['webhookEventId']]['result']['status']=='recorded'
    assert api.google.sheet_writes==1 and len(api.write_bodies)==1
    blocks=api.write_bodies[0]['requests']
    assert [b['appendCells']['sheetId'] for b in blocks]==[102,101]
    assert len(blocks[0]['appendCells']['rows'][0]['values'])==3
    assert len(blocks[1]['appendCells']['rows'][0]['values'])==5
    assert all(api.google.cells[k]==v for k,v in original.items())
    assert any(key[0]=='Database' and value=='ค่ากระดาษ' for key,value in api.google.cells.items())
    assert any(m[0]['type']=='flex' for m in api.google.line_replies)
    counts=api.models,api.google.sheet_writes,len(api.google.line_replies)
    with TestClient(api.app()) as client:assert api.send(client,ev).status_code==200
    assert (api.models,api.google.sheet_writes,len(api.google.line_replies))==counts

def ready_enrollment(api,client):
    r=api.text(client,'ขนม 50 บาท','expense',entries=[{'item':'ขนม','amount':'50'}])
    r=api.text(client,'เพิ่มในหมวด Food','enroll_item',category_text='Food')
    assert r.status=='preview'; return api.button(r,'บันทึก')

def test_lost_atomic_ack_recovers_by_id_and_mapping_readback_without_retry():
    api=HostedConversation()
    with TestClient(api.app()) as client:
        ev=ready_enrollment(api,client); api.google.fail_after_sheet_write=True
        assert api.send(client,ev).status_code==503
    assert api.google.sheet_writes==1 and len(api.write_bodies)==1
    with TestClient(api.app()) as client:assert api.send(client,ev).status_code==200
    assert api.google.sheet_writes==1 and len(api.write_bodies)==1
    assert api.snapshot()['receipts'][ev['webhookEventId']]['result']['status']=='recorded'

def test_rejected_atomic_batch_has_no_partial_master_or_expense_and_remains_uncertain():
    api=HostedConversation(); original=deepcopy(api.google.cells)
    with TestClient(api.app()) as client:
        ev=ready_enrollment(api,client); api.google.reject_atomic_batch=True
        assert api.send(client,ev).status_code==503
        assert api.send(client,ev).status_code==200
    assert api.google.sheet_writes==0 and api.google.cells==original and len(api.write_bodies)==1
    assert api.snapshot()['receipts'][ev['webhookEventId']]['result']['status']=='uncertain'

def test_bad_signature_nonowner_and_unsupported_postback_are_zero_model_and_write():
    api=HostedConversation(); count=len(api.google.calls)
    with TestClient(api.app()) as client:
        ev=api.event(text='รถเมล์ 20 บาท'); body=json.dumps({'events':[ev]}).encode()
        assert client.post('/webhooks/line',content=body,headers={'x-line-signature':'wrong'}).status_code==401
        ev['source']['userId']='U'+'b'*32; assert api.send(client,ev).status_code==200
        assert len(api.google.calls)==count
        assert api.send(client,api.event(data='forged')).status_code==200
    assert api.models==api.google.sheet_writes==0

def test_exact_transport_flex_binding_rejects_button_and_destination_mutations():
    api=HostedConversation(); app=api.app()
    with TestClient(app) as client:
        r=api.text(client,'รถเมล์ 20 บาท','expense',entries=[{'item':'รถเมล์','amount':'20'}])
        http=app.state.pilot_http; state=app.state.assistant.state
        with rpc_budget(96),state.session():
            event_id='hosted-1'; state.replies[event_id].status='executing'; state.checkpoint()
            payload={'replyToken':'synthetic-token','messages':[rendered_message(r)]}
            http.allowed('line','POST','/v2/bot/message/reply',payload,None)
            changed=deepcopy(payload); changed['messages'][0]['contents']['footer']['contents'][0]['action']['data']+='x'
            with pytest.raises(LiveDisabled):http.allowed('line','POST','/v2/bot/message/reply',changed,None)
            with pytest.raises(LiveDisabled):http.allowed('line','POST','/v2/bot/message/push',payload,None)

def test_conversation_config_is_explicit_opt_in_and_does_not_raise_call_or_money_caps():
    assert not PilotConfig.from_mapping({}).conversation_enabled
    for override in [{'PILOT_CONVERSATION_ENABLED':'yes'},{'PILOT_MODEL_MAX_CALLS_DAY':'101'},
                     {'PILOT_MODEL_MAX_CALLS_MONTH':'301'},{'PILOT_MODEL_MONTHLY_MICRO_USD':'500001'}]:
        with pytest.raises(ConfigError):PilotConfig.from_mapping({**config(),**override})
    with pytest.raises(ConfigError):PilotConfig.from_mapping({'PILOT_CONVERSATION_ENABLED':'true'})

def adapter(providers):
    target=SheetTarget(spreadsheet_id='synthetic-sheet',writes_enabled=True,input_access_verified=True,
        recovery='approved_id_column',transaction_id_metadata=TransactionIdMetadata('E',schema_approved=True))
    return SheetsApi(RestClient('sheets',providers.transport()),target)

def enrollment():
    return {'entries':[{'item':'=new literal','amount':'50','date':'2026-10-10','note':'=1+1','kind':'expense'}],
            'catalog':[{'item':'=new literal','category':'Synthetic','subcategory':''}]}

def test_catalog_enrollment_writes_only_literal_mapping_and_identified_inputs():
    providers=MockApiServices(id_column='E'); sheets=adapter(providers); before=deepcopy(providers.cells)
    assert sheets.enroll_and_add(enrollment(),'atomic')==("'All Expenses'!A2:D2",)
    assert providers.sheet_writes==1 and all(providers.cells[k]==v for k,v in before.items())
    assert providers.cells[('Database',6,1)]=='=new literal' and providers.cells[('All Expenses',2,4)]=='=1+1'
    assert sheets.reconcile_enrollment('atomic',enrollment()).outcome=='committed'
    with pytest.raises(Conflict):sheets.enroll_and_add(enrollment(),'atomic')
    assert providers.sheet_writes==1

@pytest.mark.parametrize('change',['database_full','expense_full','database_protection','category_removed','different_mapping'])
def test_enrollment_preconditions_fail_before_any_write(change):
    p=MockApiServices(id_column='E'); plan=enrollment()
    if change=='database_full':p.cells[('Database',p.database_row_count,26)]='existing formula'
    elif change=='expense_full':p.cells[('All Expenses',p.row_count,24)]='existing formula'
    elif change=='database_protection':p.database_protections=[{'warningOnly':False}]
    elif change=='category_removed':plan['catalog'][0]['category']='invented'
    else:p.cells[('Database',6,1)]='=new literal';p.cells[('Database',6,2)]='Other'
    before=deepcopy(p.cells)
    with pytest.raises((Conflict,WriteBlocked)):adapter(p).enroll_and_add(plan,'atomic')
    assert p.sheet_writes==0 and p.cells==before

def test_existing_identical_mapping_only_appends_expense_not_duplicate_catalog_row():
    p=MockApiServices(id_column='E'); p.cells[('Database',6,1)]='=new literal';p.cells[('Database',6,2)]='Synthetic'
    assert len(adapter(p).enrollment_body(enrollment(),'atomic')['requests'])==1
    adapter(p).enroll_and_add(enrollment(),'atomic')
    assert p.sheet_writes==1 and p.cells.get(('Database',7,1)) is None

def test_missing_id_or_changed_mapping_readback_never_authorizes_blind_retry():
    p=MockApiServices(id_column='E'); sheets=adapter(p)
    assert sheets.reconcile_enrollment('never-sent',enrollment()).outcome=='unknown'
    sheets.enroll_and_add(enrollment(),'atomic'); p.cells[('Database',6,2)]='Other'
    assert sheets.reconcile_enrollment('atomic',enrollment()).outcome=='unknown' and p.sheet_writes==1

def test_human_concurrent_mapping_addition_is_detected_without_overwriting_human_row():
    p=MockApiServices(id_column='E'); sheets=adapter(p)
    def human(mock):
        mock.cells[('Database',6,1)]='=new literal';mock.cells[('Database',6,2)]='Other'
    p.before_sheet_write=human
    with pytest.raises(OutcomeUnknown):sheets.enroll_and_add(enrollment(),'atomic')
    assert p.cells[('Database',6,2)]=='Other' and p.sheet_writes==1
    assert sheets.reconcile_enrollment('atomic',enrollment()).outcome=='unknown'
