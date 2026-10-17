"""Signed production factory through synthetic HTTP; no real account fixtures."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json,socket
import httpx,pytest
from fastapi.testclient import TestClient
from line_assistant.cloud_run import create_cloud_run_app
from line_assistant.history_cloud_run import create_history_cloud_run_app,HistoryConfig
from line_assistant.history_first import INSTRUCTIONS,SEMANTIC_INSTRUCTIONS,tools
from line_assistant.history_context import source_ref,HistoryState,build_request,digest
from line_assistant.persistence import restore_state,state_snapshot,result_from
from line_assistant.pilot_config import PilotConfig,MAIN_WORKBOOK_ID,TEST_WORKBOOK_ID
from line_assistant.pilot_http import rpc_budget
from line_assistant.state import MemoryState
from line_assistant.runtime_config import ConfigError
from test_conversation_transport import HostedConversation,config
from test_cloud_run import fake_secrets
from test_history_first import response,data
from test_model_api import response as intent_response
from test_conversation import decision
from pathlib import Path

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No network in production history tests')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

class Hosted(HostedConversation):
    def __init__(self):
        super().__init__();self.now=datetime(2026,10,13,12,tzinfo=timezone.utc);self.queue=[]
        for col,value in enumerate(['ค่ากระดาษ','Work/Office','Supplies'],1):self.google.cells[('Database',6,col)]=value
    def handle(self,request):
        if request.url.host=='api.openai.com':
            v=self.queue.pop(0);self.value=v(json.loads(request.content)) if callable(v) else v
        return super().handle(request)
    def app(self,*,enabled=True,semantic_enabled=True,raw_history_enabled=False):
        return create_history_cloud_run_app({**config(),'PILOT_HISTORY_STATE_ENABLED':'true','PILOT_HISTORY_ENABLED':'true' if enabled else 'false'},
            transport=httpx.MockTransport(self.handle),secret_loader=fake_secrets,clock=lambda:self.now,semantic_enabled=semantic_enabled,raw_history_enabled=raw_history_enabled)
    def send_text(self,client,text,*values):
        self.queue=list(values);e=self.event(text=text);r=self.send(client,e);assert r.status_code==200,r.text
        return result_from(self.snapshot()['receipts'][e['webhookEventId']]['result']),e
    def text(self,client,text,intent,**kw):
        return self.send_text(client,text,intent_response('interpret_message',decision(intent,**kw)))[0]
    def create(self,client,text='วันที่ 10 ค่ากระดาษ 228 บาท',**kw):
        def prepare(body):
            c=json.loads(body['input'][0]['content'])
            return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[data(source_refs=[c['current_source_ref']],**kw)]})
        return self.send_text(client,text,prepare,response(text='ขอข้อมูลเพิ่มเติม'))[0]
    def update(self,client,text,**changes):
        def read(body):
            c=json.loads(body['input'][0]['content']);return response(name='read_expense_drafts',args={'draft_ref':c['available_drafts'][0]['draft_ref']})
        def prepare(body):
            c=json.loads(body['input'][0]['content']);d=json.loads(body['input'][2]['output'])['drafts'][0];old=d['entries'][0]
            e=data(item=old['item'],amount=old['amount'],date=old['date'],note=old['note'],entry_ref=old['entry_ref'],source_refs=[c['current_source_ref']]);e.update(changes)
            return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['draft_version'],'entries':[e]})
        return self.send_text(client,text,read,prepare)[0]

def test_signed_real_factory_normal_topic_resume_amount_confirm_and_recreated_replay():
    api=Hosted();before=deepcopy(api.google.cells)
    with TestClient(api.app()) as client:
        first=api.create(client);assert first.status=='preview' and '2026-10-10' in first.message
        r,_=api.send_text(client,'ดาวฤกษ์คืออะไร',response(text='ดาวฤกษ์เปล่งแสงได้เอง'));assert r.status=='answer'
        resumed=api.update(client,'ค่ากระดาษที่ค้างอยู่เป็นเดือนตุลาคม',date='2026-10-10',date_text='เดือนตุลาคม');assert resumed.status=='preview'
        corrected=api.update(client,'แก้ยอดค่ากระดาษเป็น 250 บาท',amount=250);assert corrected.status=='preview'
        stale,_=api.send_text(client,first.data['confirm_command']);assert stale.status=='stale' and not api.google.sheet_writes
        count=api.models;r,ev=api.send_text(client,corrected.data['confirm_command']);assert r.status=='recorded' and api.models==count
    with TestClient(api.app()) as client:assert api.send(client,ev).status_code==200
    assert api.google.sheet_writes==1 and all(api.google.cells[k]==v for k,v in before.items())
    raw=api.snapshot();assert raw['version']==1 and raw['history24']['current']['drafts']
    assert next(iter(raw['history24']['current']['drafts'].values()))['status']=='recorded'
    assert all(body.get('instructions')==SEMANTIC_INSTRUCTIONS for body in api.model_bodies)
    assert any(msg[0]['type']=='flex' for msg in api.google.line_replies)
    assert all(run['decision'] is None for run in raw['model_runs'].values() if run['status']=='completed')

def test_future_day_partial_date_survives_factory_recreation_and_month_confirmation():
    api=Hosted()
    with TestClient(api.app()) as client:
        r=api.create(client,'วันที่ 20 ค่ากระดาษ 228 บาท');assert r.status=='clarification' and 'เดือนไหน' in r.message
    with TestClient(api.app()) as client:
        r=api.update(client,'กันยายน',date='2026-09-20',date_text='กันยายน');assert r.status=='preview'
        r,_=api.send_text(client,r.data['confirm_command']);assert r.status=='recorded'
    assert api.google.sheet_writes==1 and api.google.cells[('All Expenses',2,1)]==46285

def test_bridge_preserves_history_drafts_and_exact_confirmation_with_history_model_off():
    api=Hosted()
    with TestClient(api.app()) as client:r=api.create(client)
    before=deepcopy(api.snapshot());count=api.models
    with TestClient(api.app(enabled=False)) as client:
        ask,_=api.send_text(client,'แก้ยอดค่ากระดาษเป็น 250 บาท');assert ask.status=='clarification'
        assert api.snapshot()['history24']['current']['drafts']==before['history24']['current']['drafts']
        result,_=api.send_text(client,r.data['confirm_command']);assert result.status=='recorded'
    assert api.models==count and api.google.sheet_writes==1

def test_v1_core_restore_and_model_counters_survive_new_history_codec():
    api=Hosted();app=api.app();state=app.state.assistant.state
    with rpc_budget(96),state.session():
        before=state_snapshot(state,state.owner_id);state.checkpoint()
    raw=api.snapshot();plain=MemoryState();restore_state(plain,raw,state.owner_id)
    assert state_snapshot(plain,state.owner_id)==before
    assert raw['history24']['current']['history']==[] and state.max_snapshot_bytes==200000
    app.state.pilot_http.close()

def test_existing_legacy_pending_and_buttons_finish_under_history_without_synthetic_transfer():
    api=Hosted();legacy=HostedConversation.app(api)
    with TestClient(legacy) as client:
        # This synthetic pending is created by the actual old conversation.
        r=api.text(client,'รถเมล์ 20 วันที่ 2026-10-13','expense',entries=[{'item':'รถเมล์','amount':'20','date':'2026-10-13'}]);assert r.status=='preview'
        button=api.button(r,'บันทึก')
    with TestClient(api.app()) as client:assert api.send(client,button).status_code==200
    raw=api.snapshot();assert raw['pending'] is None and raw['receipts'][button['webhookEventId']]['result']['status']=='recorded'
    assert api.google.sheet_writes==1

def test_existing_legacy_unknown_item_preserves_enrollment_flow():
    api=Hosted()
    with TestClient(HostedConversation.app(api)) as client:
        r,_=api.send_text(client,'ปากกาทดสอบ 50 วันที่ 2026-10-13',intent_response('interpret_message',decision('expense',entries=[{'item':'ปากกาทดสอบ','amount':'50','date':'2026-10-13'}])))
        assert r.status=='clarification' and api.snapshot()['pending']
    with TestClient(api.app()) as client:
        r,_=api.send_text(client,'หมวด Work/Office',intent_response('interpret_message',decision('enroll_item',category_text='Work/Office')))
        r,_=api.send_text(client,'Supplies',intent_response('interpret_message',decision('enroll_item',subcategory_text='Supplies')))
        assert r.status=='preview' and not api.google.sheet_writes
        ev=api.button(r,'บันทึก');assert api.send(client,ev).status_code==200
    assert api.google.sheet_writes==1 and any(k[0]=='Database' and v=='ปากกาทดสอบ' for k,v in api.google.cells.items())

def test_history_owner_selected_new_name_mapping_requires_complete_review_before_atomic_enrollment():
    api=Hosted()
    with TestClient(api.app()) as client:
        r=api.create(client,'ปากกาทดสอบ 50 วันที่ 2026-10-13',item='ปากกาทดสอบ',amount=50);assert r.status=='clarification' and 'ร่างเดิม' in r.message and 'เพิ่มรายการใหม่' not in r.message
        r=api.update(client,'หมวด Food',category='Food',subcategory='');assert r.status=='preview' and 'เพิ่มชื่อใหม่ใน Database: Food' in r.message
        ref=next(iter(api.snapshot()['history24']['current']['drafts']));nonce=api.snapshot()['history24']['current']['drafts'][ref]['review']['nonce']
        assert not api.google.sheet_writes
        r,_=api.send_text(client,'ยืนยัน '+ref+' '+nonce);assert r.status=='recorded' and api.google.sheet_writes==1

def test_signed_invalid_and_foreign_owner_never_call_provider_in_new_factory():
    api=Hosted();count=api.models,len(api.google.calls)
    with TestClient(api.app()) as client:
        assert client.post('/webhooks/line',content=b'{"events":[]}',headers={'x-line-signature':'invalid'}).status_code==401
        ev=api.event(text='ค่ากระดาษ 228');ev['source']['userId']='U'+'b'*32;assert api.send(client,ev).status_code==200
    assert (api.models,len(api.google.calls))==count

@pytest.mark.parametrize('change',[{'PILOT_HISTORY_ENABLED':'true'},
    {'PILOT_HISTORY_STATE_ENABLED':'true','PILOT_CONVERSATION_ENABLED':'false'},
    {'PILOT_HISTORY_STATE_ENABLED':'true','PILOT_DOMAIN_TOOL_LOOP_ENABLED':'true'},
    {'PILOT_HISTORY_ENABLED':'yes','PILOT_HISTORY_STATE_ENABLED':'true'}])
def test_history_flags_fail_closed(change):
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**config(),**change})

def test_default_production_remains_old_conversation_when_flags_absent():
    api=Hosted();app=HostedConversation.app(api)
    from line_assistant.conversation import ConversationAssistant
    assert type(app.state.assistant) is ConversationAssistant and not HistoryConfig.from_mapping(config()).history_enabled
    app.state.pilot_http.close()

def test_history_workspace_archive_does_not_cross_other_workbook():
    api=Hosted()
    with TestClient(api.app()) as client:api.create(client)
    app=api.app();state=app.state.assistant.state
    with rpc_budget(96),state.session():
        before=deepcopy(state.drafts);state.select_workbook(TEST_WORKBOOK_ID,legacy_workbook=TEST_WORKBOOK_ID)
        assert not state.drafts;state.checkpoint()
        state.select_workbook(MAIN_WORKBOOK_ID,legacy_workbook=TEST_WORKBOOK_ID);assert state.drafts==before
    assert api.snapshot()['history24']['active_workbook']==MAIN_WORKBOOK_ID
    app.state.pilot_http.close()

@pytest.mark.parametrize('field',['version','fingerprint','owner','scope'])
def test_corrupt_history_frame_blocks_before_paid_model_or_write(field):
    api=Hosted()
    with TestClient(api.app()) as client:r=api.create(client)
    doc=next(iter(api.google.documents.values()));raw=api.snapshot();extra=raw['history24'];d=next(iter(extra['current']['drafts'].values()))
    if field=='version':d['version']+=1
    elif field=='fingerprint':extra['current']['draft_proofs'][d['draft_ref']]['fingerprint']='wrong'
    elif field=='owner':extra['current']['owner_id']='foreign'
    else:extra['active_workbook']=TEST_WORKBOOK_ID
    doc['fields']['snapshot']['stringValue']=json.dumps(raw);count=api.models
    with TestClient(api.app()) as client:assert api.send(client,api.event(text=r.data['confirm_command'])).status_code==503
    assert api.models==count and not api.google.sheet_writes

