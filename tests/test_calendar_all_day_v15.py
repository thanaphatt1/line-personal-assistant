"""Date-only Calendar events through signed, persisted, wholly synthetic adapters."""
from copy import deepcopy
from datetime import date,timedelta
import hashlib,json,socket
import httpx,pytest
from fastapi.testclient import TestClient
from test_response_realization import VoiceHosted,generated
from test_calendar_text_confirmation_v13 import turn
from test_calendar_mvp import response
from line_assistant.calendar_event_types import AllDayEvent,parse_payload
from line_assistant.calendar_provider import creation_body,matches

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic owner and mock business services only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def proposal(items,*,update=False,batch=False):
    def call(body):
        c=json.loads(body['input'][0]['content']);events=[]
        view=c['calendar_batches'][-1] if update and batch else None
        for value in items:
            value=deepcopy(value);index=value.pop('index',0)
            old=view['events'][index] if view else c['calendar_drafts'][-1] if update else None
            events.append({'draft_ref':old['draft_ref'] if old else None,'draft_version':old['version'] if old else None,
                **dict.fromkeys(['title','date_text','time_text','end_date_text','end_time_text','duration_text']),
                'all_day':None,'source_refs':[c['current_source_ref']],**value})
        if batch:return response('prepare_calendar_events',{'batch_ref':view['batch_ref'] if view else None,'batch_version':view['version'] if view else None,'events':events})
        return response('prepare_calendar_event',events[0])
    return call

def hosted():
    api=VoiceHosted();api.usage_gates_enabled=False;return api

def event(title='นิทรรศการทดสอบ',day='วันพุธหน้า',**extra):return {'title':title,'date_text':day,'all_day':True,**extra}

def test_single_relative_date_review_confirm_cold_restore_and_duplicate_are_date_only():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้าทั้งวัน',proposal([event()]))
    assert r.status=='preview' and r.data['calendar_view']['all_day'] is True and not api.calendar.posts,r
    assert r.data['calendar_view']['start']=='2026-10-21' and r.data['calendar_view']['end']=='2026-10-22'
    assert '21 ต.ค. 2026' in r.message and 'ทั้งวัน' in r.message and '00:00' not in r.message and not r.message.startswith('ได้เลย')
    saved,confirmation=turn(api,'ยืนยัน')
    assert saved.status=='created' and saved.data['all_day'] is True and api.calendar.posts==1,saved
    wire=next(iter(api.calendar.events.values()));assert wire['start']=={'date':'2026-10-21'} and wire['end']=={'date':'2026-10-22'}
    assert wire['reminders']=={'useDefault':False,'overrides':[]} and not wire.get('attendees')
    calls=len(api.model_bodies)
    with TestClient(api.app()) as c:assert api.send(c,deepcopy(confirmation)).status_code==200
    assert api.calendar.posts==1 and len(api.model_bodies)==calls

def test_versioned_all_day_state_does_not_masquerade_as_legacy_timed_format():
    from line_assistant.calendar_workflow import restore_calendar
    from line_assistant.history_context import HistoryState
    api=hosted();turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้าทั้งวัน',proposal([event()]))
    value=api.snapshot()['history24']['current']['calendar_v1'];assert value['format']=='calendar-history-v2'
    wrong=deepcopy(value);wrong['format']='calendar-history-v1'
    with pytest.raises(ValueError,match='requires Calendar history v2'):restore_calendar(HistoryState(),wrong)

@pytest.mark.parametrize('literal,start,end',[('31 ตุลาคม 2026','2026-10-31','2026-11-01'),('31 ธันวาคม 2026','2026-12-31','2027-01-01'),('28 กุมภาพันธ์ 2027','2027-02-28','2027-03-01'),('29 กุมภาพันธ์ 2028','2028-02-29','2028-03-01'),('มะรืน','2026-10-18','2026-10-19')])
def test_date_only_month_year_leap_and_relative_boundaries(literal,start,end):
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบ '+literal+' ทั้งวัน',proposal([event(day=literal)]))
    assert r.status=='preview' and r.data['calendar_view']['start']==start and r.data['calendar_view']['end']==end,r
    got,_=turn(api,'ยืนยัน');assert got.status=='created' and api.calendar.posts==1

@pytest.mark.parametrize('literal',['29 กุมภาพันธ์ 2027','วันพุธหน้าหรือวันพฤหัสหน้า'])
def test_invalid_or_ambiguous_date_does_not_create(literal):
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบ '+literal+' ทั้งวัน',proposal([event(day=literal)]))
    assert r.status=='clarification' and not api.calendar.posts

def test_missing_date_asks_date_only_and_no_default_all_day_or_default_times():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบทั้งวัน',proposal([event(day=None)]))
    assert r.status=='clarification' and r.data['calendar_view']['missing_fields']==['date'] and not api.calendar.posts,r
    assert 'เวลาเริ่ม' not in r.message and 'เวลาสิ้นสุด' not in r.message and '00:00' not in r.message

@pytest.mark.parametrize('voice',[False,True])
def test_dated_without_time_offers_choice_and_all_day_answer_keeps_same_draft(voice):
    api=hosted();api.realization_enabled=voice
    r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า',proposal([event(all_day=None)]))
    view=r.data['calendar_view'];ref=view['draft_ref']
    assert r.status=='clarification' and not api.calendar.posts and 'confirm_command' not in r.data
    assert 'นิทรรศการทดสอบ เป็นนัดทั้งวัน หรือเริ่มกี่โมงครับ?' in r.message
    assert 'เวลาสิ้นสุด' not in r.message and 'ระยะเวลา' not in r.message and view['missing_fields']==['time','end']
    r,_=turn(api,'ทั้งวัน',proposal([{'all_day':True}],update=True));fresh=r.data['calendar_view']
    assert r.status=='preview' and fresh['draft_ref']==ref and fresh['all_day'] is True
    assert fresh['facts']['title']==view['facts']['title'] and fresh['facts']['date_text']==view['facts']['date_text']
    assert fresh['start']=='2026-10-21' and fresh['end']=='2026-10-22' and not api.calendar.posts
    assert 'เริ่มกี่โมง' not in r.message and 'เวลาสิ้นสุด' not in r.message
    saved,_=turn(api,'ยืนยัน');assert saved.status=='created' and api.calendar.posts==1

@pytest.mark.parametrize('voice',[False,True])
@pytest.mark.parametrize('answer,clocks',[('เวลา9:00',{'time_text':'9:00'}),('9:00-10:00',{'time_text':'9:00','end_time_text':'10:00'})])
def test_timing_choice_start_or_range_retains_date_title_and_only_asks_missing_end(voice,answer,clocks):
    api=hosted();api.realization_enabled=voice
    first,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า',proposal([event(all_day=None)]));view=first.data['calendar_view']
    r,_=turn(api,answer,proposal([clocks],update=True));fresh=r.data['calendar_view']
    assert fresh['draft_ref']==view['draft_ref'] and fresh['facts']['title']==view['facts']['title'] and fresh['facts']['date_text']==view['facts']['date_text']
    assert not fresh['all_day'] and fresh['start']=='2026-10-21T09:00:00+07:00' and not api.calendar.posts
    assert 'นัดนี้ทั้งวัน หรือเริ่มกี่โมง' not in r.message
    if 'end_time_text' not in clocks:
        assert r.status=='clarification' and fresh['missing_fields']==['end'] and 'เวลาสิ้นสุด' in r.message
        assert 'ยังขาด/ต้องตรวจ: เวลาเริ่ม' not in r.message
        r,_=turn(api,'ถึง10:00',proposal([{'end_time_text':'10:00'}],update=True))
    assert r.status=='preview' and r.data['calendar_view']['draft_ref']==view['draft_ref']
    saved,_=turn(api,'ยืนยัน');assert saved.status=='created' and api.calendar.posts==1
    wire=next(iter(api.calendar.events.values()));assert wire['start']=={'dateTime':'2026-10-21T09:00:00+07:00','timeZone':'Asia/Bangkok'}

@pytest.mark.parametrize('voice',[False,True])
@pytest.mark.parametrize('first_all_day',[False,True])
def test_mixed_batch_asks_choice_only_for_second_and_resolves_only_that_member(voice,first_all_day):
    api=hosted();api.realization_enabled=voice
    first=event(day='22 Oct') if first_all_day else event(day='22 Oct',all_day=None,time_text='9:00',end_time_text='10:00')
    r,_=turn(api,'เพิ่มนัด22 Oct นิทรรศการทดสอบ'+('ทั้งวัน' if first_all_day else '9:00-10:00')+' และ23 Oct คุยโครงการทดสอบ',
        proposal([first,event('คุยโครงการทดสอบ','23 Oct',all_day=None)],batch=True))
    before=r.data['calendar_batch_view']['events'];assert r.status=='clarification' and not api.calendar.posts
    assert 'คุยโครงการทดสอบ เป็นนัดทั้งวัน หรือเริ่มกี่โมงครับ?' in r.message and 'นิทรรศการทดสอบ เป็นนัดทั้งวัน' not in r.message
    answer='นัดที่สองเวลา9:00-10:00' if first_all_day else 'นัดที่สองทั้งวัน'
    edit={'index':1,'time_text':'9:00','end_time_text':'10:00'} if first_all_day else {'index':1,'all_day':True}
    r,_=turn(api,answer,proposal([edit],update=True,batch=True));after=r.data['calendar_batch_view']['events']
    assert r.status=='preview' and [v['draft_ref'] for v in after]==[v['draft_ref'] for v in before]
    assert (after[0]['start'],after[0]['end'],after[0]['facts'])==(before[0]['start'],before[0]['end'],before[0]['facts'])
    assert after[1]['facts']['title']==before[1]['facts']['title'] and after[1]['facts']['date_text']==before[1]['facts']['date_text']
    assert [v['all_day'] for v in after]==[first_all_day,not first_all_day] and not api.calendar.posts
    saved,_=turn(api,'ยืนยันชุดนัดนี้');assert saved.status=='created' and api.calendar.posts==2

@pytest.mark.parametrize('literal',['ถึง10:00','จนถึง10:00','ถึง๑๐:๐๐'])
def test_explicit_end_clock_misplaced_in_duration_keeps_literal_role_and_completes_same_draft(literal):
    api=hosted();first,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า',proposal([event(all_day=None)]));ref=first.data['calendar_view']['draft_ref']
    r,_=turn(api,'9:00',proposal([{'time_text':'9:00'}],update=True));assert r.data['calendar_view']['missing_fields']==['end']
    r,_=turn(api,literal,proposal([{'duration_text':literal}],update=True));view=r.data['calendar_view']
    assert r.status=='preview' and view['draft_ref']==ref and view['facts']['end_time_text']==literal and view['facts']['duration_text'] is None
    assert view['start']=='2026-10-21T09:00:00+07:00' and view['end']=='2026-10-21T10:00:00+07:00' and not api.calendar.posts
    saved,_=turn(api,'ยืนยัน');assert saved.status=='created' and api.calendar.posts==1

def test_model_cannot_invent_end_role_prefix_when_only_a_bare_clock_was_supplied():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า9:00',proposal([event(all_day=None,time_text='9:00')]))
    ref=r.data['calendar_view']['draft_ref']
    r,_=turn(api,'10:00',proposal([{'duration_text':'ถึง10:00'}],update=True))
    assert r.status=='clarification' and not api.calendar.posts
    draft=api.snapshot()['history24']['current']['calendar_v1']['drafts'][ref]
    assert draft['end'] is None and draft['facts']['end_time_text'] is None and draft['facts']['duration_text'] is None

def test_multiday_range_requires_explicit_split_or_single_day_clarification():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบทั้งวัน 22 Oct ถึง23 Oct',proposal([event(day='22 Oct',end_date_text='23 Oct')]))
    assert r.status=='clarification' and 'calendar_all_day_range_unsupported' in r.data['calendar_view']['issues'] and not api.calendar.posts,r
    got,_=turn(api,'เอาเฉพาะวัน22 Oct วันเดียว',proposal([{'date_text':'22 Oct'}],update=True))
    assert got.status=='preview' and got.data['calendar_view']['end']=='2026-10-23',got

@pytest.mark.parametrize('phrase,day',[('22 Oct ถึง23 Oct','22 Oct'),('22-23 Oct','23 Oct')])
def test_omitted_range_endpoint_cannot_silently_become_a_single_day(phrase,day):
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบทั้งวัน '+phrase,proposal([event(day=day)]))
    assert r.status=='clarification' and not api.calendar.posts,r
    r,_=turn(api,'เอาเฉพาะวัน22 Oct วันเดียว',proposal([{'date_text':'22 Oct'}],update=True))
    assert r.status=='preview' and r.data['calendar_view']['start']=='2026-10-22',r

def test_mixed_batch_keeps_modes_dates_and_delivered_confirmation_exact():
    api=hosted();r,_=turn(api,'เพิ่มนัด 22 Oct นิทรรศการทดสอบทั้งวัน และ23 Oct คุยโครงการทดสอบ 9:00-10:00',proposal([event(day='22 Oct'),event('คุยโครงการทดสอบ','23 Oct',all_day=False,time_text='9:00',end_time_text='10:00')],batch=True))
    assert r.status=='preview' and not api.calendar.posts,r
    views=r.data['calendar_batch_view']['events'];assert [v['all_day'] for v in views]==[True,False]
    assert views[0]['start']=='2026-10-22' and views[1]['start']=='2026-10-23T09:00:00+07:00'
    got,_=turn(api,'ยืนยันชุดนัดนี้');assert got.status=='created' and api.calendar.posts==2,got
    assert '2 รายการ' in got.message and 'ผลชุดนัด:' not in got.message and not got.message.startswith('ได้เลย')
    wires=list(api.calendar.events.values());assert wires[0]['start']=={'date':'2026-10-22'} and 'dateTime' in wires[1]['start']

def test_single_date_edit_then_rejected_old_nonce_allows_only_latest_unchanged_delivered_review():
    api=hosted();old,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้าทั้งวัน',proposal([event()]))
    fresh,_=turn(api,'เลื่อนเป็นวันพฤหัสหน้า',proposal([{'date_text':'วันพฤหัสหน้า'}],update=True))
    assert fresh.status=='preview' and fresh.data['calendar_view']['start']=='2026-10-22'
    denied,_=turn(api,old.data['confirm_command']);assert denied.status=='stale' and not api.calendar.posts
    saved,_=turn(api,'ยืนยัน');assert saved.status=='created' and api.calendar.posts==1 and saved.data['start']=='2026-10-22',saved

def test_rejected_new_edit_or_other_reply_cannot_reactivate_older_single_review():
    api=hosted();first,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้าทั้งวัน',proposal([event()]))
    invalid=response('prepare_calendar_event',{'draft_ref':'cal_'+'0'*20,'draft_version':1,'title':None,'date_text':None,'time_text':None,'end_date_text':None,'end_time_text':None,'duration_text':None,'all_day':None,'source_refs':['fake']})
    wrong,_=turn(api,'แก้นัดที่ไม่ทราบให้เป็นวันพฤหัสหน้า',invalid);assert wrong.status=='clarification'
    # The unchanged older draft is shown again for fresh consent, never confirmed.
    got,_=turn(api,'ยืนยัน');assert got.status=='preview' and 'ยืนยันอีกครั้ง' in got.message and not api.calendar.posts
    assert got.data['confirm_command']!=first.data['confirm_command'] and got.data['calendar_view']['start']==first.data['calendar_view']['start']

def test_model_spreading_a_second_member_edit_to_both_is_rejected_and_recoverable_with_old_history():
    from test_calendar_text_confirmation_v13 import retained
    from test_calendar_batch_factory_v13 import text,proposed
    api=hosted();retained(api)
    review,_=turn(api,text(),proposed)
    before=api.snapshot()['history24']['current']['calendar_v1']['drafts']
    rejected,_=turn(api,'แก้นัดที่สองสิ้นสุด18:00',proposal([
        {'index':0,'end_time_text':'18:00'},{'index':1,'end_time_text':'18:00'}],update=True,batch=True))
    assert rejected.status=='clarification' and not api.calendar.posts
    after=api.snapshot()['history24']['current']['calendar_v1']['drafts']
    assert {ref:d['end'] for ref,d in before.items()}=={ref:d['end'] for ref,d in after.items()}
    stale,_=turn(api,review.data['confirm_command']);assert stale.status=='stale' and not api.calendar.posts
    fresh,_=turn(api,'แก้นัดที่สองสิ้นสุด18:00',proposal([{'index':1,'end_time_text':'18:00'}],update=True,batch=True))
    assert fresh.status=='preview',fresh
    saved,_=turn(api,'ยืนยันชุดนัดนี้');assert saved.status=='created' and api.calendar.posts==2
    assert sorted(v['end']['dateTime'] for v in api.calendar.events.values())==['2026-10-22T17:00:00+07:00','2026-10-23T18:00:00+07:00']

@pytest.mark.parametrize('wrong_tool',[False,True])
def test_ordinal_edit_context_is_latest_delivered_set_and_wrong_old_id_revokes_latest_consent(wrong_tool):
    from test_calendar_text_confirmation_v13 import retained
    from test_calendar_batch_factory_v13 import text,proposed
    api=hosted();older,_,_=retained(api);latest,_=turn(api,text(),proposed)
    old_view=older.data['calendar_batch_view'];new_view=latest.data['calendar_batch_view']
    def wrong(body):
        c=json.loads(body['input'][0]['content'])
        assert [b['batch_ref'] for b in c['calendar_batches']]==[new_view['batch_ref']]
        assert {d['draft_ref'] for d in c['calendar_drafts']}=={d['draft_ref'] for d in new_view['events']}
        d=old_view['events'][1]
        if wrong_tool:return response('prepare_calendar_event',{'draft_ref':d['draft_ref'],'draft_version':d['version'],
            **dict.fromkeys(['title','date_text','time_text','end_date_text','duration_text','all_day']),
            'end_time_text':'18:00','source_refs':[c['current_source_ref']]})
        return response('prepare_calendar_events',{'batch_ref':old_view['batch_ref'],'batch_version':old_view['version'],
            'events':[{'draft_ref':d['draft_ref'],'draft_version':d['version'],**dict.fromkeys(['title','date_text','time_text','end_date_text','duration_text','all_day']),
                'end_time_text':'18:00','source_refs':[c['current_source_ref']]}]})
    r,_=turn(api,'แก้นัดที่สองสิ้นสุด18:00',wrong);assert r.status=='clarification' and not api.calendar.posts
    state=api.snapshot()['history24']['current'];batches=state['calendar_batches_v1']['batches']
    assert batches[new_view['batch_ref']]['review'] is None and batches[new_view['batch_ref']]['version']>new_view['version']
    assert batches[old_view['batch_ref']]['version']==old_view['version']
    rejected,_=turn(api,latest.data['confirm_command']);assert rejected.status=='stale' and not api.calendar.posts

def test_ordinal_edit_after_retained_reviews_changes_latest_set_only():
    from test_calendar_text_confirmation_v13 import retained
    from test_calendar_batch_factory_v13 import text,proposed
    api=hosted();older,_,_=retained(api);latest,_=turn(api,text(),proposed)
    r,_=turn(api,'แก้นัดที่สองสิ้นสุด18:00',proposal([{'index':1,'end_time_text':'18:00'}],update=True,batch=True))
    assert r.status=='preview' and r.data['calendar_batch_view']['batch_ref']==latest.data['calendar_batch_view']['batch_ref']
    state=api.snapshot()['history24']['current'];old=older.data['calendar_batch_view']
    assert all(state['calendar_v1']['drafts'][d['draft_ref']]['end']==d['end'] for d in old['events'])
    denied,_=turn(api,latest.data['confirm_command']);assert denied.status=='stale' and not api.calendar.posts
    saved,_=turn(api,'ยืนยันชุดนัดนี้');assert saved.status=='created' and api.calendar.posts==2

def test_explicit_other_set_reference_is_not_silently_redirected_to_latest():
    from test_calendar_text_confirmation_v13 import retained
    from test_calendar_batch_factory_v13 import text,proposed
    api=hosted();older,_,_=retained(api);latest,_=turn(api,text(),proposed);old_ref=older.data['calendar_batch_view']['batch_ref']
    def explicit(body):
        c=json.loads(body['input'][0]['content']);b=next(b for b in c['calendar_batches'] if b['batch_ref']==old_ref);d=b['events'][1]
        assert any(b['batch_ref']==latest.data['calendar_batch_view']['batch_ref'] for b in c['calendar_batches'])
        return response('prepare_calendar_events',{'batch_ref':old_ref,'batch_version':b['version'],
            'events':[{'draft_ref':d['draft_ref'],'draft_version':d['version'],**dict.fromkeys(['title','date_text','time_text','end_date_text','duration_text','all_day']),
                'end_time_text':'18:00','source_refs':[c['current_source_ref']]}]})
    r,_=turn(api,'แก้ชุด '+old_ref+' นัดที่สองสิ้นสุด18:00',explicit)
    assert r.status in {'preview','clarification'} and not api.calendar.posts,r
    if r.status=='preview':assert r.data['calendar_batch_view']['batch_ref']==old_ref
    state=api.snapshot()['history24']['current'];new=latest.data['calendar_batch_view']
    assert state['calendar_batches_v1']['batches'][new['batch_ref']]['version']==new['version']

def test_switching_modes_requires_new_review_does_not_inherit_clock_and_revokes_old_nonce():
    api=hosted();old,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า9:00-10:00',proposal([event(all_day=False,time_text='9:00',end_time_text='10:00')]))
    r,_=turn(api,'เปลี่ยนนัดเป็นทั้งวัน',proposal([{'all_day':True}],update=True));assert r.status=='preview' and r.data['calendar_view']['all_day'],r
    assert all(r.data['calendar_view']['facts'][k] is None for k in ['time_text','end_time_text','duration_text'])
    stale,_=turn(api,old.data['confirm_command']);assert stale.status=='stale' and not api.calendar.posts
    r,_=turn(api,'เปลี่ยนเป็นเวลา13:00-14:00',proposal([{'all_day':False,'time_text':'13:00','end_time_text':'14:00'}],update=True))
    assert r.status=='preview' and r.data['calendar_view']['all_day'] is False and r.data['calendar_view']['start']=='2026-10-21T13:00:00+07:00',r
    got,_=turn(api,'ยืนยัน');assert got.status=='created' and 'dateTime' in next(iter(api.calendar.events.values()))['start']

def test_explicit_all_day_with_clock_conflict_asks_then_owner_resolves_without_write():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้าทั้งวัน9:00-10:00',proposal([event(time_text='9:00',end_time_text='10:00')]))
    assert r.status=='clarification' and not api.calendar.posts,r
    r,_=turn(api,'เอาทั้งวันอย่างเดียว',proposal([{'all_day':True}],update=True))
    assert r.status=='preview' and '00:00' not in r.message,r

def test_omitted_clock_proposal_cannot_hide_conflicting_owner_all_day_and_times():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้าทั้งวัน9:00-10:00',proposal([event()]))
    assert r.status=='clarification' and 'calendar_event_type_conflict' in r.data['calendar_view']['issues'] and not api.calendar.posts,r
    r,_=turn(api,'เอาทั้งวันอย่างเดียว',proposal([{'all_day':True}],update=True))
    assert r.status=='preview' and not api.calendar.posts,r

def test_explicit_same_relative_word_after_midnight_reanchors_but_unmentioned_date_stays():
    from datetime import datetime
    api=hosted();api.now=datetime.fromisoformat('2026-10-16T16:55:00+00:00')
    old,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบพรุ่งนี้ทั้งวัน',proposal([event(day='พรุ่งนี้')]))
    assert old.data['calendar_view']['start']=='2026-10-17'
    api.now+=timedelta(minutes=10)
    r,_=turn(api,'เลื่อนเป็นพรุ่งนี้',proposal([{'date_text':'พรุ่งนี้'}],update=True))
    assert r.status=='preview' and r.data['calendar_view']['start']=='2026-10-18',r

def test_expired_refresh_recovers_only_literal_explicit_shared_clocks_when_model_omits_them():
    from test_calendar_text_confirmation_v13 import retained
    api=hosted();retained(api);api.now+=timedelta(minutes=31)
    def omitted(body):
        c=json.loads(body['input'][0]['content']);ref=next(r['source_ref'] for r in c['history'] if r['role']=='user' and '8:00-17:00' in r['text'] and 'กิจกรรมทดสอบA' in r['text'])
        rows=[{**dict.fromkeys(['draft_ref','draft_version','time_text','end_date_text','end_time_text','duration_text','all_day']),'title':title,'date_text':day,'source_refs':[ref]} for title,day in [('กิจกรรมทดสอบA','22 Oct'),('กิจกรรมทดสอบB','23 Oct')]]
        return response('prepare_calendar_events',{'batch_ref':None,'batch_version':None,'events':rows})
    r,_=turn(api,'ช่วยตรวจร่างชุดนัดเดิมสองนัดอีกครั้ง',omitted)
    assert r.status=='preview' and all(e['start'].endswith('T08:00:00+07:00') and e['end'].endswith('T17:00:00+07:00') for e in r.data['calendar_batch_view']['events']) and not api.calendar.posts,r

def test_forged_mode_without_owner_evidence_cannot_create_all_day_event():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า',proposal([event()]))
    assert r.status=='clarification' and not api.calendar.posts and 'confirm_command' not in r.data

def test_literal_all_day_words_in_title_do_not_change_timed_mode():
    api=hosted();r,_=turn(api,'เพิ่มนัดชื่อ "อ่านทั้งวัน" วันพุธหน้า9:00-10:00',proposal([event(title='อ่านทั้งวัน',all_day=False,time_text='9:00',end_time_text='10:00')]))
    assert r.status=='preview' and r.data['calendar_view']['all_day'] is False and not api.calendar.posts,r

def test_cancelled_all_day_draft_has_no_provider_write():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้าทั้งวัน',proposal([event()]))
    def cancel(body):
        d=json.loads(body['input'][0]['content'])['calendar_drafts'][0]
        return response('control_calendar_draft',{'draft_ref':d['draft_ref'],'draft_version':d['version'],'operation':'cancel'})
    got,_=turn(api,'ยกเลิกร่างนัด',cancel);assert got.status=='cancelled' and not api.calendar.posts

def mixed(api):
    return turn(api,'เพิ่มนัด 22 Oct นิทรรศการทดสอบทั้งวัน และ23 Oct คุยโครงการทดสอบ 9:00-10:00',proposal([event(day='22 Oct'),event('คุยโครงการทดสอบ','23 Oct',all_day=False,time_text='9:00',end_time_text='10:00')],batch=True))[0]

def test_mixed_batch_partial_failure_preserves_created_date_event_and_never_repeats_it():
    api=hosted();mixed(api);original=api.calendar.request
    def failure(request):
        if request.method=='POST' and api.calendar.posts==1:
            api.calendar.posts+=1;return httpx.Response(403,json={})
        return original(request)
    api.calendar.request=failure;got,confirmation=turn(api,'ยืนยันชุดนัดนี้')
    assert got.status=='partial' and len(got.data['created_refs'])==1 and len(got.data['pending_refs'])==1 and api.calendar.posts==2,got
    assert next(iter(api.calendar.events.values()))['start']=={'date':'2026-10-22'}
    with TestClient(api.app()) as c:assert api.send(c,deepcopy(confirmation)).status_code==200
    assert api.calendar.posts==2

def test_mixed_batch_type_edit_retains_other_member_and_rejects_delivered_old_confirmation():
    api=hosted();old=mixed(api)
    r,_=turn(api,'แก้นัดที่สองให้เป็นทั้งวัน',proposal([{'index':1,'all_day':True}],update=True,batch=True))
    assert r.status=='preview' and [e['all_day'] for e in r.data['calendar_batch_view']['events']]==[True,True],r
    denied,_=turn(api,old.data['confirm_command']);assert denied.status=='stale' and not api.calendar.posts
    got,_=turn(api,'ยืนยันชุดนัดนี้');assert got.status=='created' and api.calendar.posts==2
    assert all('date' in v['start'] for v in api.calendar.events.values())

def test_all_day_and_timed_overlap_is_warned_using_local_day_without_changing_date_payload():
    api=hosted();r,_=turn(api,'เพิ่มนัด 22 Oct นิทรรศการทดสอบทั้งวัน และ22 Oct คุยโครงการทดสอบ 9:00-10:00',proposal([event(day='22 Oct'),event('คุยโครงการทดสอบ','22 Oct',all_day=False,time_text='9:00',end_time_text='10:00')],batch=True))
    assert r.status=='preview' and 'เวลาในชุดทับกัน: 1 กับ 2' in r.message and not api.calendar.posts,r

def test_explicit_shared_all_day_applies_to_both_days_without_cross_binding_times():
    api=hosted();r,_=turn(api,'เพิ่มนัด22 Oct นิทรรศการทดสอบ และ23 Oct อ่านหนังสือทดสอบ ทั้งวันทั้งสองนัด',proposal([event(day='22 Oct'),event('อ่านหนังสือทดสอบ','23 Oct')],batch=True))
    assert r.status=='preview' and [e['start'] for e in r.data['calendar_batch_view']['events']]==['2026-10-22','2026-10-23'],r

def test_all_day_mode_does_not_leak_to_a_timed_member_when_model_omits_mode():
    api=hosted();r,_=turn(api,'เพิ่มนัด 22 Oct นิทรรศการทดสอบทั้งวัน และ23 Oct คุยโครงการทดสอบ 9:00-10:00',proposal([event(day='22 Oct',all_day=None),event('คุยโครงการทดสอบ','23 Oct',all_day=None,time_text='9:00',end_time_text='10:00')],batch=True))
    assert r.status=='preview' and [e['all_day'] for e in r.data['calendar_batch_view']['events']]==[True,False],r

def test_no_all_day_assumption_without_owner_day_mode_or_clock():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า',proposal([event(all_day=None)]))
    assert r.status=='clarification' and r.data['calendar_view']['all_day'] is False and {'time','end'}<=set(r.data['calendar_view']['missing_fields']),r

def test_literal_clock_range_misplaced_in_duration_slot_is_certified_as_clocks_not_minutes():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า9:00-10:00',proposal([event(all_day=False,duration_text='9:00-10:00')]))
    assert r.status=='preview' and r.data['calendar_view']['start']=='2026-10-21T09:00:00+07:00' and r.data['calendar_view']['end']=='2026-10-21T10:00:00+07:00',r
    assert r.data['calendar_view']['facts']['duration_text'] is None and not api.calendar.posts

def test_narrow_unsupported_reminder_reply_has_no_other_feature_list_or_positive_ack():
    api=hosted();r,_=turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้า9:00-10:00 เตือนก่อน30นาที',proposal([event(all_day=False,time_text='9:00',end_time_text='10:00')]))
    assert r.status=='clarification' and 'ตั้งเตือน' in r.message and 'ทั้งวัน' not in r.message and 'นัดซ้ำ' not in r.message and not r.message.startswith('ได้เลย'),r

def test_unsupported_options_are_classified_after_masking_literal_title():
    api=hosted();r,_=turn(api,'เพิ่มนัดชื่อ "อ่านทุกวัน" วันพุธหน้า9:00-10:00 เตือนก่อน30นาที',proposal([event(title='อ่านทุกวัน',all_day=False,time_text='9:00',end_time_text='10:00')]))
    assert r.status=='clarification' and r.data['tool_result']['unsupported_features']==['reminders']
    assert 'ตั้งเตือน' in r.message and 'นัดซ้ำ' not in r.message and not api.calendar.posts

def test_lost_all_day_ack_reconciles_positive_receipt_without_second_post():
    api=hosted();turn(api,'เพิ่มนัดนิทรรศการทดสอบวันพุธหน้าทั้งวัน',proposal([event()]));api.calendar.fail_after_create=True
    with TestClient(api.app()) as c:assert api.send(c,api.event('ยืนยัน')).status_code==503
    api.calendar.fail_after_create=False;got,_=turn(api,'ตรวจผลนัด')
    assert got.status=='created' and api.calendar.posts==1 and got.data['all_day'] is True,got

def test_reconciliation_rejects_midnight_timed_equivalent_and_wrong_exclusive_end():
    args=AllDayEvent(title='นิทรรศการทดสอบ',start='2026-12-31',end='2027-01-01');wire=creation_body(args,'fake-id','fake-calendar');wire['etag']='fake-etag'
    assert matches(wire,args,'fake-id','fake-calendar')
    for field,value in [('end',{'date':'2026-12-31'}),('start',{'dateTime':'2026-12-31T00:00:00+07:00'})]:
        bad=deepcopy(wire);bad[field]=value;assert not matches(bad,args,'fake-id','fake-calendar')
    with pytest.raises(ValueError):parse_payload({'title':'นิทรรศการทดสอบ','all_day':True,'start':'2026-12-31T00:00:00+07:00','end':'2027-01-01'})

@pytest.mark.parametrize('outcome',['clarification','partial','uncertain','stale'])
def test_generic_positive_acknowledgement_is_rejected_for_non_success(outcome):
    from line_assistant.response_realization import body_for,parse
    from line_assistant.state import Result
    class Host:
        _current={};_active_event_id='fake'
        class state:history=[];receipts={}
    body=body_for(Host(),Result(outcome,'ยังไม่ได้สร้างนัด',{}));value=generated(body)
    value['output'][0]['content'][0]['text']=value['output'][0]['content'][0]['text'].replace('"opening": ""','"opening": "ได้เลยครับ"')
    with pytest.raises(ValueError):parse(value,body)
