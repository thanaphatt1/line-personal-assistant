"""Copied appointment layouts use synthetic names, dates and providers only."""
import json
import socket

import pytest

from line_assistant import operation_composition as operation
from test_calendar_mvp import Harness, response


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Synthetic Calendar and model only')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


TITLE = 'ประชุมกลุ่มทดลองปี 2569'
LAYOUTS = [
    'เพิ่ม {title}\n\nTime: Nov 7, 2026 12:00 to 13:00',
    'เพิ่ม {title}\nWhen: November 7, 2026 12:00–13:00',
    'เพิ่ม {title}\nDate: Nov 7, 2026\nStart: 12:00\nEnd: 13:00',
    'เพิ่ม {title}\nNov 7, 2026\n12:00 to 13:00',
    'เพิ่ม {title}\n7 พ.ย. 2026\nเวลา 12:00 ถึง 13:00',
]


def proposed(body, *, batch=False, date_text='Nov 7, 2026', start='12:00', end='13:00', date_value='2026-11-07'):
    context = json.loads(body['input'][0]['content'])
    quote=lambda text:{'source_ref':context['current_source_ref'],'text':text,'occurrence':0}
    fact=lambda value,text:{'value':value,'source':quote(text)}
    event = {'draft_ref': None, 'draft_version': None,
             'source_refs': [context['current_source_ref']],
             'fields':{'title':fact(TITLE,TITLE),'date_text':fact(date_value,date_text),
                 'time_text':fact(start,start),'end_time_text':fact(end,end),
                 'end_date_text':None,'duration_text':None,'all_day':None},
             'uncertain_fields':[],'approximate_fields':[],'unsupported_features':[]}
    return response('prepare_calendar_events', {'batch_ref': None, 'batch_version': None,
                    'events': [event]}) if batch else response('prepare_calendar_event', event)


@pytest.mark.parametrize('word,expected', [
    ('Nov 7, 2026', '2026-11-07'),
    ('November 7 2026', '2026-11-07'),
    ('NOV 7', '2026-11-07'),
    ('February 28, 2027', '2027-02-28'),
])
def test_model_interpreted_dates_keep_the_original_owner_quote(word, expected):
    h=Harness()
    result=h.send(f'เพิ่ม {TITLE}\n{word} 12:00 to 13:00',
        lambda body:proposed(body,date_text=word,date_value=expected))
    assert result.status=='preview'
    assert h.draft()['start'].startswith(expected)
    assert h.draft()['anchors']['date_text']['temporal']['source_text']==word


def test_an_invalid_model_interpreted_civil_date_is_rejected():
    h=Harness()
    result=h.send(f'เพิ่ม {TITLE}\nFebruary 30, 2027 12:00 to 13:00',
        lambda body:proposed(body,date_text='February 30, 2027',date_value='2027-02-30'))
    assert result.status=='clarification' and h.provider.posts==0


@pytest.mark.parametrize('layout', LAYOUTS)
@pytest.mark.parametrize('batch', [False, True])
def test_one_multiline_appointment_is_reviewed_in_one_planner_call(layout, batch):
    h = Harness()
    text = layout.format(title=TITLE)
    literal = 'November 7, 2026' if 'November' in text else '7 พ.ย. 2026' if 'พ.ย.' in text else 'Nov 7, 2026'
    result = h.send(text, lambda body: proposed(body, batch=batch, date_text=literal))
    assert result.status == 'preview', result
    assert len(h.model.requests) == 1 and h.provider.posts == 0
    assert len(h.state.calendar_drafts) == 1
    draft = h.draft()
    assert draft['facts']['title'] == TITLE
    assert draft['start'] == '2026-11-07T12:00:00+07:00'
    assert draft['end'] == '2026-11-07T13:00:00+07:00'
    assert draft['anchors']['date_text']['temporal']['source_text'] == literal
    assert all(draft['fact_sources'][field] == draft['fact_sources']['title']
               for field in ['date_text', 'time_text', 'end_time_text'])
    assert result.data['calendar_notices'] == ([] if not batch else {})
    review = operation.fallback(operation.for_result(result))
    assert 'ไม่ได้ตรวจ' not in review and 'ไม่พบนัดทับ' not in review
    assert '7 พ.ย. 2026' in review and '12:00–13:00' in review
    confirmed = h.send(result.data['confirm_command'])
    assert confirmed.status == 'created' and h.provider.posts == 1


@pytest.mark.parametrize('extra', [
    '\nNov 8, 2026 14:00 to 15:00',
    '\nTime: Nov 8, 2026 14:00 to 15:00',
    '\nวาดรูป 16:00 to 17:00',
    '\n18:00 to 19:00',
    '\nกิจกรรมทดลองอีกเรื่อง',
])
def test_model_declared_incomplete_siblings_block_partial_creation(extra):
    h = Harness()
    text = LAYOUTS[0].format(title=TITLE) + extra
    def incomplete(body):
        first=proposed(body,batch=True)
        args=json.loads(first['output'][0]['arguments'])
        sibling=json.loads(json.dumps(args['events'][0]))
        sibling['fields']={key:None for key in sibling['fields']}
        args['events'].append(sibling)
        return response('prepare_calendar_events',args)
    result = h.send(text, incomplete)
    assert result.status == 'clarification'
    assert 'confirm_command' not in result.data and h.provider.posts == 0


def test_multiline_layout_does_not_accept_a_different_clock_from_the_model():
    h = Harness()
    result = h.send(LAYOUTS[0].format(title=TITLE), lambda body: proposed(body, end='14:00'))
    assert result.status == 'clarification' and h.provider.posts == 0
    assert 'confirm_command' not in result.data


def test_real_conflict_warning_remains_a_required_review_fact(monkeypatch):
    h = Harness()
    monkeypatch.setattr(h.app.calendar_flow.adapter, 'conflicts', lambda *args: [{'id': 'synthetic-busy'}])
    result = h.send(LAYOUTS[0].format(title=TITLE), proposed)
    assert result.status == 'preview'
    view = operation.for_result(result)
    assert any('มีนัดทับ' in value for value in view['facts'].values())
    assert 'มีนัดทับ' in operation.fallback(view)
    assert h.provider.posts == 0


@pytest.mark.parametrize('batch', [False, True])
def test_signed_hosted_layout_has_a_quiet_review_and_survives_confirmation_after_restart(batch):
    from test_response_realization import VoiceHosted
    from test_calendar_text_confirmation_v13 import turn
    api = VoiceHosted()
    result, _ = turn(api, LAYOUTS[0].format(title=TITLE),
                     lambda body: proposed(body, batch=batch))
    assert result.status == 'preview' and result.data['response_realization']['status'] == 'llm'
    assert len(api.model_bodies) == 2 and len(api.voice_bodies) == 1
    assert api.calendar.posts == 0
    assert 'ไม่ได้ตรวจ' not in result.message and 'ไม่พบนัดทับ' not in result.message
    assert TITLE in result.message and '7 พ.ย. 2026' in result.message
    button = result.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    assert button.startswith('c2|confirm|' if batch else 'c1|confirm|')
    # turn() opens a fresh application backed by the same durable fake state.
    saved, _ = turn(api, button, postback=True)
    assert saved.status == 'created' and api.calendar.posts == 1
