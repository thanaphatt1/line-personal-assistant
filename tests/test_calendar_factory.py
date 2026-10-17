"""Signed production-shaped factory with wholly synthetic Google/LINE/model HTTP."""
from copy import deepcopy
from datetime import timedelta
from urllib.parse import unquote
import base64,hashlib,hmac,json,socket
import httpx,pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from line_assistant.firestore_state import FirestoreState
from line_assistant.history_cloud_run import create_history_cloud_run_app,HistoryConfig
from line_assistant.mock_api import MockApiServices
from line_assistant.pilot_config import MAIN_WORKBOOK_ID
from line_assistant.readonly_preflight import PROJECT_ID,SERVICE_ACCOUNT
from line_assistant.persistence import result_from
from line_assistant.snapshot_codec import decode
from line_assistant.transport import RestClient,LiveDisabled
from line_assistant.pilot_http import rpc_budget
from line_assistant.runtime_config import ConfigError
from test_calendar_mvp import FakeCalendar,response,NOW

OWNER='U'+'a'*32
CALENDAR='synthetic-calendar@group.calendar.google.com'

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No external HTTP, live Calendar or credentials')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def values(enabled=True):
    return {'PILOT_MODE':'production','PILOT_LAUNCH_AUTHORIZED':'true','PILOT_CONVERSATION_ENABLED':'true',
        'PILOT_SPREADSHEET_ID':MAIN_WORKBOOK_ID,'PILOT_LINE_OWNER_ID':OWNER,'PILOT_MODEL_MONTHLY_MICRO_USD':'500000',
        'PILOT_MODEL_MAX_CALLS_DAY':'100','PILOT_MODEL_MAX_CALLS_MONTH':'300','PILOT_HISTORY_ENABLED':'true',
        'PILOT_HISTORY_STATE_ENABLED':'true','PILOT_CALENDAR_ENABLED':'true' if enabled else 'false','PILOT_CALENDAR_ID':CALENDAR}

class Hosted:
    def __init__(self):
        self.now=NOW;self.counter=0;self.queue=[];self.model_bodies=[];self.calendar=FakeCalendar();self.calendar_requests=[];self.scopes=[];self.reject_reply=False
        self.google=MockApiServices(id_column='E');self.google.row_count=1001
        self.google.cells={k:v for k,v in self.google.cells.items() if k[0]!='Database' or k[1]==1}
        for col,value in enumerate(['กาแฟ','Food',''],1):self.google.cells[('Database',2,col)]=value
        state=FirestoreState(RestClient('firestore',httpx.MockTransport(self.handle)),OWNER,project_id=PROJECT_ID,
            collection_id='assistant_pilot_states',clock=lambda:self.now,max_snapshot_bytes=800000,compact_snapshots=True)
        with state.session():state.production_ready=True;state.workbook_id=MAIN_WORKBOOK_ID;state.checkpoint()
    def snapshot(self):
        doc=next(v for k,v in self.google.documents.items() if '/assistant_pilot_states/' in k)
        return decode(doc['fields']['snapshot']['stringValue'],800000)
    def handle(self,request):
        host=request.url.host
        if host=='metadata.google.internal':
            if request.url.path.endswith('/email'):return httpx.Response(200,text=SERVICE_ACCOUNT,headers={'Metadata-Flavor':'Google'})
            self.scopes.append(request.url.params.get('scopes'))
            return httpx.Response(200,json={'access_token':'synthetic-token','expires_in':3600,'token_type':'Bearer'},headers={'Metadata-Flavor':'Google'})
        if host=='api.openai.com':
            body=json.loads(request.content);self.model_bodies.append(body);value=self.queue.pop(0)
            return httpx.Response(200,json=value(body) if callable(value) else value)
        if host=='www.googleapis.com':
            base='/calendar/v3/calendars/'+CALENDAR+'/events';path=unquote(request.url.path);assert path.startswith(base)
            self.calendar_requests.append((request.method,path))
            fake=httpx.Request(request.method,'https://www.googleapis.com/calendar/v3/calendars/synthetic-calendar/events'+path[len(base):],content=request.content)
            return self.calendar.request(fake)
        if host=='api.line.me' and self.reject_reply:return httpx.Response(403,json={})
        result=self.google.handle(request)
        if host=='sheets.googleapis.com':
            body=result.json()
            if 'spreadsheetId' in body:body['spreadsheetId']=MAIN_WORKBOOK_ID
            return httpx.Response(result.status_code,json=body)
        return result
    def app(self,enabled=True):
        return create_history_cloud_run_app(values(enabled),transport=httpx.MockTransport(self.handle),secret_loader=lambda name:SecretStr('synthetic-'+name),clock=lambda:self.now)
    def event(self,text,postback=False):
        self.counter+=1;self.now+=timedelta(seconds=1)
        return {'webhookEventId':'calendar-hosted-'+str(self.counter),'timestamp':int(self.now.timestamp()*1000),
            'source':{'type':'user','userId':OWNER},'replyToken':'synthetic-reply',
            'type':'postback' if postback else 'message',**({'postback':{'data':text}} if postback else {'message':{'id':str(self.counter),'type':'text','text':text}})}
    def send(self,client,event):
        raw=json.dumps({'events':[event]},ensure_ascii=False,separators=(',',':')).encode()
        sig=base64.b64encode(hmac.new(b'synthetic-line-channel-secret',raw,hashlib.sha256).digest()).decode()
        return client.post('/webhooks/line',content=raw,headers={'x-line-signature':sig})
    def turn(self,client,text,*responses,postback=False):
        self.queue=list(responses);e=self.event(text,postback);r=self.send(client,e);assert r.status_code==200,r.text
        return result_from(self.snapshot()['receipts'][e['webhookEventId']]['result']),e

def prepare(body):
    c=json.loads(body['input'][0]['content']);return response('prepare_calendar_event',{'draft_ref':None,'draft_version':None,'title':'ตรวจฟัน',
        'date_text':'พรุ่งนี้','time_text':'บ่ายสอง','end_date_text':None,'end_time_text':None,'duration_text':'45 นาที','source_refs':[c['current_source_ref']]})

def test_signed_factory_create_delivery_cold_confirm_and_cold_replay():
    api=Hosted()
    with TestClient(api.app()) as client:
        preview,_=api.turn(client,'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',prepare)
        assert preview.status=='preview' and api.calendar.posts==0 and api.google.sheet_writes==0
    with TestClient(api.app()) as client:
        result,event=api.turn(client,'ยืนยัน')
        assert result.status=='created' and api.calendar.posts==1 and len(api.model_bodies)==1 and api.google.sheet_writes==0
    with TestClient(api.app()) as client:
        assert api.send(client,deepcopy(event)).status_code==200
    assert api.calendar.posts==1 and len(api.calendar.events)==1 and len(api.model_bodies)==1
    assert 'https://www.googleapis.com/auth/calendar.events' in api.scopes

def test_undelivered_preview_cannot_authorize_creation():
    api=Hosted();api.reject_reply=True
    with TestClient(api.app()) as client:preview,_=api.turn(client,'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',prepare)
    assert preview.status=='preview'
    api.reject_reply=False
    with TestClient(api.app()) as client:result,_=api.turn(client,preview.data['confirm_command'])
    assert result.status=='stale' and api.calendar.posts==0 and len(api.model_bodies)==1

def test_feature_off_bridge_retains_calendar_draft_and_raw_records_then_resume():
    api=Hosted()
    with TestClient(api.app()) as client:preview,_=api.turn(client,'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',prepare)
    before=deepcopy(api.snapshot()['history24']['current']['calendar_v1']);raw={k:deepcopy(v) for k,v in api.google.documents.items() if '/assistant_raw_messages/' in k}
    with TestClient(api.app(enabled=False)) as client:
        result,_=api.turn(client,preview.data['confirm_command'])
        assert result.status=='stale' and api.calendar.posts==0
    assert api.snapshot()['history24']['current']['calendar_v1']==before
    assert all(api.google.documents[k]==v for k,v in raw.items())
    with TestClient(api.app()) as client:result,_=api.turn(client,preview.data['confirm_command'])
    assert result.status=='created' and api.calendar.posts==1

@pytest.mark.parametrize('updates',[{'PILOT_CALENDAR_ENABLED':'yes'},{'PILOT_CALENDAR_ID':''},{'PILOT_CALENDAR_ID':'primary'},{'PILOT_HISTORY_ENABLED':'false'}])
def test_config_rejects_incomplete_or_arbitrary_calendar_enablement(updates):
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**values(),**updates})

def test_off_flag_does_not_request_calendar_tokens_or_allow_routes():
    api=Hosted();app=api.app(enabled=False);http=app.state.pilot_http
    with rpc_budget(5):
        with pytest.raises(Exception):http.google_token('calendar_events')
    assert not api.scopes and not api.calendar_requests

def test_calendar_resource_and_method_allowlist_precedes_token_lookup():
    api=Hosted();app=api.app();http=app.state.pilot_http
    with rpc_budget(25),app.state.assistant.state.session():
        for method,path in [('POST','/calendar/v3/calendars/primary/events'),('POST','/calendar/v3/calendars/other/events'),('DELETE','/calendar/v3/calendars/'+CALENDAR+'/events/abc'),('GET','/calendar/v3/calendars/'+CALENDAR+'/acl')]:
            with pytest.raises(LiveDisabled):http.request('calendar',method,path,body={} if method=='POST' else None)
    assert not api.calendar_requests and 'https://www.googleapis.com/auth/calendar.events' not in api.scopes

def test_production_calendar_correction_reaches_owned_calendar_draft():
    api=Hosted()
    with TestClient(api.app()) as client:
        first,_=api.turn(client,'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',prepare)
        def correct(body):
            c=json.loads(body['input'][0]['content']);d=c['calendar_drafts'][0]
            return response('prepare_calendar_event',{'draft_ref':d['draft_ref'],'draft_version':d['version'],'title':None,
                'date_text':None,'time_text':'บ่ายสาม','end_date_text':None,'end_time_text':None,'duration_text':None,'source_refs':[c['current_source_ref']]})
        fresh,_=api.turn(client,'แก้เป็นบ่ายสาม',correct)
        assert fresh.status=='preview' and '15:00' in fresh.message
        old,_=api.turn(client,first.data['confirm_command']);assert old.status=='stale' and api.calendar.posts==0
        result,_=api.turn(client,fresh.data['confirm_command']);assert result.status=='created' and api.calendar.posts==1

@pytest.mark.parametrize('word',['ยืนยัน','ยกเลิก'])
def test_calendar_and_legacy_expense_pending_require_domain_choice(word):
    from line_assistant.state import Pending
    api=Hosted();app=api.app()
    with TestClient(app) as client:
        preview,_=api.turn(client,'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',prepare)
        state=app.state.assistant.state
        with rpc_budget(25),state.session():
            state.pending=Pending('add_transactions','synthetic-legacy','old-legacy',int(api.now.timestamp()*1000),api.now+timedelta(minutes=20),
                {'entries':[{'date':'2026-10-16','item':'กาแฟ','amount':'23','note':'','kind':'expense'}]})
            state.checkpoint()
        result,_=api.turn(client,word)
        assert result.status=='clarification' and 'ทั้ง' in result.message
        assert api.calendar.posts==0 and api.google.sheet_writes==0 and len(api.model_bodies)==1
        assert api.snapshot()['pending'] is not None
        result,_=api.turn(client,preview.data['confirm_command'])
        assert result.status=='created' and api.calendar.posts==1 and api.google.sheet_writes==0 and api.snapshot()['pending'] is not None

def test_signed_cold_unknown_write_reconciles_once_and_bridge_preserves_action():
    api=Hosted()
    with TestClient(api.app()) as client:preview,_=api.turn(client,'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',prepare)
    api.calendar.fail_after_create=True
    with TestClient(api.app()) as client:
        reply=api.send(client,api.event(preview.data['confirm_command']))
        assert reply.status_code==503
    assert api.calendar.posts==1
    before=deepcopy(api.snapshot()['actions']);api.calendar.fail_after_create=False
    with TestClient(api.app(enabled=False)) as client:
        result,_=api.turn(client,'ตรวจผลนัด');assert result.status=='uncertain'
    assert api.calendar.posts==1 and api.snapshot()['actions']==before
    with TestClient(api.app()) as client:result,_=api.turn(client,'ตรวจผลนัด')
    assert result.status=='created' and api.calendar.posts==1 and len(api.model_bodies)==1

def test_synthetic_startup_health_and_signatures_need_no_provider_calls():
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[1]/'scripts/verify_history_signed_startup.py'
    spec=importlib.util.spec_from_file_location('calendar_startup_fixture',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for updates in [{'PILOT_CALENDAR_ENABLED':'false'},{},{'PILOT_CALENDAR_ENABLED':'false','PILOT_HISTORY_ENABLED':'false'}]:
        result=module.run_checks({**values(),**updates},secret_loader=lambda name:SecretStr('synthetic-'+name))
        assert result['status']=='passed' and result['provider_requests']==0
