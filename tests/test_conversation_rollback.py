from copy import deepcopy
from datetime import timedelta
import json,os,subprocess,sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from line_assistant.cloud_run import create_cloud_run_app
from line_assistant.firestore_state import FirestoreState
from line_assistant.persistence import state_snapshot,restore_state
from line_assistant.pilot_config import PilotConfig
from line_assistant.pilot_http import rpc_budget
from line_assistant.snapshot_codec import encode
from line_assistant.state import MemoryState,Action
from line_assistant.transport import RestClient
from line_assistant.readonly_preflight import PROJECT_ID
from test_conversation import Harness,NOW,deny_network
from test_conversation_transport import HostedConversation,config
from test_cloud_run import OWNER,fake_secrets

ROOT=Path(__file__).resolve().parents[1]

def test_plain_json_is_default_even_for_new_conversation_feature():
 values=config();assert PilotConfig.from_mapping(values).conversation_enabled
 assert PilotConfig.from_mapping(values).state_codec_enabled is False
 api=HostedConversation();app=api.app()
 assert app.state.assistant.state.compact_snapshots is False and app.state.assistant.state.max_snapshot_bytes==200000
 app.state.pilot_http.close()

def test_decoder_bridge_can_read_large_state_with_conversation_off_and_preserve_proofs():
 api=HostedConversation();state=FirestoreState(RestClient('firestore',__import__('httpx').MockTransport(api.handle)),OWNER,
  project_id=PROJECT_ID,collection_id='assistant_pilot_states',clock=lambda:api.now,max_snapshot_bytes=800000,compact_snapshots=True)
 with state.session():
  state.recent_turns=[{'role':'user','text':'keep current context','expires_at':(NOW+timedelta(minutes=30)).isoformat()}]
  for i in range(300):
   state.actions[f'{OWNER}:committed-{i}:add_transactions']=Action('add_transactions',{'entries':[]},
    {'immutable':'x'*1000},NOW+timedelta(minutes=30),NOW+timedelta(days=30),status='completed')
  state.checkpoint()
 before=deepcopy(api.snapshot());values={**config(),'PILOT_CONVERSATION_ENABLED':'false','PILOT_STATE_CODEC_ENABLED':'true'}
 app=create_cloud_run_app(values,transport=__import__('httpx').MockTransport(api.handle),secret_loader=fake_secrets,clock=lambda:api.now)
 assert getattr(app.state.assistant,'supports_postback',False) is False
 with rpc_budget(96),app.state.assistant.state.session():
  assert len(app.state.assistant.state.actions)==300 and app.state.assistant.state.recent_turns==before['recent_turns']
 assert api.snapshot()==before and api.models==api.google.sheet_writes==0
 app.state.pilot_http.close()

def test_maintenance_rollback_disables_all_provider_and_credential_access():
 values={**config(),'PILOT_STATE_CODEC_ENABLED':'true','PILOT_LAUNCH_AUTHORIZED':'false','PILOT_CONVERSATION_ENABLED':'false'}
 app=create_cloud_run_app(values,secret_loader=lambda n:pytest.fail('Loaded credential during maintenance rollback'))
 with TestClient(app) as client:
  assert client.get('/health').json()['live_requests_enabled'] is False
  assert client.post('/webhooks/line',content=b'{}').status_code==503

