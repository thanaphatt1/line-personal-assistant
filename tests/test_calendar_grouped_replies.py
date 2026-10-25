"""Calendar agenda headings and numbering remain factual in every reply path."""
from datetime import datetime,timedelta

import pytest

from test_calendar_mvp import Harness
from test_calendar_read_all import AllCalendarsHosted,PERSONAL
from test_calendar_text_confirmation_v13 import turn
from test_read_queries import event,plan
from test_response_realization import VoiceHosted,generated


def timed(identifier,day,clock,end):
    return event(identifier,f'{day}T{clock}:00+07:00',f'{day}T{end}:00+07:00')


def test_weekly_agenda_groups_days_sorts_times_and_restarts_numbering():
    h=Harness()
    h.provider.events={row['id']:row for row in [
        timed('late','2026-10-19','15:00','16:00'),timed('tuesday','2026-10-20','10:00','11:00'),
        timed('early','2026-10-19','09:00','10:00'),event('day','2026-10-19','2026-10-20',all_day=True)]}
    result=h.send('สัปดาห์หน้ามีนัดอะไรบ้าง',plan('calendar','2026-10-19','2026-10-25'))
    assert result.data['tool_result']['count']==4
    assert '\n\nวันจันทร์ที่ 19 ต.ค. 2026:\n1. ทั้งวัน — นัดจำลอง day\n2. 09:00–10:00 — นัดจำลอง early\n3. 15:00–16:00 — นัดจำลอง late' in result.message
    assert '\n\nวันอังคารที่ 20 ต.ค. 2026:\n1. 10:00–11:00 — นัดจำลอง tuesday' in result.message
    assert 'วันพุธที่' not in result.message
    assert not h.provider.posts and not h.sheets.write_log and not h.state.actions


def test_bangkok_day_and_clock_are_used_for_utc_events():
    h=Harness();h.provider.events={'utc':event('utc','2026-10-16T18:30:00+00:00','2026-10-16T19:30:00+00:00')}
    result=h.send('พรุ่งนี้มีนัดอะไร',plan('calendar','2026-10-17'))
    assert 'วันเสาร์ที่ 17 ต.ค. 2026:\n1. 01:30–02:30 — นัดจำลอง utc' in result.message
    assert 'วันศุกร์ที่' not in result.message


def test_all_day_carry_in_is_grouped_in_requested_period_with_exclusive_end_retained():
    h=Harness();h.provider.events={'multi':event('multi','2026-10-17','2026-10-21',all_day=True)}
    result=h.send('สัปดาห์หน้ามีนัดอะไร',plan('calendar','2026-10-19','2026-10-25'))
    assert result.data['tool_result']['count']==1
    assert 'วันจันทร์ที่ 19 ต.ค. 2026:\n1. ทั้งวัน (17 ต.ค. 2026 ถึง 20 ต.ค. 2026) — นัดจำลอง multi' in result.message
    assert result.message.count('นัดจำลอง multi')==1
    assert 'วันเสาร์ที่' not in result.message and '21 ต.ค.' not in result.message


@pytest.mark.parametrize('start,end,expected',[
    ('2026-10-19T23:00:00+07:00','2026-10-20T01:00:00+07:00','23:00 ถึง 20 ต.ค. 2026 01:00'),
    ('2026-10-18T23:00:00+07:00','2026-10-19T01:00:00+07:00','เริ่ม 18 ต.ค. 2026 23:00 ถึง 19 ต.ค. 2026 01:00'),
    ('2026-10-19T23:00:00+07:00','2026-10-20T00:00:00+07:00','23:00 ถึง 20 ต.ค. 2026 00:00'),
])
def test_overnight_appointment_preserves_actual_boundaries_and_is_counted_once(start,end,expected):
    h=Harness();h.provider.events={'night':event('night',start,end)}
    result=h.send('ดูนัด',plan('calendar','2026-10-19','2026-10-20'))
    assert 'วันจันทร์ที่ 19 ต.ค. 2026:\n1. '+expected+' — นัดจำลอง night' in result.message
    assert result.data['tool_result']['count']==1 and result.message.count('นัดจำลอง night')==1
    assert 'วันอังคารที่' not in result.message


def test_empty_calendar_keeps_zero_count_without_inventing_day_groups():
    h=Harness();result=h.send('ดูนัด',plan('calendar','2026-10-19','2026-10-25'))
    assert result.data['tool_result']['complete'] and result.data['tool_result']['count']==0
    assert result.data['tool_result']['count']==0 and 'ไม่มีนัด' in result.message and not any(key.startswith('row') for key in result.data['query_facts'])
    assert '\n' not in result.message


def test_grouping_retains_twenty_appointment_display_limit_and_full_count(monkeypatch):
    h=Harness();start=datetime.fromisoformat('2026-10-19T09:00:00+07:00')
    rows=[]
    for i in range(25):
        rows.append({'title':f'นัดจำลอง item-{i:02}','start':(start+timedelta(minutes=i)).isoformat(),
            'end':(start+timedelta(minutes=i+1)).isoformat(),'all_day':False})
    monkeypatch.setattr(h.app.calendar_flow.adapter,'saved_events',lambda *args:rows)
    result=h.send('ดูนัด',plan('calendar','2026-10-19'))
    assert result.data['tool_result']['count']==25 and result.data['tool_result']['shown']==20
    assert '20. 09:19–09:20 — นัดจำลอง item-19' in result.message
    assert 'นัดจำลอง item-20' not in result.message and '20 รายการแรก' in result.message
    assert result.message.count('วันจันทร์ที่')==1


@pytest.mark.parametrize('realization_failure',[False,True])
def test_signed_composed_and_fallback_replies_can_use_the_same_day_groups(realization_failure):
    api=VoiceHosted()
    api.calendar.events={'mon':timed('mon','2026-10-19','09:00','10:00'),'tue':timed('tue','2026-10-20','15:00','16:00')}
    if realization_failure:
        def unavailable(body):raise TimeoutError('Synthetic reply generation failure')
        api.voice=unavailable
    else:
        api.voice=lambda body:generated(body,{'blocks':[
            {'kind':'text','template':'{{f1}} มี {{f2}}ครับ'},
            {'kind':'rows','record_refs':['r1'],'heading':'\nวันจันทร์ที่ {{c1}}:','template':'{{index}}. {{c2}} — {{c3}}'},
            {'kind':'rows','record_refs':['r2'],'heading':'\nวันอังคารที่ {{c1}}:','template':'{{index}}. {{c2}} — {{c3}}'}]})
    result,_=turn(api,'สัปดาห์หน้ามีนัดอะไรบ้าง',plan('calendar','2026-10-19','2026-10-25'))
    assert result.data['response_realization']['status']==('fallback' if realization_failure else 'llm')
    assert 'วันจันทร์ที่ 19 ต.ค. 2026:\n1. 09:00–10:00 — นัดจำลอง mon' in result.message
    assert 'วันอังคารที่ 20 ต.ค. 2026:\n1. 15:00–16:00 — นัดจำลอง tue' in result.message
    assert result.message.count('วันจันทร์ที่')==result.message.count('วันอังคารที่')==1
    if realization_failure:
        assert all(row in result.message for key,row in result.data['query_facts'].items() if key.startswith('row'))
    assert not api.calendar.posts and not api.google.sheet_writes


def test_appointments_from_different_calendars_share_one_day_heading():
    api=AllCalendarsHosted();api.read_all=False;api.calendar_read_ids=[PERSONAL]
    api.calendar.events={'main':timed('main','2026-10-19','15:00','16:00')}
    api.event_pages[PERSONAL]['']['items']=[timed('shared','2026-10-19','09:00','10:00')]
    api.voice=lambda body:generated(body,{'blocks':[
        {'kind':'text','template':'{{f1}} มี {{f2}}ครับ'},
        {'kind':'rows','record_refs':['r1','r2'],'heading':'\nวันจันทร์ที่ {{c1}}:','template':'{{index}}. {{c2}} — {{c3}}'}]})
    result,_=turn(api,'ดูนัดสัปดาห์หน้า',plan('calendar','2026-10-19','2026-10-25'))
    assert result.data['tool_result']['count']==2 and result.message.count('วันจันทร์ที่')==1
    assert '1. 09:00–10:00 — นัดจำลอง shared\n2. 15:00–16:00 — นัดจำลอง main' in result.message


def test_day_groups_sort_across_year_boundary():
    h=Harness();h.provider.events={'new':timed('new','2027-01-01','09:00','10:00'),'old':timed('old','2026-12-31','09:00','10:00')}
    result=h.send('ดูนัดช่วงปีใหม่',plan('calendar','2026-12-31','2027-01-01'))
    assert result.message.index('วันพฤหัสบดีที่ 31 ธ.ค. 2026:')<result.message.index('วันศุกร์ที่ 1 ม.ค. 2027:')
