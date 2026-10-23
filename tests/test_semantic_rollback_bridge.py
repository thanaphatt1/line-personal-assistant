"""Signed offline rollback bridge; synthetic providers and drafts only."""
from copy import deepcopy
import socket
import pytest
from fastapi.testclient import TestClient
from line_assistant.persistence import result_from
from test_production_history import Hosted

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Rollback tests must not contact any provider')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)

def button(api, preview, label='บันทึก'):
    event=api.event(text='synthetic owner button')
    event.pop('message');event['type']='postback'
    actions=preview.data['line_message']['contents']['footer']['contents']
    event['postback']={'data':next(b['action']['data'] for b in actions if b['action']['label']==label)}
    return event

@pytest.mark.parametrize('label,expected',[('บันทึก','recorded'),('ยกเลิกรายการนี้','cancelled')])
def test_new_delivered_controls_survive_history_model_disabled_bridge_and_replay(label,expected):
    api=Hosted()
    with TestClient(api.app()) as client:
        preview=api.create(client)
    event=button(api,preview,label)
    count=api.models
    with TestClient(api.app(enabled=False)) as client:
        assert api.send(client,event).status_code==200
        result=result_from(api.snapshot()['receipts'][event['webhookEventId']]['result'])
        assert result.status==expected
    with TestClient(api.app(enabled=False)) as client:
        assert api.send(client,deepcopy(event)).status_code==200
    assert api.models==count
    assert api.google.sheet_writes==(1 if expected=='recorded' else 0)

def test_bridge_rejects_stale_new_button_and_preserves_corrected_owned_facts():
    api=Hosted()
    with TestClient(api.app()) as client:
        old=api.create(client)
        corrected=api.update(client,'แก้ยอดค่ากระดาษเป็น 250 บาท',amount=250)
    stale=button(api,old);count=api.models
    before=deepcopy(api.snapshot()['history24']['current']['drafts'])
    with TestClient(api.app(enabled=False)) as client:
        assert api.send(client,stale).status_code==200
        result=result_from(api.snapshot()['receipts'][stale['webhookEventId']]['result'])
        assert result.status=='stale'
        assert api.snapshot()['history24']['current']['drafts']==before
    assert api.models==count and api.google.sheet_writes==0
    # The valid delivered button still binds the unchanged corrected payload;
    # rejecting another stale button cannot substitute that earlier amount.
    with TestClient(api.app(enabled=False)) as client:
        latest=button(api,corrected)
        assert api.send(client,latest).status_code==200
        assert result_from(api.snapshot()['receipts'][latest['webhookEventId']]['result']).status=='recorded'
    assert api.models==count and api.google.sheet_writes==1
    assert any(value==250 for (tab,row,col),value in api.google.cells.items() if tab=='All Expenses' and col==3)
