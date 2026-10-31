"""The planner selects conversational facts; indexes never select write intent."""
from datetime import datetime
import json

import pytest

from test_calendar_mvp import Harness, response
from test_read_queries import plan
from test_expense_proposals import call, group, proposal, record


def read_day(day='2026-11-01', word='พรุ่งนี้'):
    h = Harness()
    h.now = datetime.fromisoformat('2026-10-31T19:00:00+07:00')
    result = h.send(word + 'มีนัดอะไรบ้าง', plan('calendar', day))
    assert result.status == 'answer'
    return h


def calendar_choice(*, use_prior=True, explicit=False, inspect=None):
    def choose(body):
        c = json.loads(body['input'][0]['content'])
        if inspect:
            inspect(c)
        original = next(row for row in c['history'] if row['role'] == 'user' and 'พรุ่งนี้' in row['text'])
        current = c['current_source_ref']
        quote = lambda ref, text: {'source_ref': ref, 'text': text, 'occurrence': 0}
        fact = lambda value, ref, text: {'value': value, 'source': quote(ref, text)}
        day = (fact('2026-11-03', current, '3 พ.ย.') if explicit else
               fact('2026-11-01', original['source_ref'], 'พรุ่งนี้') if use_prior else None)
        return response('prepare_calendar_event', {
            'draft_ref': None, 'draft_version': None,
            'source_refs': [current, original['source_ref']] if use_prior and not explicit else [current],
            'fields': {'title': fact('จองบัตรกิจกรรมจำลอง', current, 'จองบัตรกิจกรรมจำลอง'),
                       'date_text': day, 'time_text': fact('10:00', current, '10:00'),
                       'end_time_text': None, 'end_date_text': None, 'duration_text': None, 'all_day': None},
            'uncertain_fields': [], 'approximate_fields': [], 'unsupported_features': []})
    return choose


@pytest.mark.parametrize('day,word', [('2026-11-01', 'พรุ่งนี้'), ('2026-10-31', 'วันนี้'), ('2026-10-30', 'เมื่อวาน')])
def test_read_date_hint_keeps_past_present_and_future_context(day, word):
    h = read_day(day, word)
    seen = []
    def inspect(c):
        seen.extend(v for v in c['date_references'] if v.get('previous_question'))
        assert c['previous_query']['start'] == day
        return response(text='ขอรายละเอียดเพิ่มเติมครับ')
    def choose(body):
        return inspect(json.loads(body['input'][0]['content']))
    h.send('ขอดูข้อมูลต่อ', choose)
    assert [v['date'] for v in seen] == [day]


@pytest.mark.parametrize('interruption', [False, True])
def test_calendar_can_select_an_older_owner_date_even_without_a_date_hint(interruption):
    h = read_day()
    if interruption:
        h.send('ขอบคุณครับ', response(text='ยินดีครับ'))
    def inspect(c):
        assert any(row['role'] == 'user' and row['text'] == 'พรุ่งนี้มีนัดอะไรบ้าง' for row in c['history'])
        assert bool([v for v in c['date_references'] if v.get('previous_question')]) is (not interruption)
    result = h.send('เพิ่ม จองบัตรกิจกรรมจำลอง ตอน 10:00', calendar_choice(inspect=inspect))
    assert result.status == 'clarification'
    draft = h.draft()
    assert draft['facts']['date_text'] == '2026-11-01'
    assert draft['missing_fields'] == ['end']
    assert draft['anchors']['date_text']['temporal']['source_text'] == 'พรุ่งนี้'
    assert not h.provider.posts


def test_date_hint_does_not_assign_a_day_when_the_planner_does_not_select_it():
    h = read_day()
    result = h.send('เพิ่ม จองบัตรกิจกรรมจำลอง ตอน 10:00', calendar_choice(use_prior=False))
    assert result.status == 'clarification'
    assert h.draft()['facts']['date_text'] is None
    assert set(h.draft()['missing_fields']) == {'date', 'end'}
    assert not h.provider.posts


def test_current_explicit_date_overrides_the_prior_read_scope():
    h = read_day()
    h.send('เพิ่ม จองบัตรกิจกรรมจำลอง 3 พ.ย. ตอน 10:00', calendar_choice(explicit=True))
    assert h.draft()['facts']['date_text'] == '2026-11-03'
    assert not h.provider.posts


def test_relative_fact_uses_its_original_owner_timestamp_across_midnight():
    h = read_day()
    h.now = datetime.fromisoformat('2026-11-01T00:05:00+07:00')
    h.send('เพิ่ม จองบัตรกิจกรรมจำลอง ตอน 10:00', calendar_choice())
    assert h.draft()['facts']['date_text'] == '2026-11-01'
    assert datetime.fromisoformat(h.draft()['anchors']['date_text']['at']).date().isoformat() == '2026-10-31'
    assert not h.provider.posts


def test_generic_future_date_hint_does_not_bypass_expense_write_policy():
    h = read_day()
    def choose(body):
        c = json.loads(body['input'][0]['content'])
        ref = next(v['date_ref'] for v in c['date_references'] if v.get('previous_question'))
        return call(proposal([group('c', 'กาแฟ', 'กาแฟ 45')], [record('c', 45, date_ref=ref)]))(body)
    result = h.send('เพิ่ม กาแฟ 45', choose, response(text='ขอรายละเอียดเพิ่มเติมครับ'))
    assert result.status == 'clarification'
    assert 'entries[0].future_expense_date' in result.data['tool_result']['invalid_fields']
    assert not h.state.drafts and not h.sheets.entries
