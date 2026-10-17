"""Synthetic stored-data queries. Every external integration is a fake."""
from copy import deepcopy
from datetime import date, datetime, timedelta

import httpx
import pytest

from line_assistant.calendar_provider import CalendarCreationApi
from line_assistant.contracts import Transaction
from line_assistant.read_queries import period
from line_assistant.sheets_api import SheetsApi, SheetTarget
from line_assistant.sheet_dates import date_to_serial
from line_assistant.transport import RestClient, NotReady
from test_calendar_mvp import Harness, NOW, TARGET
from test_history_first import response


def seed(h):
    h.sheets.rows = [('กาแฟ', 'Food', ''), ('รถเมล์', 'Transportation', ''), ('ค่ากระดาษ', 'Work/Office', '')]
    h.sheets.entries = [Transaction(item=item, amount=amount, kind='expense', date=day) for item, amount, day in (
        ('กาแฟ', '0.10', '2026-10-16'), ('กาแฟ', '0.20', '2026-10-16'),
        ('รถเมล์', '25.00', '2026-10-15'), ('ค่ากระดาษ', '120.00', '2026-10-01'),
        ('กาแฟ', '90.00', '2026-09-30'), ('กาแฟ', '40.00', '2026-11-01'))]


@pytest.mark.parametrize('text,start,end', [
    ('สรุปเดือนนี้', '2026-10-01', '2026-10-31'),
    ('ดูสัปดาห์นี้', '2026-10-12', '2026-10-18'),
    ('ดูสัปดาห์หน้า', '2026-10-19', '2026-10-25'),
    ('ดูสัปดาห์ก่อน', '2026-10-05', '2026-10-11'),
    ('เดือนก่อน', '2026-09-01', '2026-09-30'),
    ('เดือนหน้า', '2026-11-01', '2026-11-30'),
    ('2026-10-01 ถึง 2026-10-19', '2026-10-01', '2026-10-19'),
    ('1/10/2569 ถึง 19/10/2569', '2026-10-01', '2026-10-19'),
    ('19–25 ตุลาคม 2569', '2026-10-19', '2026-10-25'),
    ('วันที่ 19 ตุลาคม 2569', '2026-10-19', '2026-10-19'),
    ('ตุลาคม 2569', '2026-10-01', '2026-10-31'),
    ('วันจันทร์หน้า', '2026-10-19', '2026-10-19'),
    ('วันนี้', '2026-10-16', '2026-10-16'),
])
def test_date_resolution(text, start, end):
    dates, _ = period(text, NOW)
    assert tuple(d.isoformat() for d in dates) == (start, end)


@pytest.mark.parametrize('text', ['วันนี้หรือพรุ่งนี้', '2026-10-30 ถึง 2026-10-01', '31/2/2569', 'วันนี้ UTC+08:00', '2026-01-01 ถึง 2028-01-01'])
def test_unclear_or_unbounded_period_never_defaults(text):
    with pytest.raises(ValueError):period(text, NOW)


def test_bangkok_midnight_leap_month_and_year_rollover():
    assert period('วันนี้', datetime.fromisoformat('2026-10-15T18:00:00+00:00'))[0] == (date(2026, 10, 16),) * 2
    assert period('เดือนก่อน', datetime.fromisoformat('2024-03-01T00:00:00+07:00'))[0] == (date(2024, 2, 1), date(2024, 2, 29))
    assert period('เดือนหน้า', datetime.fromisoformat('2026-12-31T00:00:00+07:00'))[0] == (date(2027, 1, 1), date(2027, 1, 31))


def test_expense_totals_filters_and_delivered_followups_do_not_write():
    h = Harness();seed(h);before = deepcopy(h.sheets.entries)
    result = h.send('สรุปรายจ่ายเดือนนี้')
    assert result.status == 'summary' and '145.30' in result.message
    assert result.data['tool_result']['count'] == 4
    result = h.send('เฉพาะหมวดอาหาร')
    assert result.status == 'summary' and '0.30' in result.message and 'Food' in result.message
    result = h.send('แยกรายการให้หน่อย')
    assert len(result.data['tool_result']['entries']) == 2 and 'กาแฟ' in result.message
    result = h.send('แล้วเดือนก่อนล่ะ')
    assert '90.00' in result.message and 'Food' in result.message
    result = h.send('ดูรายจ่ายเดือนนี้ทั้งหมด')
    assert '145.30' in result.message and not result.data['read_query']['filter']
    assert h.sheets.entries == before and not h.sheets.write_log and not h.state.actions and not h.state.drafts
    assert not h.model.requests and not h.provider.calls


def test_item_filter_empty_data_and_unknown_category_are_honest():
    h = Harness();seed(h)
    result = h.send('ดูรายการรายจ่ายวันนี้ กาแฟ')
    assert '0.30' in result.message and len(result.data['tool_result']['entries']) == 2
    result = h.send('สรุปรายจ่ายพรุ่งนี้')
    assert '0.00' in result.message and result.data['tool_result']['complete'] is True
    assert 'รายรับ' not in result.message
    result = h.send('สรุปรายจ่ายวันนี้หมวดไม่รู้จัก')
    assert result.status == 'clarification' and '0.00' not in result.message
    result = h.send('สรุปรายรับวันนี้')
    assert result.status == 'income_unconfigured' and '0.00' not in result.message


def test_missing_period_then_date_only_answer_and_no_stale_scope():
    h = Harness();seed(h)
    assert h.send('สรุปรายจ่าย').status == 'clarification'
    assert '145.30' in h.send('เดือนนี้').message
    h.send('คุยเรื่องอื่น', response(text='เรื่องอื่นครับ'))
    result = h.send('แล้วเมื่อวานล่ะ', response(text='ต้องการดูข้อมูลเรื่องไหนครับ'))
    assert 'read_query' not in result.data


def test_subcategory_ambiguity_and_unknown_stored_mapping_fail_closed():
    h = Harness();seed(h)
    h.sheets.rows = [('กาแฟ', 'Food/Drink', 'เช้า'), ('อาหาร', 'Food/Meal', 'เย็น'), ('รถเมล์', 'Transportation', 'เช้า')]
    assert h.send('สรุปรายจ่ายวันนี้หมวดอาหาร').status == 'clarification'
    assert h.send('ดูรายจ่ายเดือนนี้หมวดย่อยเช้า').status == 'unavailable'
    h.sheets.rows = [('กาแฟ', 'Food', 'เครื่องดื่ม'), ('รถเมล์', 'Transportation', 'รถ')]
    assert '0.30' in h.send('ดูรายจ่ายวันนี้หมวดย่อยเครื่องดื่ม').message


def event(identifier, start, end, *, all_day=False, **extra):
    return {'id': identifier, 'summary': 'นัดจำลอง ' + identifier,
            'start': {'date' if all_day else 'dateTime': start},
            'end': {'date' if all_day else 'dateTime': end}, **extra}


def test_calendar_includes_transparent_all_day_overlap_and_exclusive_end():
    h = Harness()
    h.provider.events = {e['id']: e for e in [
        event('day', '2026-10-17', '2026-10-18', all_day=True),
        event('multi', '2026-10-16', '2026-10-19', all_day=True),
        event('free', '2026-10-17T09:00:00+07:00', '2026-10-17T10:00:00+07:00', transparency='transparent'),
        event('cancelled', '2026-10-17', '2026-10-18', all_day=True, status='cancelled'),
        event('outside', '2026-10-18', '2026-10-19', all_day=True),
    ]}
    before = deepcopy(h.provider.events)
    result = h.send('พรุ่งนี้มีนัดอะไรบ้าง')
    assert result.status == 'answer' and result.data['tool_result']['count'] == 3
    assert '2026-10-17 ทั้งวัน — นัดจำลอง day' in result.message
    assert '2026-10-16 ถึง 2026-10-18 ทั้งวัน' in result.message
    assert '09:00' in result.message and 'นัดจำลอง free' in result.message
    assert 'นัดจำลอง cancelled' not in result.message and 'นัดจำลอง outside' not in result.message
    assert h.provider.events == before and h.provider.posts == 0 and not h.state.actions and not h.state.calendar_drafts
    assert all(method == 'GET' for method, _ in h.provider.calls)
    result = h.send('แล้วสัปดาห์หน้าล่ะ')
    assert result.data['read_query']['start'] == '2026-10-19' and result.data['tool_result']['count'] == 0


def test_upcoming_window_uses_current_time_and_calendar_over_week_clarifies():
    h = Harness()
    h.provider.events = {e['id']: e for e in [
        event('old', '2026-10-16T08:00:00+07:00', '2026-10-16T09:00:00+07:00'),
        event('current', '2026-10-16T14:00:00+07:00', '2026-10-16T16:00:00+07:00'),
    ]}
    result = h.send('ดูนัดถัดไป')
    assert result.data['tool_result']['count'] == 1 and 'นัดจำลอง current' in result.message
    assert h.send('ดูนัดเดือนนี้').status == 'clarification'


@pytest.mark.parametrize('text', ['พรุ่งนี้ powerbi', 'พรุ่งนี้ Workshop'])
@pytest.mark.parametrize('previous', [None, 'สรุปรายรับวันนี้', 'ดูนัดสัปดาห์หน้า', 'สรุปรายจ่ายวันนี้'])
def test_terse_dated_title_asks_intent_without_generic_evidence_failure(text, previous):
    h = Harness()
    if previous:h.send(previous)
    result = h.send(text)
    assert result.status == 'clarification' and 'เพิ่มนัด' in result.message and 'ดูนัดที่บันทึก' in result.message
    assert '2026-10-17' in result.message and not h.model.requests and h.provider.posts == 0
    assert not h.state.calendar_drafts and not h.state.actions


def test_disabled_calendar_and_read_failures_do_not_report_empty(monkeypatch):
    h = Harness(enabled=False)
    assert h.send('ดูนัดพรุ่งนี้').status == 'unavailable'
    h = Harness();h.provider.forbidden = True
    result = h.send('ดูนัดพรุ่งนี้')
    assert result.status == 'unavailable' and '0 รายการ' not in result.message
    monkeypatch.setattr(h.sheets, 'saved_entries', lambda *a: (_ for _ in ()).throw(NotReady('incomplete')))
    result = h.send('สรุปรายจ่ายวันนี้')
    assert result.status == 'unavailable' and '0.00' not in result.message


def test_saved_read_does_not_reuse_old_expense_review_consent():
    from test_raw_history import Harness as ExpenseHarness
    h = ExpenseHarness();assert h.prepare('ค่ากระดาษ 228 บาท').status == 'preview'
    before = deepcopy(h.draft())
    assert h.send('สรุปรายจ่ายวันนี้').status == 'summary'
    assert h.draft() == before
    assert h.send('ยืนยัน').status == 'clarification'
    assert not h.sheets.entries and not h.state.actions


def test_listing_is_bounded_but_total_uses_all_rows():
    h = Harness();seed(h)
    h.sheets.entries = [h.sheets.entries[1]] * 25
    result = h.send('ดูรายการรายจ่ายวันนี้')
    assert '5.00' in result.message and result.data['tool_result']['count'] == 25
    assert len(result.data['tool_result']['entries']) == 20 and '20 รายการแรกจากทั้งหมด 25' in result.message


def test_explicit_new_query_clears_old_filter_and_labels_keep_read_words():
    h = Harness();seed(h)
    assert '0.30' in h.send('สรุปรายจ่ายวันนี้ กาแฟ').message
    assert not h.send('สรุปรายจ่ายเดือนนี้').data['read_query']['filter']
    h.sheets.rows.append(('ค่าดูแล', 'Work', ''))
    h.sheets.entries.append(Transaction(item='ค่าดูแล', amount='7.00', date='2026-10-16', kind='expense'))
    assert '7.00' in h.send('ดูรายการรายจ่ายวันนี้ ค่าดูแล').message


def test_natural_ranges_category_shorthand_and_cross_domain_followup():
    h = Harness();seed(h)
    assert '0.30' in h.send('ค่าอาหารวันนี้เท่าไหร่').message
    assert '25.00' in h.send('สรุปรายจ่ายตั้งแต่วันที่ 2026-10-14 ถึงวันที่ 2026-10-15').message
    result = h.send('แล้วนัดล่ะ')
    assert result.data['read_query']['start'] == '2026-10-14' and result.data['read_query']['end'] == '2026-10-15'
    result = h.send('ส่วนรายจ่ายล่ะ')
    assert '25.00' in result.message
    assert h.send('ดูรายการที่บันทึกไว้วันนี้').data['tool_result']['count'] == 2


def test_signed_cold_restore_read_and_factual_realization_replay():
    from test_response_realization import VoiceHosted
    from test_calendar_text_confirmation_v13 import turn
    from fastapi.testclient import TestClient
    api = VoiceHosted()
    api.calendar.events = {'day': event('day', '2026-10-17', '2026-10-18', all_day=True)}
    result, original = turn(api, 'ดูนัดพรุ่งนี้')
    assert result.status == 'answer' and result.data['tool_result']['count'] == 1
    assert result.data['response_realization']['status'] == 'llm'
    assert 'ทั้งวัน' in result.message and api.calendar.posts == api.google.sheet_writes == 0
    before = len(api.model_bodies), len(api.calendar_requests)
    with TestClient(api.app()) as client:assert api.send(client, deepcopy(original)).status_code == 200
    assert (len(api.model_bodies), len(api.calendar_requests)) == before
    result, _ = turn(api, 'แล้วเมื่อวานล่ะ')
    assert result.data['read_query']['start'] == '2026-10-15' and result.data['tool_result']['count'] == 0
    # A new factory performs the complete Sheets read behind the same fence.
    serial = date_to_serial(date(2026, 10, 16))
    for col, value in enumerate([serial, 'กาแฟ', 12.5, ''], 1):api.google.cells[('All Expenses', 2, col)] = value
    result, _ = turn(api, 'ดูรายการรายจ่ายวันนี้')
    assert result.status == 'summary' and '12.50' in result.message and result.data['response_realization']['status'] == 'llm'
    assert api.calendar.posts == api.google.sheet_writes == 0


@pytest.mark.parametrize('fault', ['wrong_workbook', 'delivery_failed', 'expired'])
def test_failed_or_foreign_delivery_is_not_query_context(fault):
    from test_response_realization import VoiceHosted
    from test_calendar_text_confirmation_v13 import turn, mutate
    api = VoiceHosted();api.reject_reply = fault == 'delivery_failed'
    result, original = turn(api, 'ดูนัดพรุ่งนี้')
    api.reject_reply = False
    if fault == 'wrong_workbook':mutate(api, lambda s: s.event_workbooks.__setitem__(original['webhookEventId'], 'other-book'))
    if fault == 'expired':api.now += timedelta(hours=25)
    result, _ = turn(api, 'แล้วเมื่อวานล่ะ', lambda b: response(text='ต้องการดูข้อมูลเรื่องไหนครับ'))
    assert 'read_query' not in result.data


def test_read_does_not_carry_calendar_creation_consent():
    from test_calendar_text_confirmation_v13 import turn
    from test_calendar_factory import Hosted, prepare
    api = Hosted()
    assert turn(api, 'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที', prepare)[0].status == 'preview'
    assert turn(api, 'ดูนัดพรุ่งนี้')[0].status == 'answer'
    result, _ = turn(api, 'ยืนยัน')
    assert result.status in {'clarification', 'stale'} and not api.calendar.posts


def test_calendar_pagination_deduplication_and_incomplete_fail_closed():
    calls = []
    def request(r):
        assert r.method == 'GET';calls.append(dict(r.url.params))
        if len(calls) == 1:return httpx.Response(200, json={'items': [event('one', '2026-10-17', '2026-10-18', all_day=True)], 'nextPageToken': 'next'})
        return httpx.Response(200, json={'items': [event('one', '2026-10-17', '2026-10-18', all_day=True)]})
    api = CalendarCreationApi(RestClient('calendar', httpx.MockTransport(request)), TARGET)
    start = datetime.fromisoformat('2026-10-17T00:00:00+07:00');end = start + timedelta(days=1)
    assert len(api.saved_events(start, end)) == 1 and calls[1]['pageToken'] == 'next'
    assert calls[0]['timeZone'] == 'Asia/Bangkok' and calls[0]['maxResults'] == '20'
    api.client = RestClient('calendar', httpx.MockTransport(lambda r: httpx.Response(200, json={'items': [], 'nextPageToken': 'same'})))
    with pytest.raises(NotReady):api.saved_events(start, end)


def sheets_api(rows, *, row_count=10):
    calls = []
    def request(r):
        assert r.method == 'GET';calls.append(str(r.url))
        if '/values/' in r.url.path:return httpx.Response(200, json={'values': rows})
        return httpx.Response(200, json={'spreadsheetId': 'synthetic-read', 'sheets': [
            {'properties': {'title': title, 'sheetId': identifier, 'gridProperties': {'rowCount': row_count, 'columnCount': 4}}}
            for title, identifier in [('All Expenses', 101), ('Database', 102)]]})
    return SheetsApi(RestClient('sheets', httpx.MockTransport(request)), SheetTarget(spreadsheet_id='synthetic-read', max_rows=10)), calls


def test_sheet_complete_numeric_inputs_and_unsupported_income():
    rows = [['Date', 'List', 'Cost', 'Remark'], [date_to_serial(date(2026, 10, 16)), 'กาแฟ', 0.1, '']]
    api, calls = sheets_api(rows)
    assert api.saved_entries(date(2026, 10, 16), date(2026, 10, 16))[0]['amount'] == '0.10'
    assert len(calls) == 2
    assert api.summary(date(2026, 10, 16), date(2026, 10, 16))['income'] is None


@pytest.mark.parametrize('cost', [True, '2.00', -1, float('nan'), 0.001])
def test_bad_amount_does_not_become_zero(cost):
    api, _ = sheets_api([['Date', 'List', 'Cost', 'Remark'], [46311, 'กาแฟ', cost, '']])
    with pytest.raises((NotReady, ValueError)):api.saved_entries(date(2026, 1, 1), date(2026, 12, 31))


def test_oversized_grid_and_bad_headers_fail_before_false_zero():
    api, calls = sheets_api([['Date', 'List', 'Cost', 'Remark']], row_count=11)
    with pytest.raises(NotReady):api.saved_entries(date(2026, 1, 1), date(2026, 12, 31))
    assert len(calls) == 1
    api, _ = sheets_api([['Unexpected']])
    with pytest.raises(NotReady):api.saved_entries(date(2026, 1, 1), date(2026, 12, 31))
