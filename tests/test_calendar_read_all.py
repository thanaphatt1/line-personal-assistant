"""All-calendar reads through signed webhooks and provider-derived route gates."""
from copy import deepcopy
import json
import socket
from urllib.parse import quote, unquote

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from line_assistant.calendar_provider import CALENDAR_LIST_PATH, CALENDAR_LIST_PARAMS
from line_assistant.history_cloud_run import HistoryConfig, create_history_cloud_run_app
from line_assistant.pilot_http import rpc_budget
from line_assistant.runtime_config import ConfigError
from line_assistant.transport import LiveDisabled
from test_calendar_factory import CALENDAR, values
from test_read_queries import event, plan
from test_response_realization import VoiceHosted

WORK='synthetic-work@group.calendar.google.com'
PERSONAL='synthetic-owner@example.invalid'
WINDOW={'timeMin':'2026-10-12T00:00:00+07:00','timeMax':'2026-10-19T00:00:00+07:00',
        'singleEvents':'true','orderBy':'startTime','timeZone':'Asia/Bangkok','maxResults':20}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('Only synthetic Calendar providers are permitted')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)


def listed(identifier,role='reader',**extra):
    return {'id':identifier,'accessRole':role,**extra}


class AllCalendarsHosted(VoiceHosted):
    def __init__(self):
        super().__init__()
        self.read_all=True
        self.calendar_read_ids=[]
        self.connections_enabled=False
        self.list_pages={'':{'items':[listed(WORK,hidden=True,selected=False),listed(PERSONAL)]}}
        self.event_pages={WORK:{'':{'items':[]}},PERSONAL:{'':{'items':[]}}}
        self.list_requests=[];self.extra_requests=[]

    def app(self,enabled=True):
        config={**values(enabled),'PILOT_CALENDAR_READ_ALL_ENABLED':'true' if self.read_all and enabled else 'false',
                'PILOT_CALENDAR_READ_IDS':json.dumps(self.calendar_read_ids),
                'PILOT_CALENDAR_CONNECTIONS_ENABLED':'true' if self.connections_enabled else 'false',
                'PILOT_RESPONSE_REALIZATION_ENABLED':'true',**getattr(self,'extra_config',{})}
        return create_history_cloud_run_app(config,transport=httpx.MockTransport(self.handle),
            secret_loader=lambda name:SecretStr('synthetic-'+name),clock=lambda:self.now)

    def handle(self,request):
        if request.url.host=='www.googleapis.com':
            path=unquote(request.url.path);page=request.url.params.get('pageToken','')
            if path==CALENDAR_LIST_PATH:
                assert request.method=='GET'
                self.list_requests.append(dict(request.url.params))
                value=self.list_pages[page]
                return httpx.Response(value if isinstance(value,int) else 200,json={} if isinstance(value,int) else value)
            for identifier,pages in self.event_pages.items():
                if path=='/calendar/v3/calendars/'+identifier+'/events':
                    assert request.method=='GET'
                    self.extra_requests.append((identifier,dict(request.url.params)))
                    value=pages[page]
                    return httpx.Response(value if isinstance(value,int) else 200,json={} if isinstance(value,int) else value)
        return super().handle(request)


def query(api):
    with TestClient(api.app()) as client:
        return api.turn(client,'สัปดาห์นี้มีนัดอะไรบ้าง',
            plan('calendar',start=None,date_mode='calendar_week',relative_offset=0))


def test_weekly_question_combines_hidden_personal_and_existing_calendars_and_replays_without_reads():
    api=AllCalendarsHosted()
    api.calendar.events={'old':event('old','2026-10-14T09:00:00+07:00','2026-10-14T10:00:00+07:00')}
    api.event_pages[WORK]['']['items']=[event('work','2026-10-16T16:00:00+07:00','2026-10-16T17:00:00+07:00')]
    api.event_pages[PERSONAL]['']['items']=[event('personal','2026-10-17','2026-10-18',all_day=True,transparency='transparent')]
    result,original=query(api)
    assert result.status=='answer' and result.data['tool_result']['complete']
    assert result.data['tool_result']['count']==3
    assert result.data['read_query']['start']=='2026-10-12' and result.data['read_query']['end']=='2026-10-18'
    assert not result.data['read_query']['upcoming']
    assert all('นัดจำลอง '+name in result.message for name in ['old','work','personal'])
    assert result.data['response_realization']['status']=='llm'
    assert api.list_requests==[{'maxResults':'20','minAccessRole':'reader','showHidden':'true','showDeleted':'false'}]
    assert {identifier for identifier,_ in api.extra_requests}=={WORK,PERSONAL}
    assert all(params['timeMin']==WINDOW['timeMin'] and params['timeMax']==WINDOW['timeMax'] for _,params in api.extra_requests)
    assert 'https://www.googleapis.com/auth/calendar.readonly' in api.scopes
    assert 'https://www.googleapis.com/auth/calendar.events' not in api.scopes
    assert not api.calendar.posts and not api.google.sheet_writes and not api.snapshot()['actions']
    before=(len(api.list_requests),len(api.extra_requests),len(api.calendar_requests),len(api.model_bodies))
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(original)).status_code==200
    assert (len(api.list_requests),len(api.extra_requests),len(api.calendar_requests),len(api.model_bodies))==before


def test_same_invitation_is_counted_once_but_equal_titles_and_recurrences_remain_distinct():
    api=AllCalendarsHosted()
    shared=event('main','2026-10-16T16:00:00+07:00','2026-10-16T17:00:00+07:00',iCalUID='invitation@example.invalid')
    api.calendar.events={'main':shared}
    copy={**deepcopy(shared),'id':'copy'}
    recurrence={**deepcopy(shared),'id':'next','start':{'dateTime':'2026-10-17T16:00:00+07:00'},'end':{'dateTime':'2026-10-17T17:00:00+07:00'}}
    unrelated={**deepcopy(shared),'id':'other','iCalUID':'unrelated@example.invalid'}
    api.event_pages[WORK]['']['items']=[copy,recurrence,unrelated]
    result,_=query(api)
    assert result.data['tool_result']['count']==3


def test_discovery_and_each_calendar_finish_pagination_before_counting():
    api=AllCalendarsHosted()
    api.list_pages={'':{'items':[listed(WORK)],'nextPageToken':'more'},'more':{'items':[listed(PERSONAL),listed(CALENDAR)]}}
    api.event_pages[WORK]={'':{'items':[],'nextPageToken':'events'},'events':{'items':[event('later','2026-10-17','2026-10-18',all_day=True)]}}
    result,_=query(api)
    assert result.data['tool_result']['complete'] and result.data['tool_result']['count']==1
    assert len(api.list_requests)==2 and api.list_requests[1]['pageToken']=='more'
    assert [params.get('pageToken') for identifier,params in api.extra_requests if identifier==WORK]==[None,'events']
    assert len(api.calendar_requests)==1  # The configured calendar was also discovered.


@pytest.mark.parametrize('fault',['list_denied','list_malformed','list_loop','list_capacity','events_denied','events_malformed','events_loop'])
def test_any_incomplete_calendar_prevents_false_complete_or_empty_answer(fault):
    api=AllCalendarsHosted()
    api.calendar.events={'known':event('known','2026-10-17','2026-10-18',all_day=True)}
    if fault=='list_denied':api.list_pages['']=403
    elif fault=='list_malformed':api.list_pages['']={'items':[{'id':WORK}]}
    elif fault=='list_loop':api.list_pages={'':{'items':[],'nextPageToken':'again'},'again':{'items':[],'nextPageToken':'again'}}
    elif fault=='list_capacity':api.list_pages['']={'items':[listed(f'calendar-{i}@example.invalid') for i in range(16)]}
    elif fault=='events_denied':api.event_pages[PERSONAL]['']=403
    elif fault=='events_malformed':api.event_pages[PERSONAL]['']={'items':[{'id':'broken'}]}
    elif fault=='events_loop':api.event_pages[PERSONAL]={'':{'items':[],'nextPageToken':'again'},'again':{'items':[],'nextPageToken':'again'}}
    result,_=query(api)
    output=result.data['tool_result']
    assert not output['complete'] and 'events' not in output and 'count' not in output
    assert '0 นัด' not in result.message and not api.calendar.posts and not api.google.sheet_writes
    if fault.startswith('list_'):assert not api.extra_requests and not api.calendar_requests


def test_busy_only_and_deleted_calendars_are_not_claimed_as_readable():
    api=AllCalendarsHosted()
    api.list_pages['']={'items':[listed(WORK,'freeBusyReader'),listed(PERSONAL,deleted=True)]}
    result,_=query(api)
    assert result.data['tool_result']['complete'] and result.data['tool_result']['count']==0
    assert not api.extra_requests and len(api.calendar_requests)==1


def test_feature_off_keeps_single_calendar_and_does_not_request_discovery_scope():
    api=AllCalendarsHosted();api.read_all=False
    api.event_pages[WORK]['']['items']=[event('hidden','2026-10-17','2026-10-18',all_day=True)]
    result,_=query(api)
    assert result.data['tool_result']['complete'] and result.data['tool_result']['count']==0
    assert not api.list_requests and not api.extra_requests
    assert 'https://www.googleapis.com/auth/calendar.readonly' not in api.scopes
    assert 'https://www.googleapis.com/auth/calendar.events' in api.scopes


def test_explicit_shared_calendar_is_read_without_calendar_list_registration():
    api=AllCalendarsHosted();api.read_all=False;api.calendar_read_ids=[PERSONAL]
    api.event_pages[PERSONAL]['']['items']=[event('personal','2026-10-17','2026-10-18',all_day=True)]
    result,_=query(api)
    assert result.data['tool_result']['complete'] and result.data['tool_result']['count']==1
    assert not api.list_requests and [identifier for identifier,_ in api.extra_requests]==[PERSONAL]
    assert len(api.calendar_requests)==1 and not api.calendar.posts and not api.google.sheet_writes
    assert 'https://www.googleapis.com/auth/calendar.readonly' not in api.scopes
    request=next(body for body in api.model_bodies if body.get('tool_choice')=='auto')
    assert PERSONAL not in json.dumps(request['input'])


def test_explicit_and_discovered_calendars_are_combined_without_duplicate_reads():
    api=AllCalendarsHosted();api.calendar_read_ids=[PERSONAL]
    result,_=query(api)
    assert result.data['tool_result']['complete']
    assert [identifier for identifier,_ in api.extra_requests].count(PERSONAL)==1


def test_explicit_shared_calendar_access_failure_prevents_false_zero():
    api=AllCalendarsHosted();api.read_all=False;api.calendar_read_ids=[PERSONAL]
    api.event_pages[PERSONAL]['']=403
    result,_=query(api)
    assert not result.data['tool_result']['complete'] and result.data['tool_result']['error_code']=='provider_failure'
    assert 'count' not in result.data['tool_result'] and not api.list_requests


def test_changing_explicit_calendar_scope_invalidates_previous_query_dates():
    api=AllCalendarsHosted();api.read_all=False
    query(api)
    api.calendar_read_ids=[PERSONAL]
    with TestClient(api.app()) as client:
        result,_=api.turn(client,'ขอดูอีกครั้ง',plan('calendar',start=None,date_mode='previous'))
    request=next(body for body in reversed(api.model_bodies) if body.get('tool_choice')=='auto')
    assert json.loads(request['input'][0]['content'])['previous_query'] is None
    assert result.data['tool_result']['error_code']=='date_required' and not api.extra_requests


def test_explicit_read_target_does_not_expand_write_or_discovery_routes():
    api=AllCalendarsHosted();api.read_all=False;api.calendar_read_ids=[PERSONAL]
    app=api.app();http=app.state.pilot_http
    target='/calendar/v3/calendars/'+quote(PERSONAL,safe='')+'/events'
    with rpc_budget(30),app.state.assistant.state.session():
        assert http.request('calendar','GET',target,params=WINDOW)=={'items':[]}
        for method,path in [('POST',target),('PATCH',target+'/event'),('DELETE',target+'/event'),('GET',CALENDAR_LIST_PATH)]:
            with pytest.raises(LiveDisabled):
                http.request('calendar',method,path,body={} if method in {'POST','PATCH'} else None,
                    params=CALENDAR_LIST_PARAMS if path==CALENDAR_LIST_PATH else None)
    assert not api.list_requests and not api.calendar.posts
    http.close()


@pytest.mark.parametrize('raw',[
    'null','{}','"synthetic@example.invalid"','["primary"]','["https://example.invalid/calendar"]',
    '["../outside"]','[true]','[{}]','["synthetic@example.invalid","synthetic@example.invalid"]',
    json.dumps([CALENDAR]),json.dumps([f'calendar-{i}@example.invalid' for i in range(16)]),
    '["synthetic@example.invalid"] trailing',[],
])
def test_explicit_read_ids_require_a_bounded_json_list_of_unique_calendar_ids(raw):
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**values(),'PILOT_CALENDAR_READ_IDS':raw})


def test_explicit_read_ids_require_calendar_history_enablement():
    with pytest.raises(ConfigError):
        HistoryConfig.from_mapping({**values(False),'PILOT_CALENDAR_READ_IDS':json.dumps([PERSONAL])})


@pytest.mark.parametrize('updates',[
    {'PILOT_CALENDAR_READ_ALL_ENABLED':'yes'},
    {'PILOT_CALENDAR_READ_ALL_ENABLED':True},
    {'PILOT_CALENDAR_READ_ALL_ENABLED':'true','PILOT_CALENDAR_ENABLED':'false'},
])
def test_all_calendar_configuration_requires_explicit_opt_in(updates):
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**values(),**updates})


def test_read_targets_require_complete_provider_discovery_and_current_owner_lease():
    api=AllCalendarsHosted();app=api.app();http=app.state.pilot_http
    target='/calendar/v3/calendars/'+quote(WORK,safe='')+'/events'
    with rpc_budget(30),app.state.assistant.state.session():
        with pytest.raises(LiveDisabled):http.request('calendar','GET',target,params=WINDOW)
        assert not any('/auth/calendar' in scope for scope in api.scopes) and not api.extra_requests
        http.request('calendar','GET',CALENDAR_LIST_PATH,params=CALENDAR_LIST_PARAMS)
        assert http.request('calendar','GET',target,params=WINDOW)=={'items':[]}
        before=(len(api.extra_requests),len(api.scopes))
        for method,path in [('POST',target),('PATCH',target+'/event'),('DELETE',target+'/event'),('GET',target+'/event'),('GET',target.replace('/events','/acl'))]:
            with pytest.raises(LiveDisabled):http.request('calendar',method,path,body={} if method in {'POST','PATCH'} else None)
        with pytest.raises(LiveDisabled):http.request('calendar','GET','/calendar/v3/calendars/foreign/events',params=WINDOW)
        assert (len(api.extra_requests),len(api.scopes))==before
    with rpc_budget(30),app.state.assistant.state.session():
        with pytest.raises(LiveDisabled):http.request('calendar','GET',target,params=WINDOW)
    http.close()


def test_incomplete_discovery_does_not_admit_partial_targets():
    api=AllCalendarsHosted();app=api.app();http=app.state.pilot_http
    api.list_pages['']={'items':[listed(WORK)],'nextPageToken':'next'}
    with rpc_budget(30),app.state.assistant.state.session():
        http.request('calendar','GET',CALENDAR_LIST_PATH,params=CALENDAR_LIST_PARAMS)
        with pytest.raises(LiveDisabled):http.request('calendar','GET','/calendar/v3/calendars/'+quote(WORK,safe='')+'/events',params=WINDOW)
    assert not api.extra_requests
    http.close()


@pytest.mark.parametrize('change',[{'maxResults':21},{'showHidden':'false'},{'minAccessRole':'freeBusyReader'},{'pageToken':'unbound'},{'syncToken':'unbound'}])
def test_unbounded_or_unbound_discovery_is_denied_before_token_lookup(change):
    api=AllCalendarsHosted();app=api.app();http=app.state.pilot_http
    with rpc_budget(30),app.state.assistant.state.session(),pytest.raises(LiveDisabled):
        http.request('calendar','GET',CALENDAR_LIST_PATH,params={**CALENDAR_LIST_PARAMS,**change})
    assert not any('/auth/calendar' in scope for scope in api.scopes) and not api.list_requests
    http.close()


def test_discovery_does_not_expand_creation_or_conflict_targets():
    from test_calendar_factory import prepare
    api=AllCalendarsHosted()
    query(api)
    with TestClient(api.app()) as client:
        preview,_=api.turn(client,'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',prepare)
        assert preview.status=='preview'
        result,_=api.turn(client,'ยืนยัน')
    assert result.status=='created' and api.calendar.posts==1
    assert len(api.list_requests)==1  # Conflict checks and writes use only the configured calendar.
    assert 'https://www.googleapis.com/auth/calendar.events' in api.scopes
