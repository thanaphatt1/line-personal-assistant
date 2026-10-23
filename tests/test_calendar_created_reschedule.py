"""Created-event identity must survive cold continuation and use fresh ETags."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest
from fastapi.testclient import TestClient

from calendar_reschedule_hosted import RescheduleHosted,create,find_last,move,context
from test_calendar_text_confirmation_v13 import turn,mutate
from test_calendar_all_day_v15 import proposal,event
from test_calendar_mvp import response
from line_assistant.pilot_http import rpc_budget
from line_assistant.transport import LiveDisabled
from test_calendar_factory import CALENDAR


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic Calendar, LINE, model and state only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def created(api):
    review,_=turn(api,'เพิ่มนัดกินข้าวในวันที่ 4 พย ให้หน่อย ลงเวลาเป็น 16:00-22:00',create)
    assert review.status=='preview',review
    saved,_=turn(api,review.data['confirm_command']);assert saved.status=='created',saved
    return saved


def test_created_event_reschedules_after_28_minutes_without_repeating_month_or_title():
    api=RescheduleHosted();saved=created(api);identifier=saved.data['event_id']
    api.now+=timedelta(minutes=28)
    review,_=turn(api,'เปลี่ยนเป็นวันที่ 14 ได้มั้ย',find_last,move())
    assert review.status=='preview',review
    view=api.snapshot()['pending']['payload']
    assert view['before']['id']==identifier and view['before']['start']=='2026-11-04T16:00:00+07:00'
    assert view['after']['start']=='2026-11-14T16:00:00+07:00' and view['after']['end']=='2026-11-14T22:00:00+07:00'
    assert api.calendar.posts==1 and api.patches==0
    updated,confirmation=turn(api,'ยืนยัน');assert updated.status=='updated',updated
    assert api.calendar.posts==1 and api.patches==1 and updated.data['id']==identifier
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(confirmation)).status_code==200
    assert api.patches==1


@pytest.mark.parametrize('followup',['ปฏิทินที่เพิ่งให้ไปก่อนหน้านี้','แก้นัดกินข้าวเป็นวันที่ 14'])
def test_created_identity_still_resolves_after_intervening_clarification(followup):
    api=RescheduleHosted();saved=created(api);api.now+=timedelta(minutes=28)
    turn(api,'เปลี่ยนเป็นวันที่ 14 ได้มั้ย',response(text='ต้องการแก้รายการไหนครับ?'))
    if 'วันที่' in followup:
        result,_=turn(api,followup,find_last,move())
        assert result.status=='preview',result
    else:
        result,_=turn(api,followup,find_last,response(text='พบนัดเดิมแล้วครับ'))
        # Read/select is sufficient here; the next proposal still needs source citations.
        assert result.data.get('tool_result',{}).get('status')!='saved_edit_unavailable',result
        assert api.snapshot()['pending']['payload']['candidates'][0]['record']['id']==saved.data['event_id']
    assert api.calendar.posts==1 and api.patches==0


def test_followup_after_first_reschedule_uses_updated_result_identity():
    api=RescheduleHosted();saved=created(api)
    turn(api,'เปลี่ยนเป็นวันที่ 14',find_last,move());turn(api,'ยืนยัน')
    review,_=turn(api,'เลื่อนไปวันที่ 15',find_last,move('2026-11-15','วันที่ 15'))
    assert review.status=='preview',review
    updated,_=turn(api,'ยืนยัน')
    assert updated.status=='updated' and updated.data['id']==saved.data['event_id']
    assert updated.data['after']['start']=='2026-11-15T16:00:00+07:00' and api.calendar.posts==1 and api.patches==2


@pytest.mark.parametrize('kind',['expired','foreign_calendar','uncertain','wrong_id'])
def test_recent_creation_requires_unexpired_completed_matching_calendar_proof(kind):
    api=RescheduleHosted();created(api)
    if kind=='expired':api.now+=timedelta(days=1,seconds=1)
    else:
        def change(state):
            action=next(a for a in state.actions.values() if a.kind=='create_event')
            if kind=='foreign_calendar':action.metadata['calendar_id']='foreign@group.calendar.google.com'
            elif kind=='uncertain':action.status='uncertain'
            else:action.result.data['event_id']='different-event'
        mutate(api,change)
    result,_=turn(api,'แก้นัดล่าสุดเป็นวันที่ 14',find_last)
    assert result.status!='preview' and api.calendar.posts==1 and api.patches==0,result


def test_created_all_day_event_moves_without_changing_mode_or_duration():
    api=RescheduleHosted()
    review,_=turn(api,'เพิ่มนัดกิจกรรมทดสอบ 4 พ.ย. ทั้งวัน',proposal([event(title='กิจกรรมทดสอบ',day='4 พ.ย.')]))
    assert review.status=='preview'
    saved,_=turn(api,review.data['confirm_command']);assert saved.status=='created'
    review,_=turn(api,'เปลี่ยนเป็นวันที่ 14',find_last,move())
    assert review.status=='preview',review
    updated,_=turn(api,'ยืนยัน')
    assert updated.status=='updated' and updated.data['after']['all_day']
    assert updated.data['after']['start']=='2026-11-14' and updated.data['after']['end']=='2026-11-15'
    assert api.calendar.posts==1 and api.patches==1


def test_created_event_reschedule_keeps_conditional_version_check():
    api=RescheduleHosted();saved=created(api)
    review,_=turn(api,'เปลี่ยนเป็นวันที่ 14',find_last,move());assert review.status=='preview'
    api.patch_race=True
    result,_=turn(api,'ยืนยัน')
    assert result.status=='conflict' and api.calendar.events[saved.data['event_id']]['start']['dateTime'].startswith('2026-11-04')
    assert api.calendar.posts==1 and api.patches==1


def test_single_event_batch_creation_can_be_rescheduled_by_its_completed_child():
    api=RescheduleHosted()
    def batch(body):
        result=create(body)
        args=json.loads(result['output'][0]['arguments'])
        return response('prepare_calendar_events',{'batch_ref':None,'batch_version':None,'events':[args]})
    review,_=turn(api,'เพิ่มนัดกินข้าววันที่ 4 พย 16:00-22:00',batch)
    assert review.status=='preview',review
    saved,_=turn(api,review.data['confirm_command']);assert saved.status=='created'
    api.now+=timedelta(minutes=28)
    review,_=turn(api,'เปลี่ยนเป็นวันที่ 14',find_last,move())
    assert review.status=='preview',review
    updated,_=turn(api,'ยืนยัน');assert updated.status=='updated'
    assert api.calendar.posts==1 and api.patches==1


@pytest.mark.parametrize('mutation',['receipt_key','payload_hash','result_id','status','calendar'])
def test_transport_creation_proof_refuses_unbound_ids_before_token_lookup(mutation):
    api=RescheduleHosted();saved=created(api);app=api.app();host=app.state.assistant
    with rpc_budget(30),host.state.session():
        state=host.state;key=next(k for k,a in state.actions.items() if a.kind=='create_event');action=state.actions[key]
        if mutation=='receipt_key':state.actions['foreign:calendar:unbound']=state.actions.pop(key)
        elif mutation=='payload_hash':action.metadata['calendar_payload_hash']='foreign'
        elif mutation=='result_id':action.result.data['event_id']='foreign'
        elif mutation=='status':action.status='prepared'
        else:action.metadata['calendar_id']='foreign@group.calendar.google.com'
        before=len(api.scopes)
        with pytest.raises(LiveDisabled):
            app.state.pilot_http.request('calendar','GET','/calendar/v3/calendars/'+CALENDAR+'/events/'+saved.data['event_id'])
        assert len(api.scopes)==before
    app.state.pilot_http.close()


def test_unknown_reschedule_reconciles_without_second_patch_and_keeps_recent_identity():
    api=RescheduleHosted();saved=created(api)
    turn(api,'เปลี่ยนเป็นวันที่ 14',find_last,move());api.patch_lost_ack=True
    uncertain,_=turn(api,'ยืนยัน');assert uncertain.status=='uncertain'
    api.reschedule_enabled=False
    result,_=turn(api,'ตรวจผล',response('check_saved_record_change',{}))
    assert result.data['tool_result']['status']=='saved_edit_status' and api.patches==1
    api.reschedule_enabled=True
    review,_=turn(api,'เลื่อนไปวันที่ 15',find_last,move('2026-11-15','วันที่ 15'))
    assert review.status=='preview' and api.patches==1,review
