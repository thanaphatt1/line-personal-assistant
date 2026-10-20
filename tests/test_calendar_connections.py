"""Signed, cold-restored LINE onboarding with synthetic calendars and roles."""
from copy import deepcopy
from datetime import timedelta
import json
from urllib.parse import quote

from fastapi.testclient import TestClient
import httpx
import pytest

from line_assistant.calendar_connections import probe_params,seal
from line_assistant.history_cloud_run import HistoryConfig
from line_assistant.history_context import HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.pilot_http import rpc_budget
from line_assistant.runtime_config import ConfigError
from line_assistant.transport import LiveDisabled
from test_calendar_factory import values,OWNER
from test_calendar_mvp import response
from test_calendar_read_all import AllCalendarsHosted,PERSONAL,WORK,WINDOW,query,offline
from test_read_queries import plan,event


class ConnectionsHosted(AllCalendarsHosted):
    def __init__(self):
        super().__init__();self.connections_enabled=True;self.read_all=False
        self.probe_requests=[];self.role='reader';self.probe_status=200
    def handle(self,request):
        if request.url.host=='www.googleapis.com' and request.url.params.get('fields'):
            self.probe_requests.append((request.method,str(request.url)))
            assert request.method=='GET' and request.url.params['fields']=='kind,accessRole'
            assert request.url.params['maxResults']=='1'
            return httpx.Response(self.probe_status,json={'kind':'calendar#events','accessRole':self.role})
        return super().handle(request)
    def registration(self):return self.snapshot()['history24']['current'].get('calendar_connections_v1')


def connect(operation,identifier=None,*,refs=None):
    def choose(body):
        context=json.loads(body['input'][0]['content'])
        assert 'connect_calendar' in {tool['name'] for tool in body['tools']}
        return response('connect_calendar',{'operation':operation,'calendar_id':identifier,
            'source_refs':refs if refs is not None else [context['current_source_ref']]})
    return choose


def turn(api,text,planner):
    with TestClient(api.app()) as client:return api.turn(client,text,planner)


def begin(api):return turn(api,'I want to add new calendar',connect('start'))


def test_line_flow_shares_configured_email_verifies_only_role_and_persists_for_weekly_reads():
    api=ConnectionsHosted()
    prompt,_=begin(api)
    assert prompt.status=='awaiting_calendar_id'
    assert values()['PILOT_CALENDAR_ID'] not in prompt.message
    assert api.app().state.pilot_http.config.service_account in prompt.message
    assert not api.probe_requests and not api.list_requests
    assert len(api.model_bodies)==1  # Deterministic setup text does not consume a second model call.
    connected,original=turn(api,PERSONAL,connect('add',PERSONAL))
    assert connected.status=='calendar_connected' and len(api.probe_requests)==1
    assert [row['id'] for row in api.registration()['calendars']]==[PERSONAL]
    assert api.registration()['pending'] is None
    assert not api.calendar.posts and not api.google.sheet_writes and not api.list_requests
    before=(len(api.model_bodies),len(api.probe_requests))
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(original)).status_code==200
    assert (len(api.model_bodies),len(api.probe_requests))==before
    api.event_pages[PERSONAL]['']['items']=[event('personal','2026-10-17','2026-10-18',all_day=True)]
    result,_=query(api)
    assert result.data['tool_result']['complete'] and result.data['tool_result']['count']==1
    assert 'นัดจำลอง personal' in result.message and not api.list_requests
    assert [identifier for identifier,_ in api.extra_requests]==[PERSONAL]


@pytest.mark.parametrize('claim',['เชื่อมปฏิทินให้เรียบร้อยแล้วครับ','Your calendar is now connected.'])
def test_model_prose_cannot_claim_connection_without_verified_tool_result(claim):
    api=ConnectionsHosted()
    result,_=turn(api,'เพิ่มปฏิทินใหม่',response(text=claim))
    assert result.status=='clarification' and api.registration() is None and not api.probe_requests


def test_embed_link_can_supply_literal_calendar_id():
    api=ConnectionsHosted();begin(api)
    result,_=turn(api,'https://calendar.google.com/calendar/embed?src='+quote(PERSONAL,safe='')+'&ctz=Asia%2FBangkok',connect('add',PERSONAL))
    assert result.status=='calendar_connected' and len(api.probe_requests)==1


@pytest.mark.parametrize('role',['freeBusyReader','none',None,'invented'])
def test_unverified_event_reading_permission_does_not_register_calendar(role):
    api=ConnectionsHosted();begin(api);api.role=role
    result,_=turn(api,PERSONAL,connect('add',PERSONAL))
    assert result.status=='connection_error' and not api.registration()['calendars']
    assert api.registration()['pending'] and not api.calendar.posts


@pytest.mark.parametrize('status',[401,403,404,410,429,500])
def test_failed_access_probe_preserves_setup_and_can_be_retried_after_sharing(status):
    api=ConnectionsHosted();begin(api);api.probe_status=status
    result,_=turn(api,PERSONAL,connect('add',PERSONAL))
    assert result.status=='connection_error' and not api.registration()['calendars']
    assert api.registration()['pending']
    api.probe_status=200
    result,_=turn(api,PERSONAL,connect('add',PERSONAL))
    assert result.status=='calendar_connected' and len(api.registration()['calendars'])==1


def test_no_connection_without_prior_delivered_onboarding():
    api=ConnectionsHosted()
    result,_=turn(api,PERSONAL,connect('add',PERSONAL))
    assert result.data['tool_result']['error_code']=='start_required'
    assert api.registration() is None and not api.probe_requests
    api.reject_reply=True;begin(api);api.reject_reply=False
    result,_=turn(api,PERSONAL,connect('add',PERSONAL))
    assert result.data['tool_result']['error_code']=='prompt_not_delivered'
    assert not api.registration()['calendars'] and not api.probe_requests


@pytest.mark.parametrize('text,identifier',[(PERSONAL,WORK),('ดูนัดของฉัน',PERSONAL),(PERSONAL,'primary'),(PERSONAL,'https://example.invalid/calendar')])
def test_tool_cannot_invent_or_replace_owner_calendar_id(text,identifier):
    api=ConnectionsHosted();begin(api)
    result,_=turn(api,text,connect('add',identifier))
    assert result.status=='connection_error' and not api.probe_requests and not api.registration()['calendars']


def test_assistant_or_old_message_reference_cannot_authorize_new_connection():
    api=ConnectionsHosted();prompt,_=begin(api)
    result,_=turn(api,PERSONAL,connect('add',PERSONAL,refs=['msg_'+'a'*20]))
    assert result.data['tool_result']['error_code']=='invalid_arguments' and not api.probe_requests


def test_onboarding_expiry_requires_new_request_and_keeps_saved_calendars():
    api=ConnectionsHosted();begin(api);turn(api,PERSONAL,connect('add',PERSONAL))
    begin(api);api.now+=timedelta(hours=25)
    result,_=turn(api,WORK,connect('add',WORK))
    assert result.data['tool_result']['error_code']=='start_required'
    assert [row['id'] for row in api.registration()['calendars']]==[PERSONAL]


def test_cancel_only_stops_setup_and_duplicate_connection_is_idempotent():
    api=ConnectionsHosted();begin(api);turn(api,PERSONAL,connect('add',PERSONAL))
    begin(api);result,_=turn(api,PERSONAL,connect('add',PERSONAL))
    assert result.status=='already_connected' and len(api.probe_requests)==1
    begin(api);result,_=turn(api,'ยกเลิกการเชื่อมปฏิทิน',connect('cancel'))
    assert result.status=='connection_cancelled'
    assert [row['id'] for row in api.registration()['calendars']]==[PERSONAL]
    assert api.registration()['pending'] is None


def test_same_source_feature_off_bridge_retains_registry_but_does_not_read_it():
    api=ConnectionsHosted();begin(api);turn(api,PERSONAL,connect('add',PERSONAL))
    before=deepcopy(api.registration());api.connections_enabled=False
    result,_=query(api)
    assert result.data['tool_result']['complete'] and result.data['tool_result']['count']==0
    assert api.registration()==before and not api.extra_requests
    request=next(body for body in reversed(api.model_bodies) if body.get('tool_choice')=='auto')
    assert 'connect_calendar' not in {tool['name'] for tool in request['tools']}
    api.connections_enabled=True
    query(api)
    assert [identifier for identifier,_ in api.extra_requests]==[PERSONAL]


def test_connected_calendar_get_is_permitted_but_writes_and_unstaged_probes_are_denied():
    api=ConnectionsHosted();begin(api);turn(api,PERSONAL,connect('add',PERSONAL))
    app=api.app();http=app.state.pilot_http
    target='/calendar/v3/calendars/'+quote(PERSONAL,safe='')+'/events'
    with rpc_budget(30),app.state.assistant.state.session():
        assert http.request('calendar','GET',target,params=WINDOW)=={'items':[]}
        for method,path in [('POST',target),('PATCH',target+'/event'),('DELETE',target+'/event')]:
            with pytest.raises(LiveDisabled):http.request('calendar',method,path,body={} if method in {'POST','PATCH'} else None)
        with pytest.raises(LiveDisabled):http.request('calendar','GET',target,params=probe_params(api.now))
        with pytest.raises(LiveDisabled):http.request('calendar','GET','/calendar/v3/calendars/foreign/events',params=WINDOW)
    assert not api.calendar.posts and len(api.probe_requests)==1
    http.close()


@pytest.mark.parametrize('fault',['owner','id','role','source','proof'])
def test_bad_persisted_connection_is_rejected_without_changing_live_state(fault):
    api=ConnectionsHosted();begin(api);turn(api,PERSONAL,connect('add',PERSONAL))
    value=deepcopy(api.registration())
    if fault=='owner':value['owner_id']='another-owner'
    if fault=='id':value['calendars'][0]['id']='primary'
    if fault=='role':value['calendars'][0]['access_role']='freeBusyReader'
    if fault=='source':value['calendars'][0]['source_ref']='invented'
    if fault!='proof':seal(value)
    else:value['proof']='a'*64
    state=HistoryState();state.owner_id=OWNER
    snapshot=history_snapshot(state);snapshot['calendar_connections_v1']=value
    with pytest.raises(ValueError):restore_history(state,snapshot,OWNER)
    assert state.calendar_connections is None and not state.history


@pytest.mark.parametrize('updates',[
    {'PILOT_CALENDAR_CONNECTIONS_ENABLED':'yes'},
    {'PILOT_CALENDAR_CONNECTIONS_ENABLED':'true','PILOT_CALENDAR_ENABLED':'false'},
])
def test_connections_require_explicit_calendar_history_configuration(updates):
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**values(),**updates})
