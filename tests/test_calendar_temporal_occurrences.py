"""Shared owner clauses can provide multiple proof-backed appointment dates."""
from copy import deepcopy
import json,socket
import pytest
from line_assistant.history_context import HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history
from test_calendar_mvp import Harness,response
from test_temporal_plans import resolve


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No live providers or model')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket.socket,'connect_ex',deny)


def proposed(body,*,duplicate=False,forge=False):
    context=json.loads(body['input'][0]['content'])
    values=context['temporal_context']['resolved']
    events=[]
    for i,value in enumerate(values):
        ref=values[0]['temporal_ref'] if duplicate else value['temporal_ref']
        if forge:ref='temporal_forged'
        events.append({'draft_ref':None,'draft_version':None,'title':'โครงการออโรรา',
            'date_text':None,'time_text':'8:00','end_time_text':'17:00',
            'temporal_refs':[{'field':'date_text','temporal_ref':ref}],
            'source_refs':[context['current_source_ref']]})
    return response('prepare_calendar_events',{'batch_ref':None,'batch_version':None,'events':events})


@pytest.mark.parametrize('words',['วันที่ 29 30 oct','วันที่ 29 และ 30 ตุลาคม','วันที่ 29, 30 October'])
def test_distinct_resolved_dates_share_the_original_title_and_time_without_new_source_text(words):
    h=Harness();text=f'เพิ่มนัดโครงการออโรรา {words} เวลา 8:00-17:00'
    r=h.send(text,resolve('29',kind='date',mode='components',date='2026-10-29'),
        resolve('30',kind='date',mode='components',date='2026-10-30'),proposed)
    assert r.status=='preview',r
    drafts=list(h.state.calendar_drafts.values())
    assert len(drafts)==2 and [d['start'][:10] for d in drafts]==['2026-10-29','2026-10-30']
    assert all(d['facts']['title']=='โครงการออโรรา' and d['start'][11:16]=='08:00' and d['end'][11:16]=='17:00' for d in drafts)
    assert all(d['anchors']['date_text']['temporal']['source_text'] in text for d in drafts)
    assert not h.provider.posts


def test_cold_restored_temporal_occurrences_confirm_and_replay_once():
    h=Harness();text='เพิ่มนัดโครงการออโรรา วันที่ 29 30 oct เวลา 8:00-17:00'
    review=h.send(text,resolve('29',kind='date',mode='components',date='2026-10-29'),
        resolve('30',kind='date',mode='components',date='2026-10-30'),proposed)
    assert review.status=='preview',review
    state=HistoryState();state.clock=lambda:h.now;state.raw_journal=h.state.raw_journal
    restore_history(state,history_snapshot(h.state),'mock-owner')
    other=Harness(state=state,provider=h.provider);other.now=h.now;other.counter=20
    event=other.event(review.data['confirm_command']);saved=other.app.handle(event)
    assert saved.status=='created' and h.provider.posts==2
    assert other.app.handle(deepcopy(event))==saved and h.provider.posts==2


@pytest.mark.parametrize('damage',['duplicate','forge'])
def test_unproved_extra_occurrence_cannot_create_a_review_or_write(damage):
    h=Harness();text='เพิ่มนัดโครงการออโรรา วันที่ 29 30 oct เวลา 8:00-17:00'
    r=h.send(text,resolve('29',kind='date',mode='components',date='2026-10-29'),
        resolve('30',kind='date',mode='components',date='2026-10-30'),
        lambda body:proposed(body,**{damage:True}),response(text='ยังจัดเตรียมไม่สำเร็จครับ'))
    assert r.status!='preview' and not h.state.calendar_drafts and not h.provider.posts
    assert 'ขอรายละเอียดนัดที่ชัดเจน' not in r.message


@pytest.mark.parametrize('extra',[' และ งานที่ยังไม่ได้ตั้งชื่อ',' จำนวน 3 นัด'])
def test_temporal_occurrences_never_absorb_unknown_content_or_drop_explicit_extra_slots(extra):
    h=Harness();text='เพิ่มนัดโครงการออโรรา วันที่ 29 30 oct เวลา 8:00-17:00'+extra
    r=h.send(text,resolve('29',kind='date',mode='components',date='2026-10-29'),
        resolve('30',kind='date',mode='components',date='2026-10-30'),proposed,response(text='ยังจัดเตรียมไม่สำเร็จครับ'))
    assert r.status!='preview' and not h.provider.posts
