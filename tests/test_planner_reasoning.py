"""Reasoning profile, accounting and HTTP fences using synthetic providers."""
from copy import deepcopy
import json,socket
import httpx,pytest
from fastapi.testclient import TestClient
from line_assistant import raw_history as raw
from line_assistant.history_first import parse_response
from line_assistant.history_context import HistoryState
from line_assistant.pilot_http import rpc_budget,take_rpc
from line_assistant.transport import LiveDisabled
from test_history_first import response,NOW
from test_raw_history import Harness
from test_production_history import Hosted


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)


@pytest.mark.parametrize('calendar',[None,[]])
def test_planner_enables_medium_and_reserves_reasoning_for_every_domain(calendar):
    state=HistoryState()
    body,_,_=raw.request(state,'คำถามทดสอบ',NOW,'reasoning-profile',[],calendar_drafts=calendar)
    assert body['reasoning']=={'effort':'medium'}
    assert body['max_output_tokens']==32768
    small={**body,'max_output_tokens':1024}
    assert raw.reservation(body)>raw.reservation(small)+15000
    with pytest.raises(raw.ContextOverflow):
        raw.request(state,'คำถามทดสอบ',NOW,'reasoning-profile',[],
            context_tokens=raw.tokens_bound(body)+32767,calendar_drafts=calendar)


def test_reasoning_response_accepts_total_usage_and_complete_call_without_weakening_bounds():
    value=response(name='read_expense_drafts',args={'draft_ref':None})
    value['usage'].update(output_tokens=6000,output_tokens_details={'reasoning_tokens':5900})
    value['output'].insert(0,{'type':'reasoning','id':'synthetic-reasoning','summary':[]})
    assert parse_response(value,max_output_tokens=raw.PLANNER_MAX_OUTPUT_TOKENS)['call']['name']=='read_expense_drafts'
    value['usage']['output_tokens']=32769
    with pytest.raises(OverflowError):parse_response(value,max_output_tokens=raw.PLANNER_MAX_OUTPUT_TOKENS)
    value['usage']['output_tokens']=6000;value.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
    with pytest.raises(ValueError):parse_response(value,max_output_tokens=raw.PLANNER_MAX_OUTPUT_TOKENS)


def test_incomplete_reasoning_never_retries_same_event_or_creates_a_draft():
    h=Harness();value=response(name='prepare_expenses',args={'entries':[]})
    value.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
    event=h.event('รายการทดสอบ 30');h.port.values=[value]
    result=h.service.handle(event)
    assert result.status=='model_unavailable' and not h.state.drafts and not h.sheets.entries
    assert result.data['model_failure']['kind']=='output_limit'
    held=h.state.model_budgets['2026-10'].reserved_micro_usd
    assert h.service.handle(deepcopy(event))==result
    assert len(h.port.requests)==1 and h.state.model_budgets['2026-10'].reserved_micro_usd==held


def test_planner_http_accepts_reserved_medium_profile_and_uses_longer_timeout():
    api=Hosted();timeouts=[]
    original=api.handle
    def handle(request):
        if request.url.host=='api.openai.com':timeouts.append(request.extensions['timeout']['read'])
        return original(request)
    app=api.app(raw_history_enabled=True)
    app.state.pilot_http.client.close()
    app.state.pilot_http.client=httpx.Client(transport=httpx.MockTransport(handle),trust_env=False,follow_redirects=False)
    with TestClient(app) as client:
        result,_=api.send_text(client,'คุยเรื่องทดสอบ',response(text='ตอบทดสอบ'))
        assert result.status=='answer' and timeouts==[raw.PLANNER_RPC_SECONDS]
        http=app.state.pilot_http
        for effort,output in [('none',32768),('high',32768),('medium',1024)]:
            body=deepcopy(api.model_bodies[0]);body.update(reasoning={'effort':effort},max_output_tokens=output)
            with rpc_budget(5),pytest.raises(LiveDisabled):http.request('model','POST','/v1/responses',body=body)
    assert api.models==1 and not api.google.sheet_writes


def test_longer_model_timeout_still_obeys_total_turn_deadline(monkeypatch):
    import line_assistant.pilot_http as transport
    clock=[10.0];monkeypatch.setattr(transport,'monotonic',lambda:clock[0])
    with rpc_budget(3,seconds=50):
        assert take_rpc()==12
        assert take_rpc(max_seconds=30)==30
        clock[0]=58
        assert take_rpc(max_seconds=30)==2
        with pytest.raises(LiveDisabled):take_rpc(max_seconds=30)
    with rpc_budget(3,seconds=1):
        clock[0]+=2
        with pytest.raises(LiveDisabled):take_rpc(max_seconds=30)
