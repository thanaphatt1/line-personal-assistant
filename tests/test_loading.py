"""Delayed animation never sends a message or expands the chat audience."""
from types import SimpleNamespace
from threading import Event
import pytest
from line_assistant.loading import indicator,OWNER
from line_assistant.line_api import LineReplyApi
from test_raw_history import Harness


def test_slow_verified_owner_gets_one_animation_without_message_or_reply_token():
    h=Harness();seen=[];called=Event()
    class Client:
        service='line'
        def request(self,method,path,*,body):
            seen.append((method,path,body));called.set();return {}
    api=LineReplyApi(Client())
    with indicator(h.service,h.event('synthetic'),SimpleNamespace(api=api),delay=.001):
        assert called.wait(1)
    assert seen==[('POST','/v2/bot/chat/loading/start',{'chatId':'mock-owner','loadingSeconds':20})]
    assert OWNER.get() is None and not h.sheets.entries


@pytest.mark.parametrize('change',[{'source':{'type':'group','userId':'mock-owner'}},
    {'source':{'type':'user','userId':'foreign-owner'}},{'mode':'standby'},
    {'deliveryContext':{'isRedelivery':True}},{'deliveryContext':[]},{'type':'follow'}])
def test_non_owner_standby_and_redelivery_never_start_indicator(change):
    h=Harness();event=h.event('private');event.update(change);calls=[]
    api=SimpleNamespace(loading=lambda owner:calls.append(owner))
    with indicator(h.service,event,SimpleNamespace(api=api),delay=0):pass
    assert not calls


def test_fast_answer_cancels_timer_and_animation_failure_never_blocks_answer():
    h=Harness();calls=[]
    api=SimpleNamespace(loading=lambda owner:calls.append(owner))
    with indicator(h.service,h.event('fast'),SimpleNamespace(api=api),delay=10):pass
    assert not calls
    done=Event()
    def broken(owner):done.set();raise RuntimeError('private provider body')
    with indicator(h.service,h.event('slow'),SimpleNamespace(api=SimpleNamespace(loading=broken)),delay=.001):
        assert done.wait(1)
    assert not h.sheets.entries


def test_direct_loading_call_without_verified_scope_is_rejected():
    api=LineReplyApi(SimpleNamespace(service='line'))
    with pytest.raises(ValueError):api.loading('foreign-owner')


def test_signed_webhook_loading_uses_bound_production_route_and_metrics_include_delivery(monkeypatch,capsys):
    import httpx,json
    from line_assistant import loading
    from line_assistant.performance import summarize
    from test_calendar_semantic_queries import SemanticHosted
    from test_calendar_text_confirmation_v13 import turn
    from test_expense_query_operations import expense
    class ImmediateTimer:
        def __init__(self,delay,callback):self.callback=callback
        def start(self):self.callback()
        def cancel(self):pass
        def join(self,timeout):pass
    class Hosted(SemanticHosted):
        def __init__(self):super().__init__();self.indicators=[]
        def handle(self,request):
            if request.url.host=='api.line.me' and request.url.path==loading.PATH:
                self.indicators.append(json.loads(request.content));return httpx.Response(202,json={})
            return super().handle(request)
    monkeypatch.setattr(loading,'Timer',ImmediateTimer)
    api=Hosted();result,_=turn(api,'รายจ่ายเดือน ก.ย.',expense())
    assert result.status in {'answer','summary'} and len(api.indicators)==1
    assert set(api.indicators[0])=={'chatId','loadingSeconds'}
    rows=[json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{')]
    report=summarize(rows)
    assert report['attempts']==1 and report['scopes']=={'webhook':1}
    assert 'line_delivery' in report['latency_ms'] and not api.google.sheet_writes and not api.calendar.posts
