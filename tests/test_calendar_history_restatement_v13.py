"""Retained 24h requests are evidence, not additional appointment occurrences."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest
from test_calendar_mvp import Harness,response
from test_calendar_batches_v13 import prepare,children,batch
from line_assistant.history_context import HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history
A='Stellar Metrics with Data Canvas';B='Signal Analytics with Generative Models'
TEXT=f'เพิ่มปฏิทิน 22 Oct {A} และ และ 23 Oct {B}'
FULL=TEXT+' เวลา 8:00-17:00 ทั้งสองวัน'
@pytest.fixture(autouse=True)
def offline(monkeypatch):
 def deny(*a,**k):raise AssertionError('No real network, Calendar, Sheets or credentials')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)
def retained():
 h=Harness();h.send(TEXT,response(text='ขอเวลาเพิ่มเติม ยังไม่ได้สร้างนัด'));h.now+=timedelta(minutes=1)
 h.send(FULL,response(text='ยังไม่ได้สร้างนัด'));h.now+=timedelta(hours=9,minutes=34)
 return h
def multi_refs(h,*,omit_second=False):
 def call(body):
  c=json.loads(body['input'][0]['content']);prior=[r['source_ref'] for r in c['history'] if r['role']=='user'];ref=c['current_source_ref']
  events=[]
  for i,(title,date) in enumerate([(A,'22 Oct'),(B,'23 Oct')][:1 if omit_second else 2]):
   events.append({'draft_ref':None,'draft_version':None,'title':title,'date_text':date,'time_text':'8:00','end_date_text':None,'end_time_text':'17:00','duration_text':None,'source_refs':[prior[i],ref]})
  return response('prepare_calendar_events',{'batch_ref':None,'batch_version':None,'events':events})
 return h.send(FULL,call)
def assert_two(h,r):
 assert r.status=='preview',r
 assert len(children(h))==2
 assert [(d['facts']['title'],d['start'],d['end']) for d in children(h)]==[(A,'2026-10-22T08:00:00+07:00','2026-10-22T17:00:00+07:00'),(B,'2026-10-23T08:00:00+07:00','2026-10-23T17:00:00+07:00')]
 assert h.provider.posts==0 and not h.sheets.entries

def test_three_retained_sources_for_same_two_events_do_not_create_six_slots():
 h=retained();r=multi_refs(h);assert_two(h,r)
 assert len({d['source_unit']['source_ref'] for d in children(h)})==1

def test_restatement_still_preserves_an_event_omitted_by_the_model():
 h=retained();r=multi_refs(h,omit_second=True)
 assert r.status=='clarification' and len(children(h))==2 and children(h)[1]['facts']['title'] is None,r
 assert 'confirm_command' not in r.data and h.provider.posts==0

def test_cold_history_migration_with_same_request_remains_two_slots():
 h=retained();saved=history_snapshot(h.state);state=HistoryState();state.clock=lambda:h.now;state.raw_journal=h.state.raw_journal;restore_history(state,saved,'mock-owner')
 cold=Harness(state=state);cold.now=h.now;cold.counter=h.counter
 r=multi_refs(cold);assert_two(cold,r)


def malformed_history_batch():
    from line_assistant import calendar_sources as sources
    h=retained();r=multi_refs(h);assert_two(h,r)
    valid=deepcopy(children(h));b=deepcopy(batch(h));c=json.loads(h.model.requests[-1]['input'][0]['content'])
    rows=[row for row in c['history'] if row['role']=='user']+[h.app._current]
    b.update(members=[],status='needs_information',review=None);staged={}
    for index,row in enumerate(rows):
        for j,unit in enumerate(sources.units(row,[A,B])):
            placeholder=h.app.calendar_flow.batches.placeholder(b,unit)
            if (index,j) in [(0,0),(1,1)]:
                d=deepcopy(valid[j]);d.update(draft_ref=placeholder['draft_ref'],ordinal=placeholder['ordinal'],source_unit=unit,status='needs_information',review=None)
                for field in ['title','date_text']:
                    d['fact_sources'][field]=[row['source_ref']];d['anchors'][field]['source_ref']=row['source_ref'];d['anchors'][field]['at']=row['at']
                d['source_refs']=list(dict.fromkeys([*d['source_refs'],row['source_ref']]))
            else:d=placeholder
            staged[d['draft_ref']]=d
    assert len(staged)==6
    h.state.calendar_drafts=staged;h.state.calendar_proofs={};h.state.calendar_batches={b['batch_ref']:b};h.state.calendar_batch_proofs={}
    for d in staged.values():h.app.calendar_flow.seal(d)
    h.app.calendar_flow.batches.seal(b)
    return h

def test_fresh_full_owner_restatement_recovers_only_empty_proven_shadows_from_six_slot_state():
    h=malformed_history_batch();before={ref:deepcopy(d) for ref,d in h.state.calendar_drafts.items()};known=[d['draft_ref'] for d in children(h) if d['facts']['title']]
    r=prepare(h,FULL,[{'index':1,'time_text':'8:00','end_time_text':'17:00'},{'index':2,'time_text':'8:00','end_time_text':'17:00'}])
    assert_two(h,r);assert [d['draft_ref'] for d in children(h)]==known
    removed=[d for ref,d in h.state.calendar_drafts.items() if ref not in known]
    assert len(removed)==4 and all(d['status']=='cancelled' and all(v is None for v in d['facts'].values()) for d in removed)
    assert all(h.state.calendar_drafts[ref]['source_unit']==old['source_unit'] for ref,old in before.items())
    saved=history_snapshot(h.state);state=HistoryState();state.raw_journal=h.state.raw_journal;restore_history(state,saved,'mock-owner')
    assert len(next(iter(state.calendar_batches.values()))['members'])==2 and len(state.calendar_drafts)==6
    event=h.event(r.data['confirm_command']);got=h.app.handle(event)
    assert got.status=='created' and h.provider.posts==2
    assert h.app.handle(deepcopy(event))==got and h.provider.posts==2

def test_shadow_proof_does_not_match_another_date_or_clock_or_extra_event_content():
    from line_assistant import calendar_sources as sources
    h=malformed_history_batch();real=next(d for d in children(h) if d['facts']['title']==A)
    template=deepcopy(children(h)[2]['source_unit'])
    for text in ['24 Oct '+A,'22 Oct '+A+' 9:00-10:00','22 Oct '+A+' และคุยอีกเรื่อง']:
        unit={**template,'text':text};assert not sources.shadow_of(unit,real)

def test_explicit_same_title_twice_in_current_message_keeps_two_real_occurrences():
    h=retained()
    text=f'เพิ่มนัด 22 Oct {A} 8:00-9:00 และ23 Oct {A} 10:00-11:00'
    r=prepare(h,text,[{'title':A,'date_text':'22 Oct','time_text':'8:00','end_time_text':'9:00'},{'title':A,'date_text':'23 Oct','time_text':'10:00','end_time_text':'11:00'}])
    assert r.status=='preview' and len(children(h))==2 and h.provider.posts==0,r

@pytest.mark.parametrize('explicit_date',[True,False])
def test_unexpired_v12_draft_relative_day_is_not_silently_reused_after_midnight(explicit_date):
    from datetime import datetime
    h=Harness();h.now=datetime.fromisoformat('2026-10-16T16:59:00+00:00')
    old=h.prepare(f'นัด {A} พรุ่งนี้8:00-17:00',title=A,date_text='พรุ่งนี้',time_text='8:00',end_time_text='17:00')
    assert old.status=='preview';original=deepcopy(h.draft());h.now+=timedelta(minutes=2)
    text=f'เพิ่มปฏิทิน พรุ่งนี้ {A} และ 23 Oct {B} เวลา 8:00-17:00 ทั้งสองวัน'
    def call(body):
        c=json.loads(body['input'][0]['content']);ref=c['current_source_ref']
        specs=[{'draft_ref':original['draft_ref'],'draft_version':original['version'],'title':A,'date_text':'พรุ่งนี้' if explicit_date else None},
               {'draft_ref':None,'draft_version':None,'title':B,'date_text':'23 Oct'}]
        values=[{**s,'time_text':'8:00','end_date_text':None,'end_time_text':'17:00','duration_text':None,'source_refs':[ref]} for s in specs]
        return response('prepare_calendar_events',{'batch_ref':None,'batch_version':None,'events':values})
    r=h.send(text,call)
    if explicit_date:
        assert r.status=='preview' and len(children(h))==2,r
        assert children(h)[0]['draft_ref']==original['draft_ref'] and children(h)[0]['start']=='2026-10-18T08:00:00+07:00'
    else:
        assert r.status=='clarification' and h.state.calendar_drafts[original['draft_ref']]['start']==original['start']
    assert h.provider.posts==0 and not h.sheets.entries

def test_model_context_projects_proven_two_slots_without_mutating_persisted_six_slot_state():
    h=malformed_history_batch();before=deepcopy([h.state.calendar_drafts,h.state.calendar_batches,h.state.calendar_proofs,h.state.calendar_batch_proofs]);seen=[]
    def inspect(body):
        c=json.loads(body['input'][0]['content']);seen.append(c['calendar_batches'][-1])
        assert len(seen[-1]['events'])==2 and [d['ordinal'] for d in seen[-1]['events']]==[1,2]
        assert h.app.calendar_flow.batches.request_views[batch(h)['batch_ref']]==h.state.calendar_batch_proofs[batch(h)['batch_ref']]
        return response(text='กำลังตรวจร่าง ยังไม่ได้สร้าง')
    h.send(FULL,inspect)
    assert seen and [h.state.calendar_drafts,h.state.calendar_batches,h.state.calendar_proofs,h.state.calendar_batch_proofs]==before
    assert h.provider.posts==0 and not h.sheets.entries

def test_different_day_restatement_does_not_hide_unproven_slots_in_model_context():
    h=malformed_history_batch();seen=[]
    def inspect(body):
        c=json.loads(body['input'][0]['content']);seen.append(c['calendar_batches'][-1])
        return response(text='ขอตรวจวันนัด')
    h.send(FULL.replace('22 Oct','24 Oct'),inspect)
    assert len(seen[-1]['events'])==6 and len(children(h))==6 and h.provider.posts==0

@pytest.mark.parametrize('second_start,second_end',[('10:00','11:00'),('8:00','9:00')])
def test_same_title_same_day_separate_occurrences_are_never_collapsed(second_start,second_end):
    h=retained()
    text=f'เพิ่มนัด 22 Oct {A} 8:00-9:00 และ 22 Oct {A} {second_start}-{second_end}'
    r=prepare(h,text,[{'title':A,'date_text':'22 Oct','time_text':'8:00','end_time_text':'9:00'},{'title':A,'date_text':'22 Oct','time_text':second_start,'end_time_text':second_end}])
    assert r.status=='preview' and len(children(h))==2,r
    assert len({d['draft_ref'] for d in children(h)})==2 and [d['start'] for d in children(h)]==['2026-10-22T08:00:00+07:00','2026-10-22T'+second_start.zfill(5)+':00+07:00']
    assert h.provider.posts==0 and not h.sheets.entries
