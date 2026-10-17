"""Model-selected query contracts; no fake semantic parser and no real network."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import httpx
import pytest
from line_assistant.calendar_provider import CalendarCreationApi
from line_assistant.contracts import Transaction
from line_assistant.sheets_api import SheetsApi, SheetTarget
from line_assistant.sheet_dates import date_to_serial
from line_assistant.transport import RestClient, NotReady, ApiError
from test_calendar_mvp import Harness, NOW, TARGET
from test_history_first import response


def plan(domain='expense', start='2026-10-16', end=None, *, kind=None, value=None, listing=False, measure='expense', upcoming=False, date_mode='range', relative_offset=None, filter_text=None):
    def choose(body):
        context=json.loads(body['input'][0]['content'])
        args={'start_date':start,'end_date':end if end is not None else start,'source_refs':[context['current_source_ref']],'date_mode':date_mode,'relative_offset':relative_offset}
        args.update({'filter':{'kind':kind,'value':value} if kind else None,'view':'list' if listing else 'total','measure':measure} if domain=='expense' else {'upcoming':upcoming,'filter_text':filter_text})
        return response(name='query_expenses' if domain=='expense' else 'query_calendar',args=args)
    return choose


def seed(h):
    h.sheets.rows=[('กาแฟ','Food',''),('รถเมล์','Transportation',''),('ค่ากระดาษ','Work/Office','')]
    h.sheets.entries=[Transaction(item=item,amount=amount,kind='expense',date=day) for item,amount,day in (
        ('กาแฟ','0.10','2026-10-16'),('กาแฟ','0.20','2026-10-16'),('รถเมล์','25.00','2026-10-15'),
        ('ค่ากระดาษ','120.00','2026-10-01'),('กาแฟ','90.00','2026-09-30'),('กาแฟ','40.00','2026-11-01'))]


def event(identifier,start,end,*,all_day=False,**extra):
    return {'id':identifier,'summary':'นัดจำลอง '+identifier,'start':{'date' if all_day else 'dateTime':start},'end':{'date' if all_day else 'dateTime':end},**extra}


@pytest.mark.parametrize('text',['เดือนนี้ค่าเดินทางเท่าไหร่','เดือนที่แล้วล่ะ','เมื่อวานจ่ายอะไรไปบ้าง','เดือน ต.ค. รวมให้หน่อย','food เมื่อวาน','พรุ่งนี้ Workshop','คุยเรื่องอื่น'])
def test_original_language_reaches_model_without_semantic_preemption(text):
    h=Harness();r=h.send(text,response(text='ขอรายละเอียดเพิ่มเติมครับ'))
    assert len(h.model.requests)==1 and json.loads(h.model.requests[0]['input'][0]['content'])['message']==text
    names={t['name'] for t in h.model.requests[0]['tools']}
    assert {'query_expenses','query_calendar'}<=names and 'spending_summary' not in names
    assert not h.sheets.entries and not h.state.actions


@pytest.mark.parametrize('start,end', [('2026-10-01','2026-10-31'),('2026-10-12','2026-10-18'),('2024-02-01','2024-02-29'),('2026-12-31','2027-01-01'),('2026-10-16','2026-10-16')])
def test_typed_dates_are_explicit_not_reparsed_from_owner_words(start,end):
    h=Harness();seed(h);r=h.send('ช่วงที่ต้องการ',plan(start=start,end=end))
    assert r.data['read_query']['start']==start and r.data['read_query']['end']==end
    assert r.data['tool_result']['complete']


@pytest.mark.parametrize('start,end', [('2026-10-30','2026-10-01'),('invalid','2026-10-16'),('2569-10-16','2569-10-16'),('2026-01-01','2028-01-01'),('2026-02-31','2026-02-31')])
def test_bad_date_arguments_do_not_read_or_default(start,end,monkeypatch):
    h=Harness();monkeypatch.setattr(h.sheets,'saved_entries',lambda *a:pytest.fail('invalid query read'))
    r=h.send('ข้อความ',plan(start=start,end=end));assert r.status=='clarification'
    assert r.data['tool_result']['error_code']=='date_bounds'


def test_multiturn_explicit_arguments_carry_and_clear_scope_without_mutating_business_state():
    h=Harness();seed(h);before=deepcopy(h.sheets.entries)
    assert h.send('สรุปรายจ่ายเดือนนี้',plan(start='2026-10-01',end='2026-10-31')).data['tool_result']['expense']=='145.30'
    assert h.send('เฉพาะหมวดอาหาร',plan(start='2026-10-01',end='2026-10-31',kind='category',value='Food')).data['tool_result']['expense']=='0.30'
    r=h.send('เดือนที่แล้วล่ะ',plan(start='2026-09-01',end='2026-09-30',kind='category',value='Food'))
    assert r.data['tool_result']['expense']=='90.00'
    c=json.loads(h.model.requests[-1]['input'][0]['content']);assert c['previous_query']['filter']=={'category':'Food'}
    r=h.send('เมื่อวานจ่ายอะไรไปบ้าง',plan(start='2026-10-15',listing=True))
    assert r.data['read_query']['filter'] is None and r.data['tool_result']['count']==1
    assert h.sheets.entries==before and not h.state.actions and not h.state.drafts and not h.sheets.write_log


def test_new_query_and_literal_catalog_words_are_not_stripped():
    h=Harness();seed(h);h.sheets.rows.append(('ค่าดูแลรายการยอด','Work',''))
    h.sheets.entries.append(Transaction(item='ค่าดูแลรายการยอด',amount='7.00',date='2026-10-16',kind='expense'))
    r=h.send('ดูรายการยอดนี้',plan(kind='item',value='ค่าดูแลรายการยอด',listing=True));assert r.data['tool_result']['expense']=='7.00'
    assert 'ค่าดูแลรายการยอด' in r.message


@pytest.mark.parametrize('measure',['income','balance'])
def test_income_never_fabricates_zero(measure):
    h=Harness();r=h.send('ข้อมูลเงิน',plan(measure=measure))
    assert r.status=='income_unconfigured' and '0.00' not in r.message and not r.data['tool_result']['complete']


def test_unknown_filter_and_unmapped_historical_rows_have_distinct_honest_reasons():
    h=Harness();seed(h)
    r=h.send('หมวดใหม่',plan(kind='category',value='Unknown'));assert r.data['tool_result']['error_code']=='filter_unknown'
    h.sheets.rows=[('กาแฟ','Food',''),('รถเมล์','Transportation','')]
    r=h.send('เดือนนี้อาหาร',plan(start='2026-10-01',end='2026-10-31',kind='category',value='Food'))
    assert r.data['tool_result']['error_code']=='mapping_incomplete' and 'expense' not in r.data['tool_result']
    assert h.send('ทั้งหมด',plan(start='2026-10-01',end='2026-10-31')).data['tool_result']['expense']=='145.30'


def test_complete_empty_and_bounded_listing():
    h=Harness();seed(h)
    r=h.send('วันว่าง',plan(start='2026-10-17'));assert r.data['tool_result']['expense']=='0.00' and r.data['tool_result']['complete']
    h.sheets.entries=[h.sheets.entries[1]]*25
    r=h.send('รายการทั้งหมด',plan(listing=True));assert r.data['tool_result']['count']==25 and r.data['tool_result']['expense']=='5.00'
    assert len(r.data['tool_result']['entries'])==20 and '20 รายการแรก' in r.message


@pytest.mark.parametrize('failure,reason',[(NotReady('private schema detail'),'schema_failure'),(ApiError(403),'provider_failure'),(ValueError('private detail'),'schema_failure')])
def test_provider_failures_never_publish_details_or_zero(failure,reason,monkeypatch):
    h=Harness()
    def fail(*a):raise failure
    monkeypatch.setattr(h.sheets,'saved_entries',fail)
    r=h.send('อ่าน',plan());assert r.data['tool_result']['error_code']==reason and not r.data['tool_result']['complete']
    assert 'private' not in r.message and '0.00' not in r.message


def test_calendar_preserves_transparent_all_day_overlap_and_exclusive_end():
    h=Harness();h.provider.events={e['id']:e for e in [event('day','2026-10-17','2026-10-18',all_day=True),event('multi','2026-10-16','2026-10-19',all_day=True),event('free','2026-10-17T09:00:00+07:00','2026-10-17T10:00:00+07:00',transparency='transparent'),event('cancelled','2026-10-17','2026-10-18',all_day=True,status='cancelled'),event('outside','2026-10-18','2026-10-19',all_day=True)]}
    r=h.send('นัด',plan('calendar','2026-10-17'))
    assert r.data['tool_result']['count']==3 and 'ทั้งวัน' in r.message and '18 ต.ค. 2026' in r.message and '09:00' in r.message
    assert not h.provider.posts


def test_calendar_bounds_disabled_and_upcoming():
    h=Harness();assert h.send('ทั้งเดือน',plan('calendar','2026-10-01','2026-10-31')).data['tool_result']['error_code']=='date_bounds'
    h.provider.events={'past':event('past','2026-10-16T09:00:00+07:00','2026-10-16T10:00:00+07:00'),'current':event('current','2026-10-16T14:00:00+07:00','2026-10-16T16:00:00+07:00')}
    assert h.send('ถัดไป',plan('calendar','2026-10-16','2026-10-22',upcoming=True)).data['tool_result']['count']==1
    assert Harness(enabled=False).send('นัด',plan('calendar')).data['tool_result']['error_code']=='calendar_disabled'


def test_signed_cold_restore_realization_and_replay_are_factual():
    from test_response_realization import VoiceHosted
    from test_calendar_text_confirmation_v13 import turn
    from fastapi.testclient import TestClient
    api=VoiceHosted();api.calendar.events={'day':event('day','2026-10-17','2026-10-18',all_day=True)}
    r,original=turn(api,'ดูนัดพรุ่งนี้',plan('calendar','2026-10-17'))
    assert r.data['tool_result']['count']==1 and r.data['response_realization']['status']=='llm'
    assert set(r.data['query_facts'])=={'period','count','row1'} and '17 ต.ค. 2026' in r.message
    before=len(api.model_bodies),len(api.calendar_requests)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(original)).status_code==200
    assert (len(api.model_bodies),len(api.calendar_requests))==before and not api.calendar.posts and not api.google.sheet_writes


@pytest.mark.parametrize('fault',['wrong_workbook','delivery_failed','expired'])
def test_failed_foreign_or_expired_delivery_not_admitted_as_scope(fault):
    from test_response_realization import VoiceHosted
    from test_calendar_text_confirmation_v13 import turn,mutate
    api=VoiceHosted();api.reject_reply=fault=='delivery_failed'
    _,original=turn(api,'นัดพรุ่งนี้',plan('calendar','2026-10-17'));api.reject_reply=False
    if fault=='wrong_workbook':mutate(api,lambda s:s.event_workbooks.__setitem__(original['webhookEventId'],'other-book'))
    if fault=='expired':api.now+=timedelta(hours=25)
    turn(api,'แล้วล่ะ',response(text='ต้องการดูข้อมูลเรื่องไหนครับ'))
    body=next(b for b in reversed(api.model_bodies) if b.get('tool_choice')=='auto')
    assert json.loads(body['input'][0]['content'])['previous_query'] is None


def test_query_cannot_reuse_prior_expense_or_calendar_confirmation():
    from test_raw_history import Harness as ExpenseHarness
    h=ExpenseHarness();assert h.prepare('ค่ากระดาษ 228 บาท').status=='preview';before=deepcopy(h.draft())
    assert h.send('ดูรายจ่าย',plan()).status=='summary' and h.draft()==before
    assert h.send('ยืนยัน').status=='clarification' and not h.sheets.entries and not h.state.actions
    from test_calendar_text_confirmation_v13 import turn
    from test_calendar_factory import Hosted,prepare
    api=Hosted();assert turn(api,'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',prepare)[0].status=='preview'
    assert turn(api,'ดูนัด',plan('calendar','2026-10-17'))[0].status=='answer'
    assert turn(api,'ยืนยัน')[0].status in {'clarification','stale'} and not api.calendar.posts


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
