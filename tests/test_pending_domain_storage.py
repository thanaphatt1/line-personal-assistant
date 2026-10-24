"""Cold mixed-domain flow on both stores and normalized evidence records."""
from copy import deepcopy
import json,socket
import pytest
from fastapi.testclient import TestClient
from expense_storage_hosted import StorageHosted
from state_records_hosted import RecordsProtocol
from test_previous_expense_prices import turn,prepare
from test_temporal_plans import resolve
from test_calendar_temporal_occurrences import proposed
from test_calendar_mvp import response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Only synthetic signed requests')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)


class RecordsHosted(RecordsProtocol,StorageHosted):pass


def change(item,field,value,quote):
    def plan(body):
        c=json.loads(body['input'][0]['content'])
        d=next(d for d in c['pending_records'] if d['domain']=='expense'
            and any(r['values']['item']==item for r in d['records']))
        record=next(r for r in d['records'] if r['values']['item']==item)
        return response('patch_pending_records',{'draft_ref':d['draft_ref'],'draft_version':d['version'],
            'records':[{'record_ref':record['record_ref'],'changes':[{'field':field,'value':value,
                'source_text':quote,'source_ref':c['current_source_ref'],'temporal_ref':None}]}]})
    return plan


@pytest.mark.parametrize('backend',['sheets','firestore'])
def test_new_catalog_write_calendar_request_and_return_to_old_expense_after_cold_restore(backend):
    api=RecordsHosted(backend)
    first,_=turn(api,'Spotify 35 บาท วันที่ 27',prepare(amounts={'Spotify':'35'},date_text='วันที่ 27'))
    assert first.status=='clarification',first
    first_ref=next(iter(api.drafts()));old=deepcopy(api.drafts()[first_ref])
    added,_=turn(api,'ชื่อทดสอบโนวา 40 บาท',prepare(('ชื่อทดสอบโนวา',),amounts={'ชื่อทดสอบโนวา':'40'}))
    assert added.status=='clarification',added
    reviewed,_=turn(api,'จัดของกิน',change('ชื่อทดสอบโนวา','category','Food','ของกิน'))
    assert reviewed.status=='preview',reviewed
    assert turn(api,reviewed.data['confirm_command'])[0].status=='recorded'
    assert api.drafts()[first_ref]==old
    appointments,_=turn(api,'เพิ่มนัดโครงการออโรรา วันที่ 29 30 oct เวลา 8:00-17:00',
        resolve('29',kind='date',mode='components',date='2026-10-29'),
        resolve('30',kind='date',mode='components',date='2026-10-30'),proposed)
    assert appointments.status=='preview' and api.calendar.posts==0,appointments
    updated,_=turn(api,'Spotify เมื่อวาน',change('Spotify','date_text','เมื่อวาน','เมื่อวาน'))
    assert updated.status=='preview',updated
    current=api.drafts()[first_ref]
    assert current['entries'][0]['date']=='2026-10-17' and current['entries'][0]['amount']=='35'
    assert current['entries'][0]['fact_sources']['amount']==old['entries'][0]['fact_sources']['amount']
    saved,event=turn(api,updated.data['confirm_command'])
    assert saved.status=='recorded' and api.expense_writes==2 and api.calendar.posts==0,saved
    with TestClient(api.app()) as web:assert api.send(web,deepcopy(event)).status_code==200
    assert api.expense_writes==2 and api.calendar.posts==0
    assert api.snapshot()['version']==2
