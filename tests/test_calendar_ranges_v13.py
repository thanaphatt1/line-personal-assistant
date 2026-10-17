"""Literal clock ranges versus genuine explicit foreign offsets."""
import pytest
from line_assistant.calendar_dates import bangkok_zone
from test_calendar_mvp import Harness

@pytest.mark.parametrize('range_text',['8:00-17:00','08:00-17:00','8:00 -17:00','8:00- 17:00','8:00 – 17:00','8:00—17:00','8:00 ถึง 17:00'])
@pytest.mark.parametrize('zone',['',' เวลาไทย',' Asia/Bangkok',' UTC+7',' +07:00'])
def test_ascending_ranges_are_not_offsets(range_text,zone):bangkok_zone('เวลา '+range_text+' ทั้งสองวัน'+zone)

@pytest.mark.parametrize('text',['2026-10-22T08:00-05:00','2026-10-22T08:00-09:00','8:00-17:00 UTC','8:00-17:00 JST','8:00-17:00 +09:00','8:00+09:00','08:00-05:00','8:00-25:00','8:00-17:00 เวลาโตเกียว'])
def test_range_fix_does_not_discard_foreign_or_invalid_offsets(text):
    with pytest.raises(ValueError,match='calendar_timezone_unsupported'):bangkok_zone(text)

def test_exact_owner_example_followup_keeps_certified_literals():
    h=Harness();h.prepare('เพิ่มปฏิทิน 22 Oct Dashboard Design with Power BI และ และ 23 Oct Business Analytics with Generative AI',title='Dashboard Design with Power BI',date_text='22 Oct')
    r=h.prepare('เวลา 8:00-17:00 ทั้งสองวัน',time_text='8:00',end_time_text='17:00')
    assert r.status=='clarification' and r.data['tool_result'].get('error')!='calendar_timezone_unsupported'
    values=list(h.state.calendar_drafts.values())
    assert len(values)==2 and values[0]['start']=='2026-10-22T08:00:00+07:00'
    assert values[0]['end']=='2026-10-22T17:00:00+07:00' and h.provider.posts==0

@pytest.mark.parametrize('text',['เวลา ๘:๐๐-๑๗:๐๐','เวลา 8:00-17:00 UTC+๗','เวลา ๘:๐๐-๑๗:๐๐ +๐๗:๐๐'])
def test_thai_digit_ranges_and_bangkok_offsets_use_the_same_guard(text):bangkok_zone(text)

@pytest.mark.parametrize('text',['เวลา ๘:๐๐+๐๙:๐๐','2026-10-22T๐๘:๐๐-๐๕:๐๐'])
def test_thai_digits_do_not_hide_a_foreign_offset(text):
    with pytest.raises(ValueError):bangkok_zone(text)


@pytest.mark.parametrize('text',['ประมาณ 45 นาทีพอดี','45 นาทีพอดีหรือ60นาที','45 นาทีพอดี30นาที','45 นาทีพอดีครึ่ง'])
def test_exact_duration_suffix_does_not_accept_ambiguous_or_extra_numeric_content(text):
    from line_assistant.calendar_dates import duration
    with pytest.raises(ValueError):duration(text)
