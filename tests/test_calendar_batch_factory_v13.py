"""Signed factory, fake metadata/Google/LINE/model, real production fences."""
from copy import deepcopy
import json,socket
import httpx,pytest
from fastapi.testclient import TestClient
from test_calendar_factory import Hosted
from test_calendar_mvp import response

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Only synthetic injected services')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def proposed(body,count=2):
    c=json.loads(body['input'][0]['content']);values=[]
    for i in range(count):
        values.append({'draft_ref':None,'draft_version':None,'title':'กิจกรรมทดสอบ'+chr(65+i),'date_text':str(22+i)+' Oct','time_text':'8:00','end_date_text':None,'end_time_text':'17:00','duration_text':None,'source_refs':[c['current_source_ref']]})
    return response('prepare_calendar_events',{'batch_ref':None,'batch_version':None,'events':values})
def text(count=2):return 'เพิ่มนัด '+' และ '.join(str(22+i)+' Oct กิจกรรมทดสอบ'+chr(65+i) for i in range(count))+' เวลา 8:00-17:00 ทั้งหมด'

def test_signed_batch_delivery_cold_confirmation_and_replay():
    api=Hosted()
    with TestClient(api.app()) as client:
        r,_=api.turn(client,text(),proposed);assert r.status=='preview',r
    with TestClient(api.app()) as client:
        saved,event=api.turn(client,r.data['confirm_command']);assert saved.status=='created',saved
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.calendar.posts==2 and len(api.model_bodies)==1 and api.google.sheet_writes==0

def test_undelivered_batch_review_cannot_authorize_any_event():
    api=Hosted();api.reject_reply=True
    with TestClient(api.app()) as client:r,_=api.turn(client,text(),proposed)
    api.reject_reply=False
    with TestClient(api.app()) as client:got,_=api.turn(client,r.data['confirm_command'])
    assert got.status=='stale' and api.calendar.posts==0

def test_calendar_off_bridge_preserves_full_batch_and_raw_history():
    api=Hosted()
    with TestClient(api.app()) as client:r,_=api.turn(client,text(),proposed)
    before=deepcopy(api.snapshot()['history24']['current']['calendar_batches_v1'])
    with TestClient(api.app(False)) as client:got,_=api.turn(client,r.data['confirm_command'])
    assert got.status=='stale' and api.calendar.posts==0
    assert api.snapshot()['history24']['current']['calendar_batches_v1']==before

def test_partial_second_permission_denial_retains_success_and_pending_members():
    api=Hosted();original=api.calendar.request
    def deny_second(request):
        if request.method=='POST' and api.calendar.posts==1:
            api.calendar.posts+=1;return httpx.Response(403,json={})
        return original(request)
    # Hosted resolves this attribute for every request, so no real transport.
    api.calendar.request=deny_second
    with TestClient(api.app()) as client:
        r,_=api.turn(client,text(),proposed);got,_=api.turn(client,r.data['confirm_command'])
        assert got.status=='partial' and len(got.data['created_refs'])==1 and len(got.data['pending_refs'])==1,got
    state=api.snapshot()['history24']['current'];drafts=list(state['calendar_v1']['drafts'].values())
    assert sorted(d['status'] for d in drafts)==['needs_information','recorded'] and api.calendar.posts==2

def test_eight_events_finish_in_bounded_chunks_read_only_checks_never_advance_unattempted_events():
    api=Hosted()
    with TestClient(api.app()) as client:
        r,_=api.turn(client,text(8),lambda body:proposed(body,8));assert r.status=='preview',r
        from line_assistant.raw_history import PLANNER_MAX_OUTPUT_TOKENS
        assert api.model_bodies[-1]['max_output_tokens']==PLANNER_MAX_OUTPUT_TOKENS
        result,_=api.turn(client,r.data['confirm_command']);assert result.status=='in_progress' and api.calendar.posts==2,result
    with TestClient(api.app()) as client:
        read,event=api.turn(client,'ตรวจผลชุดนัด');assert read.status=='in_progress' and api.calendar.posts==2
        assert api.send(client,deepcopy(event)).status_code==200 and api.calendar.posts==2
        for expected in [4,6,8]:
            result,_=api.turn(client,'ดำเนินชุดนัดต่อ');assert api.calendar.posts==expected,result
        assert result.status=='created' and len(result.data['created_refs'])==8
    assert len(api.calendar.events)==8 and len(api.model_bodies)==1 and api.google.sheet_writes==0


def test_workspace_switch_keeps_calendar_sets_and_member_proofs_isolated():
    from line_assistant.pilot_config import MAIN_WORKBOOK_ID,TEST_WORKBOOK_ID
    from line_assistant.pilot_http import rpc_budget
    api=Hosted()
    with TestClient(api.app()) as client:api.turn(client,text(),proposed)
    app=api.app();state=app.state.assistant.state
    with rpc_budget(96),state.session():
        before=deepcopy([state.calendar_drafts,state.calendar_proofs,state.calendar_batches,state.calendar_batch_proofs])
        assert all(before)
        state.select_workbook(TEST_WORKBOOK_ID,legacy_workbook=MAIN_WORKBOOK_ID)
        assert not any([state.calendar_drafts,state.calendar_proofs,state.calendar_batches,state.calendar_batch_proofs])
        state.checkpoint()
        state.select_workbook(MAIN_WORKBOOK_ID,legacy_workbook=TEST_WORKBOOK_ID)
        assert [state.calendar_drafts,state.calendar_proofs,state.calendar_batches,state.calendar_batch_proofs]==before
    app.state.pilot_http.close()


def test_cold_partial_unknown_batch_survives_off_bridge_then_reconciles_without_post():
    api=Hosted();original=api.calendar.request
    def lose_second(request):
        if request.method=='POST' and api.calendar.posts==1:api.calendar.fail_after_create=True
        return original(request)
    api.calendar.request=lose_second
    with TestClient(api.app()) as client:
        preview,_=api.turn(client,text(),proposed)
        assert api.send(client,api.event(preview.data['confirm_command'])).status_code==503
    assert api.calendar.posts==2 and len(api.calendar.events)==2
    before=deepcopy(api.snapshot());api.calendar.fail_after_create=False
    with TestClient(api.app(False)) as client:
        result,_=api.turn(client,'ตรวจผลชุดนัด');assert result.status=='uncertain'
    after=api.snapshot()
    assert after['actions']==before['actions']
    assert after['history24']['current']['calendar_batches_v1']==before['history24']['current']['calendar_batches_v1']
    assert after['history24']['current']['calendar_v1']==before['history24']['current']['calendar_v1']
    with TestClient(api.app()) as client:
        result,_=api.turn(client,'ตรวจผลชุดนัด');assert result.status=='created'
    assert api.calendar.posts==2 and len(api.model_bodies)==1 and api.google.sheet_writes==0
