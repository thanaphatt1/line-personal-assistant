from copy import deepcopy
from datetime import datetime, timezone
import json

from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr
import pytest

from line_assistant.cloud_run import app_factory, create_cloud_run_app
from line_assistant.main import signature
from line_assistant.model_api import RESERVATION_MICRO_USD, request_body
from line_assistant.pilot_config import PilotConfig, SECRET_PATHS
from line_assistant.pilot_http import PilotHttp, rpc_budget
from line_assistant.readonly_preflight import SERVICE_ACCOUNT
from line_assistant.runtime_config import ConfigError
from line_assistant.transport import LiveDisabled
from test_model_api import response
from test_synthetic_integration import NativeIdsApi, TOKEN


OWNER = "U" + "a"*32  # Synthetic test identity only; never a default launch owner.
NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)


def launch_values():
    return {"PILOT_LAUNCH_AUTHORIZED":"true", "PILOT_LINE_OWNER_ID":OWNER,
            "PILOT_MODEL_MONTHLY_MICRO_USD":str(RESERVATION_MICRO_USD * 10)}


def fake_secrets(name):
    return SecretStr("synthetic-" + name)


def event(text="BTS 65", event_id="pilot-event", owner=OWNER):
    return {"type":"message", "webhookEventId":event_id, "timestamp":int(NOW.timestamp()*1000),
            "source":{"type":"user","userId":owner}, "message":{"type":"text","id":event_id,"text":text},
            "replyToken":"synthetic-reply-token"}


def send(client, ev, secret="synthetic-line-channel-secret"):
    body=json.dumps({"events":[ev]},ensure_ascii=False).encode()
    return client.post("/webhooks/line",content=body,headers={"x-line-signature":signature(body,secret)})


class HostedApi:
    def __init__(self):
        self.google=NativeIdsApi()
        self.models=self.lines=self.metadata=0
        self.value=response()
        self.identity=SERVICE_ACCOUNT
        self.bodies=[]

    def handle(self, request):
        if request.url.host == "metadata.google.internal":
            self.metadata+=1
            assert request.headers["Metadata-Flavor"] == "Google"
            if request.url.path.endswith("/email"):
                return httpx.Response(200,text=self.identity,headers={"Metadata-Flavor":"Google"})
            return httpx.Response(200,json={"access_token":TOKEN,"expires_in":3600,"token_type":"Bearer"},headers={"Metadata-Flavor":"Google"})
        if request.url.host == "api.openai.com":
            self.models+=1
            assert request.headers["Authorization"]=="Bearer synthetic-openai-api-key"
            snapshot=json.loads(next(iter(self.google.documents.values()))["fields"]["snapshot"]["stringValue"])
            assert snapshot["model_runs"]["pilot-event"]["status"]=="reserved"
            assert snapshot["model_budgets"]["2026-10"]["reserved_micro_usd"]==RESERVATION_MICRO_USD
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200,json=self.value)
        if request.url.host == "api.line.me":
            self.lines+=1
            assert request.url.path=="/v2/bot/message/reply"
            assert request.headers["Authorization"]=="Bearer synthetic-line-channel-access-token"
            return httpx.Response(200,json={"sentMessages":[{"id":"synthetic-message-id"}]})
        return self.google.handle(request)


def app(api, values=None):
    return create_cloud_run_app(values or launch_values(), transport=httpx.MockTransport(api.handle),
                                secret_loader=fake_secrets,clock=lambda:NOW)


def test_default_cloud_startup_does_not_read_secrets_or_create_http_or_accept_webhook():
    with TestClient(create_cloud_run_app(secret_loader=lambda name:pytest.fail("Loaded secret"))) as client:
        assert client.get("/health").json()=={"status":"ok","mode":"launch_disabled","live_requests_enabled":False}
        assert send(client,event()).status_code==503


@pytest.mark.parametrize("key,value", [("PILOT_MODEL_ID","gpt-6-sol"),("PILOT_SPREADSHEET_ID","main-workbook"),
                                      ("PILOT_REGION","asia-southeast1"),("PILOT_LINE_OWNER_ID","mock-owner"),
                                      ("PILOT_MODEL_MONTHLY_MICRO_USD","0"),("PILOT_UNRECOGNIZED","true")])
def test_launch_requires_exact_model_target_owner_budget_before_any_secret_load(key,value):
    values=launch_values()
    values[key]=value
    with pytest.raises(ConfigError):
        create_cloud_run_app(values,secret_loader=lambda name:pytest.fail("Loaded secret"))


def test_hosted_expense_and_new_controller_replay_mock_all_external_requests_once():
    api=HostedApi()
    with TestClient(app(api)) as client:
        assert send(client,event()).json()=={"ok":True}
    assert api.models==api.lines==api.google.sheet_writes==1
    assert len(api.google.documents)==1 and "/assistant_pilot_states/" in next(iter(api.google.documents))
    with TestClient(app(api)) as client:
        assert send(client,event()).json()=={"ok":True}
    assert api.models==api.lines==api.google.sheet_writes==1
    doc=next(iter(api.google.documents.values()))
    stored=json.loads(doc["fields"]["snapshot"]["stringValue"])
    assert stored["model_runs"]["pilot-event"]["status"]=="completed"
    assert stored["receipts"]["pilot-event"]["result"]["status"]=="recorded"
    assert stored["replies"]["pilot-event"]["status"]=="sent" and doc["fields"]["lease_owner"]["stringValue"]==""
    assert not any(name in json.dumps(api.bodies) for name in (OWNER,SERVICE_ACCOUNT,"transaction_id","Categories"))


def test_bad_signature_or_nonowner_has_zero_credentials_metadata_google_model_or_line_calls():
    api=HostedApi()
    with TestClient(app(api)) as client:
        assert send(client,event(),secret="wrong").status_code==401
        assert send(client,event(owner="U"+"b"*32)).status_code==200
    assert api.metadata==api.models==api.lines==api.google.sheet_writes==0
    assert api.google.native_calls==[] and api.google.documents=={}


def test_hosted_body_and_event_count_limits_fail_before_provider():
    api=HostedApi()
    with TestClient(app(api)) as client:
        assert send(client,event(text="a"*17000)).status_code==413
        body=json.dumps({"events":[event()]*6}).encode()
        assert client.post("/webhooks/line",content=body,headers={"x-line-signature":signature(body,"synthetic-line-channel-secret")}).status_code==400
    assert api.metadata==api.models==api.google.sheet_writes==0


def test_attached_sa_mismatch_blocks_before_google_or_paid_model():
    api=HostedApi()
    api.identity="wrong@synthetic.invalid"
    with TestClient(app(api)) as client:
        assert send(client,event()).status_code==503
    assert api.models==api.lines==api.google.sheet_writes==0 and api.google.documents=={}


@pytest.mark.parametrize("service,method,path",[("line","POST","/v2/bot/message/push"),("calendar","POST","/calendar/v3/events"),
                                             ("sheets","POST","/v4/spreadsheets/main:batchUpdate"),("firestore","DELETE","/v1/any")])
def test_transport_refuses_other_services_targets_and_mutations_before_token(service,method,path):
    http=PilotHttp(PilotConfig.from_mapping(launch_values()),{name:fake_secrets(name) for name in SECRET_PATHS},
                   transport=httpx.MockTransport(lambda request:pytest.fail("Network")))
    with rpc_budget(10),pytest.raises(LiveDisabled):
        http.request(service,method,path,body={})


def test_model_http_requires_durable_reservation_not_only_a_valid_body():
    http=PilotHttp(PilotConfig.from_mapping(launch_values()),{name:fake_secrets(name) for name in SECRET_PATHS},
                   transport=httpx.MockTransport(lambda request:pytest.fail("Network")))
    with rpc_budget(10),pytest.raises(LiveDisabled):
        http.request("model","POST","/v1/responses",body=request_body("BTS 65",NOW))


def test_normal_cloud_entrypoint_ignores_unrelated_env_credentials_and_existing_dotenv(monkeypatch):
    monkeypatch.delenv("PILOT_LAUNCH_AUTHORIZED",raising=False)
    monkeypatch.setenv("OPENAI_API_KEY","synthetic-unrelated-never-used")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS","/must/not/read")
    with TestClient(app_factory()) as client:
        assert client.get("/health").json()["mode"]=="launch_disabled"


@pytest.mark.parametrize("count,seconds",[(0,30),(1,0)])
def test_rpc_count_and_elapsed_deadline_stop_before_network(count,seconds):
    http=PilotHttp(PilotConfig.from_mapping(launch_values()),{name:fake_secrets(name) for name in SECRET_PATHS},
                   transport=httpx.MockTransport(lambda request:pytest.fail("Network")))
    with rpc_budget(count,seconds=seconds),pytest.raises(LiveDisabled):
        http._http("GET","http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email")


def test_secret_mounts_have_independent_directories_and_disabled_plan_does_not_authorize_budget_spend():
    from pathlib import PurePosixPath
    from line_assistant.deployment_plan import launch_plan
    assert len({str(PurePosixPath(path).parent) for path in SECRET_PATHS.values()})==3
    plan=launch_plan()
    assert plan["budget_configuration_required"] is True
    assert plan["monthly_micro_usd"]==0 and plan["launch_authorized"] is False and plan["execution_enabled"] is False
