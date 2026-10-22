"""Source binding and durable multi-event effects. Every integration is fake."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest
from test_calendar_mvp import Harness,FakeCalendar,response,ProviderFailure
from line_assistant.history_context import HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history

ORIGINAL='เพิ่มปฏิทิน 22 Oct Dashboard Design with Power BI และ และ 23 Oct Business Analytics with Generative AI'
A='Dashboard Design with Power BI';B='Business Analytics with Generative AI'
SYNTH='เพิ่มนัด 22 Oct อบรมภาพดาว และ 23 Oct เวิร์กช็อปหุ่นยนต์'

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No real network, Calendar, Sheets or credentials')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def prepare(h,text,events,*,new=False):
    def call(body):
        c=json.loads(body['input'][0]['content']);batch=c['calendar_batches'][-1] if c['calendar_batches'] and not new else None
        values=[]
        for spec in events:
            spec=deepcopy(spec);index=spec.pop('index',None);d=next((d for d in batch['events'] if d['ordinal']==index),None) if batch and index else None
            values.append({'draft_ref':d['draft_ref'] if d else None,'draft_version':d['version'] if d else None,
                **dict.fromkeys(['title','date_text','time_text','end_date_text','end_time_text','duration_text']),
                'source_refs':[c['current_source_ref']],**spec})
        return response('prepare_calendar_events',{'batch_ref':batch['batch_ref'] if batch else None,'batch_version':batch['version'] if batch else None,'events':values})
    return h.send(text,call)
def two(h,complete=False):
    specs=[{'title':'อบรมภาพดาว','date_text':'22 Oct'},{'title':'เวิร์กช็อปหุ่นยนต์','date_text':'23 Oct'}]
    if complete:
        for e in specs:e.update(time_text='8:00',end_time_text='17:00')
    return prepare(h,SYNTH+(' เวลา 8:00-17:00 ทั้งสองวัน' if complete else ''),specs)
def batch(h):return next(reversed(h.state.calendar_batches.values()))
def children(h):return [h.state.calendar_drafts[ref] for ref in batch(h)['members']]
def control(h,text,operation,ordinals=()):
    def call(body):
        c=json.loads(body['input'][0]['content']);b=c['calendar_batches'][-1]
        return response('control_calendar_events',{'batch_ref':b['batch_ref'],'batch_version':b['version'],'operation':operation,
            'event_refs':[d['draft_ref'] for d in b['events'] if d['ordinal'] in ordinals]})
    return h.send(text,call)

def test_exact_user_courses_and_shared_time_followup_preserve_both_slots():
    h=Harness();first=prepare(h,ORIGINAL,[{'title':A,'date_text':'22 Oct'},{'title':B,'date_text':'23 Oct'}])
    assert first.status=='clarification',first
    refs=[d['draft_ref'] for d in children(h)];assert len(set(refs))==2
    r=prepare(h,'เวลา 8:00-17:00 ทั้งสองวัน',[{'index':1,'time_text':'8:00','end_time_text':'17:00'}])
    assert r.status=='preview',r
    assert [d['draft_ref'] for d in children(h)]==refs
    assert [(d['facts']['title'],d['start'],d['end']) for d in children(h)]==[
        (A,'2026-10-22T08:00:00+07:00','2026-10-22T17:00:00+07:00'),(B,'2026-10-23T08:00:00+07:00','2026-10-23T17:00:00+07:00')]
    assert not h.provider.events and not h.sheets.entries # Never confirm the real course titles, even in fixtures.

def test_model_omission_remains_visible_pending_slot_and_blocks_confirmation():
    h=Harness();r=prepare(h,SYNTH+' เวลา 8:00-17:00 ทั้งสองวัน',[{'title':'อบรมภาพดาว','date_text':'22 Oct','time_text':'8:00','end_time_text':'17:00'}])
    assert r.status=='clarification' and len(children(h))==2,r
    assert 'เวิร์กช็อปหุ่นยนต์' in r.message and children(h)[1]['facts']['title'] is None
    assert 'confirm_command' not in r.data and h.send('ยืนยัน').status!='created' and h.provider.posts==0

@pytest.mark.parametrize('wrong',[{'title':'เวิร์กช็อปหุ่นยนต์','date_text':'22 Oct'},{'title':'อบรมภาพดาว','date_text':'23 Oct'}])
def test_titles_cannot_borrow_the_other_event_date(wrong):
    h=Harness();r=prepare(h,SYNTH,[wrong])
    assert r.status=='clarification' and not h.provider.events
    assert not any(d['status']=='review' for d in h.state.calendar_drafts.values())

def test_same_date_shared_leading_expression_and_distinct_times():
    h=Harness();r=prepare(h,'พรุ่งนี้ นัดตรวจตา 9:00-10:00 และซ้อมไวโอลิน 14:00-15:00',[
        {'title':'ตรวจตา','date_text':'พรุ่งนี้','time_text':'9:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview',r
    assert [d['start'] for d in children(h)]==['2026-10-17T09:00:00+07:00','2026-10-17T14:00:00+07:00']

def test_partial_clarification_only_second_duration_preserves_first_and_version():
    h=Harness();r=prepare(h,'นัดคุยฟ้า 22 Oct 9:00-10:00 และนัดคุยเขียว 23 Oct 14:00',[
        {'title':'คุยฟ้า','date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'},
        {'title':'คุยเขียว','date_text':'23 Oct','time_text':'14:00'}])
    assert r.status=='clarification';first=deepcopy(children(h)[0])
    r=prepare(h,'นัดที่สองใช้เวลา 45 นาที',[{'index':2,'duration_text':'45 นาที'}])
    assert r.status=='preview',r
    assert children(h)[0]['version']==first['version'] and children(h)[0]['facts']==first['facts']
    assert children(h)[1]['end']=='2026-10-23T14:45:00+07:00'

def test_per_event_edit_stales_old_batch_and_wrong_target_is_rejected():
    h=Harness();old=two(h,True);first=deepcopy(children(h)[0])
    fresh=prepare(h,'แก้นัดที่สองสิ้นสุด 18:00',[{'index':2,'end_time_text':'18:00'}])
    assert fresh.status=='preview',fresh
    assert children(h)[0]['facts']==first['facts'] and children(h)[0]['version']==first['version']
    assert h.send(old.data['confirm_command']).status=='stale' and h.provider.posts==0
    wrong=prepare(h,'แก้นัดที่สองสิ้นสุด 19:00',[{'index':1,'end_time_text':'19:00'}])
    assert wrong.status=='clarification' and children(h)[0]['end']==first['end']

def test_remove_second_keeps_first_identity_and_old_consent_is_stale():
    h=Harness();old=two(h,True);ref=children(h)[0]['draft_ref']
    r=control(h,'ยกเลิกนัดที่สอง','remove',[2])
    assert r.status=='preview',r
    assert children(h)[0]['draft_ref']==ref and children(h)[1]['status']=='cancelled'
    assert h.send(old.data['confirm_command']).status=='stale' and h.provider.posts==0
    assert h.send(r.data['confirm_command']).status=='created' and h.provider.posts==1

def test_add_third_retains_existing_members_and_unique_refs():
    h=Harness();old=two(h,True);refs=[d['draft_ref'] for d in children(h)]
    r=prepare(h,'เพิ่มนัดอีก 24 Oct อ่านแผนที่ 10:00-11:00',[{'title':'อ่านแผนที่','date_text':'24 Oct','time_text':'10:00','end_time_text':'11:00'}])
    assert r.status=='preview',r
    assert [d['draft_ref'] for d in children(h)][:2]==refs and len(set(d['draft_ref'] for d in children(h)))==3
    assert h.send(old.data['confirm_command']).status=='stale'

def test_duplicate_titles_are_distinct_occurrences_not_collapsed():
    h=Harness();r=prepare(h,'นัดซ้อมดนตรี 22 Oct 9:00-10:00 และนัดซ้อมดนตรี 23 Oct 14:00-15:00',[
        {'title':'ซ้อมดนตรี','date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'},
        {'title':'ซ้อมดนตรี','date_text':'23 Oct','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview',r
    assert len(children(h))==2 and len(set(d['draft_ref'] for d in children(h)))==2

def test_batch_confirmation_and_webhook_replay_create_each_event_once():
    h=Harness();r=two(h,True);event=h.event(r.data['confirm_command']);calls=len(h.model.requests)
    saved=h.app.handle(event)
    assert saved.status=='created' and len(saved.data['created_refs'])==2 and h.provider.posts==2
    assert h.app.handle(deepcopy(event))==saved and h.provider.posts==2 and len(h.model.requests)==calls
    assert h.send(r.data['confirm_command']).status=='stale' and h.provider.posts==2
    assert not h.sheets.entries

class PartialProvider(FakeCalendar):
    def __init__(self,before=False):super().__init__();self.before=before
    def request(self,request):
        if request.method=='POST' and self.posts==1:
            self.fail_before_create=self.before;self.fail_after_create=not self.before
        return super().request(request)

@pytest.mark.parametrize('cold',[False,True])
def test_partial_success_lost_second_acknowledgement_reconciles_without_duplicate_posts(cold):
    provider=PartialProvider();h=Harness(provider=provider);r=two(h,True);event=h.event(r.data['confirm_command'])
    with pytest.raises(ProviderFailure):h.app.handle(event)
    assert provider.posts==2 and len(provider.events)==2 and children(h)[0]['status']=='recorded'
    assert h.state.claims[event['webhookEventId']].action_key.startswith('mock-owner:calendar-batch:')
    if cold:
        snap=history_snapshot(h.state);state=HistoryState();state.raw_journal=h.state.raw_journal;state.clock=lambda:h.now
        restore_history(state,snap,'mock-owner');fresh=Harness(state=state,provider=provider);fresh.now=h.now;fresh.counter=100;h=fresh
    provider.fail_after_create=False
    got=h.app.handle(deepcopy(event));assert got.status=='created' and provider.posts==2
    assert h.app.handle(deepcopy(event))==got and provider.posts==2

def test_unknown_absent_second_event_blocks_more_posts_and_removal():
    provider=PartialProvider(before=True);h=Harness(provider=provider);r=two(h,True);event=h.event(r.data['confirm_command'])
    with pytest.raises(ProviderFailure):h.app.handle(event)
    provider.fail_before_create=False
    got=h.app.handle(deepcopy(event))
    assert got.status=='uncertain' and len(got.data['created_refs'])==1 and len(got.data['pending_refs'])==1 and provider.posts==2
    again=h.send('ตรวจผลชุดนัด');assert again.status=='uncertain' and provider.posts==2
    removed=control(h,'ยกเลิกนัดที่สอง','remove',[2]);assert removed.status=='clarification' and provider.posts==2

def test_foreign_offset_still_blocks_a_multi_event_request():
    h=Harness();r=prepare(h,SYNTH+' เวลา 8:00-17:00 ทั้งสองวัน JST',[
        {'title':'อบรมภาพดาว','date_text':'22 Oct','time_text':'8:00','end_time_text':'17:00'},
        {'title':'เวิร์กช็อปหุ่นยนต์','date_text':'23 Oct','time_text':'8:00','end_time_text':'17:00'}])
    assert r.status=='clarification' and r.data['tool_result']['error']=='calendar_timezone_unsupported' and h.provider.posts==0

def test_off_bridge_preserves_batch_and_will_not_resume_unknown_effect():
    provider=PartialProvider(before=True);h=Harness(provider=provider);r=two(h,True);event=h.event(r.data['confirm_command'])
    with pytest.raises(ProviderFailure):h.app.handle(event)
    snap=history_snapshot(h.state);state=HistoryState();state.raw_journal=h.state.raw_journal;state.clock=lambda:h.now;restore_history(state,snap,'mock-owner')
    off=Harness(state=state,provider=provider,enabled=False);off.now=h.now;calls=len(provider.calls)
    assert off.app.handle(deepcopy(event)).status=='uncertain' and len(provider.calls)==calls and history_snapshot(off.state)['calendar_batches_v1']==snap['calendar_batches_v1']

def test_shared_date_edit_applies_to_both_and_keeps_each_clock():
    h=Harness();two(h,True)
    r=prepare(h,'ย้ายทั้งสองนัดเป็นวันที่ 24 Oct',[{'index':1,'date_text':'24 Oct'}])
    assert r.status=='preview',r
    assert all(d['start']=='2026-10-24T08:00:00+07:00' and d['end']=='2026-10-24T17:00:00+07:00' for d in children(h))

def test_title_edit_has_new_current_evidence_without_erasing_original_slot():
    h=Harness();two(h,True);old=deepcopy(children(h)[1])
    r=prepare(h,'เปลี่ยนชื่อนัดที่สองเป็น เวิร์กช็อปแสงเหนือ',[{'index':2,'title':'เวิร์กช็อปแสงเหนือ'}])
    assert r.status=='preview',r
    assert children(h)[1]['source_unit']==old['source_unit'] and children(h)[1]['start']==old['start']

def test_adopt_existing_single_event_when_owner_adds_another_appointment():
    h=Harness();old=h.prepare('นัดอ่านหนังสือพรุ่งนี้9:00 30นาที',title='อ่านหนังสือ',date_text='พรุ่งนี้',time_text='9:00',duration_text='30นาที')
    oldref=h.draft()['draft_ref'];oldversion=h.draft()['version']
    def call(body):
        c=json.loads(body['input'][0]['content']);ref=c['current_source_ref']
        values=[{'draft_ref':oldref,'draft_version':oldversion,**dict.fromkeys(['title','date_text','time_text','end_date_text','end_time_text','duration_text']),'source_refs':[ref]},
            {'draft_ref':None,'draft_version':None,'title':'ซ้อมเพลง','date_text':'23 Oct','time_text':'14:00','end_date_text':None,'end_time_text':'15:00','duration_text':None,'source_refs':[ref]}]
        return response('prepare_calendar_events',{'batch_ref':None,'batch_version':None,'events':values})
    r=h.send('เพิ่มนัดซ้อมเพลง 23 Oct 14:00-15:00 อีกนัดด้วย',call)
    assert r.status=='preview',r
    assert len(children(h))==2 and children(h)[0]['draft_ref']==oldref
    assert h.send(old.data['confirm_command']).status=='stale'

@pytest.mark.parametrize('fault',['nonce','version','text_postback'])
def test_forged_or_stale_batch_postback_never_writes(fault):
    h=Harness();r=two(h,True);data=r.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    if fault=='nonce':data=data[:-16]+'0'*16
    if fault=='version':data=data.replace('|1|','|99|')
    event=h.event(data,postback=fault!='text_postback')
    assert h.app.handle(event).status=='stale' and h.provider.posts==0

def test_real_batch_postback_confirms_all_without_model_call():
    h=Harness();r=two(h,True);calls=len(h.model.requests)
    data=r.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    assert h.app.handle(h.event(data,postback=True)).status=='created' and h.provider.posts==2 and len(h.model.requests)==calls

def test_after_ttl_reconcile_known_children_and_never_start_unattempted_third():
    provider=PartialProvider();h=Harness(provider=provider);two(h,True)
    r=prepare(h,'เพิ่มนัดอีก 24 Oct อ่านแผนที่ 10:00-11:00',[{'title':'อ่านแผนที่','date_text':'24 Oct','time_text':'10:00','end_time_text':'11:00'}])
    with pytest.raises(ProviderFailure):h.send(r.data['confirm_command'])
    h.now+=timedelta(days=31);provider.fail_after_create=False
    got=h.send('ตรวจผลชุดนัด')
    assert got.status=='expired' and len(got.data['created_refs'])==2 and len(got.data['pending_refs'])==1 and provider.posts==2
    assert children(h)[2]['status']=='needs_information'

def test_mixed_current_calendar_financial_intent_reaches_planner_and_keeps_raw_text():
    h=Harness();text='เพิ่มนัดอ่านหนังสือพรุ่งนี้9:00-10:00 และบันทึกรายจ่ายกาแฟ 31 บาท'
    r=h.send(text,response(text='เริ่มเตรียมนัดก่อนครับ'))
    assert len(h.model.requests)==1 and 'domain_selection_required' not in r.data
    assert any(row['text']==text for row in h.state.raw_journal.rows.values()) and not h.provider.events and not h.sheets.entries

@pytest.mark.parametrize('field',['members','owner_id','calendar_id','version'])
def test_tampered_batch_snapshot_cannot_partially_replace_state(field):
    h=Harness();two(h,True);snap=history_snapshot(h.state);bad=deepcopy(snap)
    b=next(iter(bad['calendar_batches_v1']['batches'].values()))
    b[field]=list(reversed(b[field])) if field=='members' else 99 if field=='version' else 'forged'
    fresh=HistoryState();fresh.raw_journal=h.state.raw_journal;fresh.clock=lambda:h.now
    with pytest.raises(ValueError):restore_history(fresh,bad,'mock-owner')
    assert not fresh.calendar_batches and not fresh.calendar_drafts

@pytest.mark.parametrize('separator',[' และ ',' และ และ ','\n','; ', ', ', ' and '])
@pytest.mark.parametrize('shared',['ทั้งสองวัน','ทั้ง 2 วัน','ทั้ง๒วัน','both days'])
def test_owner_event_separators_and_shared_time_paraphrases_keep_both(separator,shared):
    h=Harness();text='เพิ่มนัด 22 Oct อบรมภาพดาว'+separator+'23 Oct เวิร์กช็อปหุ่นยนต์'
    first=prepare(h,text,[{'title':'อบรมภาพดาว','date_text':'22 Oct'},{'title':'เวิร์กช็อปหุ่นยนต์','date_text':'23 Oct'}])
    assert first.status=='clarification' and len(children(h))==2,first
    r=prepare(h,'เวลา 8:00-17:00 '+shared,[{'index':1,'time_text':'8:00','end_time_text':'17:00'}])
    assert r.status=='preview',r
    assert [d['start'] for d in children(h)]==['2026-10-22T08:00:00+07:00','2026-10-23T08:00:00+07:00']

def test_thai_digits_shared_clocks_store_original_owner_spans_for_every_slot():
    h=Harness();two(h)
    r=prepare(h,'เวลา ๘:๐๐-๑๗:๐๐ ทั้งสองวัน',[{'index':1,'time_text':'8:00','end_time_text':'17:00'}])
    assert r.status=='preview',r
    assert all(d['facts']['time_text']=='๘:๐๐' and d['facts']['end_time_text']=='๑๗:๐๐' for d in children(h))

def test_remove_pending_second_with_delete_word_never_deletes_provider_event():
    h=Harness();two(h,True);r=control(h,'ลบนัดที่สอง','remove',[2])
    assert r.status=='preview' and children(h)[1]['status']=='cancelled' and h.provider.posts==0

def test_missing_month_cannot_borrow_current_month_when_other_slot_has_explicit_different_month():
    h=Harness();r=prepare(h,'นัด 22 อบรมภาพดาว และ 23 Nov เวิร์กช็อปหุ่นยนต์ เวลา 8:00-17:00 ทั้งสองวัน',[
        {'title':'อบรมภาพดาว','date_text':'22','time_text':'8:00','end_time_text':'17:00'},
        {'title':'เวิร์กช็อปหุ่นยนต์','date_text':'23 Nov','time_text':'8:00','end_time_text':'17:00'}])
    assert r.status=='clarification' and h.provider.posts==0 and not any(d['status']=='review' for d in h.state.calendar_drafts.values())

def test_omitted_title_that_looks_like_filler_is_still_a_temporal_slot():
    h=Harness();r=prepare(h,'เพิ่มนัด 22 Oct อบรมภาพดาว และ 23 Oct ช่วยด้วย เวลา 8:00-17:00 ทั้งสองวัน',[
        {'title':'อบรมภาพดาว','date_text':'22 Oct','time_text':'8:00','end_time_text':'17:00'}])
    assert r.status=='clarification' and len(children(h))==2 and 'ช่วยด้วย' in r.message

def test_shared_leading_date_on_separate_line_is_not_a_third_event():
    h=Harness();r=prepare(h,'พรุ่งนี้\nนัดตรวจตา 9:00-10:00\nนัดซ้อมไวโอลิน 14:00-15:00',[
        {'title':'ตรวจตา','date_text':'พรุ่งนี้','time_text':'9:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and len(children(h))==2,r

@pytest.mark.parametrize('forged',['c2|confirm|bad','ยืนยันชุดนัด calset_00000000000000000000 bad','c2|confirm|calset_00000000000000000000|1|0000000000000000'])
def test_malformed_batch_confirmation_is_stale_without_model_or_post(forged):
    h=Harness();two(h,True);calls=len(h.model.requests)
    assert h.send(forged).status=='stale' and h.provider.posts==0 and len(h.model.requests)==calls

def test_missing_titles_keep_two_date_time_slots_then_fill_each_independently():
    h=Harness();r=prepare(h,'เพิ่มนัด 22 Oct 9:00-10:00 และ 23 Oct 14:00-15:00',[
        {'date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'},
        {'date_text':'23 Oct','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='clarification' and len(children(h))==2,r
    assert all(d['missing_fields']==['title'] for d in children(h))
    refs=[d['draft_ref'] for d in children(h)]
    r=prepare(h,'นัดแรกชื่อ ตรวจตา',[{'index':1,'title':'ตรวจตา'}]);assert r.status=='clarification'
    r=prepare(h,'นัดที่สองชื่อ ซ้อมไวโอลิน',[{'index':2,'title':'ซ้อมไวโอลิน'}]);assert r.status=='preview',r
    assert [d['draft_ref'] for d in children(h)]==refs and h.provider.posts==0

@pytest.mark.parametrize('word',['สองนัด','2 นัด','๒นัด'])
def test_explicit_count_without_facts_keeps_every_requested_slot(word):
    h=Harness();r=prepare(h,'ช่วยเพิ่ม '+word,[{},{}])
    assert r.status=='clarification' and len(children(h))==2 and len(set(d['draft_ref'] for d in children(h)))==2,r
    assert all(set(d['missing_fields'])=={'title','date','time','end'} for d in children(h))
    assert 'confirm_command' not in r.data and h.provider.posts==0

def test_shared_time_suffix_on_own_clause_is_not_an_extra_event():
    h=Harness();r=prepare(h,'เพิ่มนัด 22 Oct อบรมภาพดาว และ 23 Oct เวิร์กช็อปหุ่นยนต์ และเวลา 8:00-17:00 ทั้งสองวัน',[
        {'title':'อบรมภาพดาว','date_text':'22 Oct','time_text':'8:00','end_time_text':'17:00'},
        {'title':'เวิร์กช็อปหุ่นยนต์','date_text':'23 Oct','time_text':'8:00','end_time_text':'17:00'}])
    assert r.status=='preview' and len(children(h))==2,r

def test_count_words_inside_complete_literal_title_do_not_add_a_phantom_event():
    h=Harness();r=h.prepare('นัดอ่านหนังสือสองนัดพรุ่งนี้9:00 30นาที',title='อ่านหนังสือสองนัด',date_text='พรุ่งนี้',time_text='9:00',duration_text='30นาที')
    assert r.status=='preview' and len(h.state.calendar_drafts)==1 and not h.state.calendar_batches,r

@pytest.mark.parametrize('wrong',[False,True])
def test_multiple_source_messages_bind_each_title_to_its_own_date(wrong):
    h=Harness();h.send('เพิ่มนัด 22 Oct อบรมภาพดาว',response(text='กำลังจัดร่าง'))
    def call(body):
        c=json.loads(body['input'][0]['content']);old=next(r['source_ref'] for r in c['history'] if r['role']=='user')
        ref=c['current_source_ref'];events=[]
        for title,day in [('อบรมภาพดาว','23 Oct' if wrong else '22 Oct'),('เวิร์กช็อปหุ่นยนต์','23 Oct')]:
            events.append({'draft_ref':None,'draft_version':None,'title':title,'date_text':day,'time_text':'8:00','end_date_text':None,'end_time_text':'17:00','duration_text':None,'source_refs':[old,ref]})
        return response('prepare_calendar_events',{'batch_ref':None,'batch_version':None,'events':events})
    r=h.send('เพิ่มนัด 23 Oct เวิร์กช็อปหุ่นยนต์ เวลา 8:00-17:00 ทั้งสองวัน',call)
    if wrong:assert r.status=='clarification' and not any(d['status']=='review' for d in h.state.calendar_drafts.values())
    else:assert r.status=='preview' and [d['start'][:10] for d in children(h)]==['2026-10-22','2026-10-23'],r
    assert h.provider.posts==0

def test_cancelled_slots_are_not_resurrected_from_old_source_on_unrelated_message():
    h=Harness();two(h,True);control(h,'ยกเลิกทั้งชุด','cancel')
    def call(body):
        c=json.loads(body['input'][0]['content']);ref=next(r['source_ref'] for r in c['history'] if r['role']=='user')
        events=[{'draft_ref':None,'draft_version':None,'title':title,'date_text':day,'time_text':'8:00','end_date_text':None,'end_time_text':'17:00','duration_text':None,'source_refs':[ref]} for title,day in [('อบรมภาพดาว','22 Oct'),('เวิร์กช็อปหุ่นยนต์','23 Oct')]]
        return response('prepare_calendar_events',{'batch_ref':None,'batch_version':None,'events':events})
    r=h.send('สวัสดี',call)
    assert r.status=='clarification' and all(d['status']=='cancelled' for d in h.state.calendar_drafts.values()) and h.provider.posts==0

@pytest.mark.parametrize('link',['แล้วก็','และ',';'])
def test_natural_title_then_date_connector_keeps_dates_with_their_titles(link):
    h=Harness();r=prepare(h,'ช่วยนัดอบรมภาพดาววันที่ 22 ตุลาคม '+link+'เวิร์กช็อปหุ่นยนต์วันที่ 23 ตุลาคมด้วย',[
        {'title':'อบรมภาพดาว','date_text':'วันที่ 22 ตุลาคม'},
        {'title':'เวิร์กช็อปหุ่นยนต์','date_text':'วันที่ 23 ตุลาคม'}])
    assert r.status=='clarification' and len(children(h))==2,r
    assert [d['facts']['date_text'] for d in children(h)]==['วันที่ 22 ตุลาคม','วันที่ 23 ตุลาคม']
    refs=[d['draft_ref'] for d in children(h)]
    r=prepare(h,'ทั้งสองรายการใช้เวลา 08:00–17:00 น.',[{'index':1,'time_text':'08:00–17:00 น.'}])
    assert r.status=='preview' and [d['draft_ref'] for d in children(h)]==refs,r
    assert [(d['start'],d['end']) for d in children(h)]==[('2026-10-22T08:00:00+07:00','2026-10-22T17:00:00+07:00'),('2026-10-23T08:00:00+07:00','2026-10-23T17:00:00+07:00')]


def test_natural_then_connector_shared_leading_date_has_no_spurious_unknown_text():
    h=Harness();r=prepare(h,'พรุ่งนี้มีตรวจตาตอน 09:00–10:00 น. แล้วก็ซ้อมไวโอลิน 14:00–15:00 น. ช่วยลงให้ด้วย',[
        {'title':'ตรวจตา','date_text':'พรุ่งนี้','time_text':'09:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and len(children(h))==2,r
    assert [d['start'] for d in children(h)]==['2026-10-17T09:00:00+07:00','2026-10-17T14:00:00+07:00']


def test_explicit_repeated_single_title_has_two_independently_timed_occurrences():
    h=Harness();r=prepare(h,'ช่วยลงซ้อมดนตรีสองครั้งนะ วันที่ 22 ตุลาคม 09:00–10:00 น. แล้ววันที่ 23 ตุลาคม 14:00–15:00 น.',[
        {'title':'ซ้อมดนตรี','date_text':'วันที่ 22 ตุลาคม','time_text':'09:00','end_time_text':'10:00 น.'},
        {'title':'ซ้อมดนตรี','date_text':'วันที่ 23 ตุลาคม','time_text':'14:00','end_time_text':'15:00 น.'}])
    assert r.status=='preview' and len(children(h))==2,r
    assert [(d['facts']['title'],d['start']) for d in children(h)]==[('ซ้อมดนตรี','2026-10-22T09:00:00+07:00'),('ซ้อมดนตรี','2026-10-23T14:00:00+07:00')]
    assert h.send(r.data['confirm_command']).status=='created' and h.provider.posts==2


def test_conflicts_are_visible_and_changed_conflicts_require_fresh_batch_consent():
    h=Harness();old=two(h,True)
    h.provider.events['busy']={'id':'busy','start':{'date':'2026-10-22'},'end':{'date':'2026-10-23'},'etag':'"1"'}
    changed=h.send(old.data['confirm_command'])
    assert changed.status=='preview' and 'ทับนัดเดิม 1 รายการ' in changed.message and h.provider.posts==0
    assert h.send(old.data['confirm_command']).status=='stale'
    assert h.send(changed.data['confirm_command']).status=='created' and h.provider.posts==2


def test_within_set_overlap_is_visible_before_consent():
    h=Harness();r=prepare(h,'พรุ่งนี้ นัดตรวจตา 9:00-10:00 และซ้อมไวโอลิน 9:30-10:30',[
        {'title':'ตรวจตา','date_text':'พรุ่งนี้','time_text':'9:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'9:30','end_time_text':'10:30'}])
    assert r.status=='preview' and 'เวลาในชุดทับกัน: 1 กับ 2' in r.message and h.provider.posts==0


def test_model_repeating_clock_unit_keeps_original_clock_and_distinct_dates():
    h=Harness();r=prepare(h,'ช่วยลงซ้อมดนตรีสองครั้งนะ วันที่ 22 ตุลาคม 09:00–10:00 น. แล้ววันที่ 23 ตุลาคม 14:00–15:00 น.',[
        {'title':'ซ้อมดนตรี','date_text':'วันที่ 22 ตุลาคม','time_text':'09:00 น.','end_time_text':'10:00 น.'},
        {'title':'ซ้อมดนตรี','date_text':'วันที่ 23 ตุลาคม','time_text':'14:00 น.','end_time_text':'15:00 น.'}])
    assert r.status=='preview',r
    assert [d['facts']['time_text'] for d in children(h)]==['09:00','14:00']
    assert [d['start'] for d in children(h)]==['2026-10-22T09:00:00+07:00','2026-10-23T14:00:00+07:00']


def test_natural_addition_into_current_set_keeps_both_previous_members():
    h=Harness();two(h,True);refs=[d['draft_ref'] for d in children(h)]
    r=prepare(h,'เพิ่มอีกนัด ซ้อมเขียน วันที่ 24 Oct เวลา 10:00-11:00 เข้าในชุดนี้',[
        {'title':'ซ้อมเขียน','date_text':'24 Oct','time_text':'10:00','end_time_text':'11:00'}])
    assert r.status=='preview' and [d['draft_ref'] for d in children(h)][:2]==refs,r
    assert len(children(h))==3 and children(h)[2]['start']=='2026-10-24T10:00:00+07:00'

@pytest.mark.parametrize('leading',[True,False])
@pytest.mark.parametrize('wrong',[True,False])
def test_unconnected_two_names_keep_independent_dates_in_both_word_orders(leading,wrong):
    h=Harness();text='เพิ่มนัด '+('22 Oct อบรมภาพดาว 23 Oct เวิร์กช็อปหุ่นยนต์' if leading else 'อบรมภาพดาว 22 Oct เวิร์กช็อปหุ่นยนต์ 23 Oct')
    r=prepare(h,text,[{'title':'อบรมภาพดาว','date_text':'23 Oct' if wrong else '22 Oct'},{'title':'เวิร์กช็อปหุ่นยนต์','date_text':'23 Oct'}])
    assert r.status=='clarification' and h.provider.posts==0
    if wrong:assert not h.state.calendar_batches
    else:assert [d['facts']['date_text'] for d in children(h)]==['22 Oct','23 Oct']

@pytest.mark.parametrize('text,spec',[
    ('แก้นัดที่สองสิ้นสุด 18:00',{'index':2,'end_time_text':'19:00'}),
    ('แก้นัดที่สองเป็น 18:00 JST',{'index':2,'end_time_text':'18:00'})])
def test_rejected_update_revokes_old_batch_consent_without_changing_owned_facts(text,spec):
    h=Harness();old=two(h,True);facts=[deepcopy(d['facts']) for d in children(h)]
    r=prepare(h,text,[spec])
    assert r.status=='clarification' and [d['facts'] for d in children(h)]==facts,r
    assert h.send(old.data['confirm_command']).status=='stale' and h.provider.posts==0


def test_repeated_title_thai_clocks_and_exclamation_preserve_both_occurrences():
    h=Harness();r=prepare(h,'นัดซ้อมดนตรีวันที่ ๒๒ ต.ค. เวลา ๐๙:๐๐-๑๐:๐๐ น. และซ้อมดนตรีอีกครั้งวันที่ ๒๓ ต.ค. เวลา ๑๔:๐๐-๑๕:๐๐ น.!',[
        {'title':'ซ้อมดนตรี','date_text':'วันที่ ๒๒ ต.ค.','time_text':'เวลา ๐๙:๐๐-๑๐:๐๐ น.'},
        {'title':'ซ้อมดนตรี','date_text':'วันที่ ๒๓ ต.ค.','time_text':'เวลา ๑๔:๐๐-๑๕:๐๐ น.'}])
    assert r.status=='preview' and [d['start'] for d in children(h)]==['2026-10-22T09:00:00+07:00','2026-10-23T14:00:00+07:00'],r
    assert h.send(r.data['confirm_command']).status=='created' and h.provider.posts==2


def test_first_round_then_another_round_carries_one_explicit_name():
    h=Harness();r=prepare(h,'ขอนัดซ้อมดนตรีรอบแรก 22 ตุลาคม 09:00–10:00 น. แล้วอีกรอบวันที่ 23 ตุลาคม 14:00–15:00 น.',[
        {'title':'ซ้อมดนตรี','date_text':'22 ตุลาคม','time_text':'09:00','end_time_text':'10:00'},
        {'title':'ซ้อมดนตรี','date_text':'วันที่ 23 ตุลาคม','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and len(children(h))==2,r
    assert [d['start'] for d in children(h)]==['2026-10-22T09:00:00+07:00','2026-10-23T14:00:00+07:00']


def test_natural_insert_and_separate_clause_connectors_do_not_hide_missing_end():
    h=Harness();r=prepare(h,'ช่วยจัดคุยโครงการฟ้าในวันที่ 22 ตุลาคม เวลา 09:00–10:00 น. ส่วนคุยโครงการเขียววันที่ 23 ตุลาคม เริ่มตอน 14:00 น.',[
        {'title':'คุยโครงการฟ้า','date_text':'วันที่ 22 ตุลาคม','time_text':'09:00','end_time_text':'10:00'},
        {'title':'คุยโครงการเขียว','date_text':'วันที่ 23 ตุลาคม','time_text':'14:00'}])
    assert r.status=='clarification' and len(children(h))==2,r
    assert children(h)[0]['status']=='review' and children(h)[1]['missing_fields']==['end']

@pytest.mark.parametrize('decorated',[True,False])
def test_repeated_occurrence_markers_are_not_invented_event_names(decorated):
    h=Harness();r=prepare(h,'ขอนัดซ้อมดนตรีรอบแรก 22 ตุลาคม 09:00–10:00 น. แล้วอีกรอบวันที่ 23 ตุลาคม 14:00–15:00 น.',[
        {'title':'ซ้อมดนตรีรอบแรก' if decorated else 'ซ้อมดนตรี','date_text':'22 ตุลาคม','time_text':'09:00','end_time_text':'10:00'},
        {'title':'ซ้อมดนตรีอีกรอบ' if decorated else 'ซ้อมดนตรี','date_text':'วันที่ 23 ตุลาคม','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and [d['facts']['title'] for d in children(h)]==['ซ้อมดนตรี','ซ้อมดนตรี'],r


def test_explicitly_quoted_distinct_titles_keep_their_occurrence_words():
    h=Harness();r=prepare(h,'นัด "ซ้อมดนตรีรอบแรก" 22 Oct 9:00-10:00 และ "ซ้อมดนตรีอีกรอบ" 23 Oct 14:00-15:00',[
        {'title':'ซ้อมดนตรีรอบแรก','date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'},
        {'title':'ซ้อมดนตรีอีกรอบ','date_text':'23 Oct','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and [d['facts']['title'] for d in children(h)]==['ซ้อมดนตรีรอบแรก','ซ้อมดนตรีอีกรอบ'],r


def test_literal_combined_start_markers_do_not_leave_a_false_missing_time():
    h=Harness();r=prepare(h,'คุยโครงการฟ้า 22 Oct 9:00-10:00 และคุยโครงการเขียว 23 Oct เริ่มตอน 14:00 น.',[
        {'title':'คุยโครงการฟ้า','date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'},
        {'title':'คุยโครงการเขียว','date_text':'23 Oct','time_text':'เริ่มตอน 14:00 น.'}])
    assert r.status=='clarification' and children(h)[1]['missing_fields']==['end'] and not children(h)[1]['issues'],r
    r=prepare(h,'คุยโครงการเขียวกินเวลา 45 นาที',[{'index':2,'duration_text':'45 นาที'}])
    assert r.status=='preview' and children(h)[1]['end']=='2026-10-23T14:45:00+07:00',r


def test_model_proposal_from_quoted_text_still_cannot_create_without_new_confirmation():
    h=Harness();r=prepare(h,'"เพิ่มนัด ซ้อมดนตรี 22 Oct 9:00-10:00 และซ้อมดนตรี 23 Oct 14:00-15:00"',[
        {'title':'ซ้อมดนตรี','date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'},
        {'title':'ซ้อมดนตรี','date_text':'23 Oct','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and h.provider.posts==0 and not h.sheets.entries


@pytest.mark.parametrize('request_word',['นัด','ลง','มี'])
def test_request_verb_does_not_become_an_event_name_even_when_model_copies_it(request_word):
    h=Harness();r=prepare(h,'พรุ่งนี้'+('ช่วยใส่' if request_word=='นัด' else '')+request_word+'ตรวจตา 09:00 ถึง 10:00 น. แล้วก็ซ้อมไวโอลิน 14:00 ถึง 15:00 น. ให้หน่อย',[
        {'title':request_word+'ตรวจตา','date_text':'พรุ่งนี้','time_text':'09:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and [d['facts']['title'] for d in children(h)]==['ตรวจตา','ซ้อมไวโอลิน'],r


@pytest.mark.parametrize('request_word',['นัด','ลง','มี'])
def test_quoted_name_with_request_word_is_preserved(request_word):
    h=Harness();r=prepare(h,'นัด "'+request_word+'ตรวจตา" 22 Oct 9:00-10:00 และ "ซ้อมไวโอลิน" 23 Oct 14:00-15:00',[
        {'title':request_word+'ตรวจตา','date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'23 Oct','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and children(h)[0]['facts']['title']==request_word+'ตรวจตา',r


@pytest.mark.parametrize('prefix',['เวลา','ใช้เวลา','กินเวลา','ระยะเวลา','มีระยะเวลา','กำหนดระยะเวลาไว้','นาน'])
@pytest.mark.parametrize('ordinal',['สอง','2'])
def test_literal_duration_labels_complete_only_the_selected_missing_event(prefix,ordinal):
    h=Harness();r=prepare(h,'คุยโครงการฟ้า 22 Oct 9:00-10:00 และคุยโครงการเขียว 23 Oct 14:00',[
        {'title':'คุยโครงการฟ้า','date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'},
        {'title':'คุยโครงการเขียว','date_text':'23 Oct','time_text':'14:00'}])
    assert r.status=='clarification';refs=[d['draft_ref'] for d in children(h)]
    r=prepare(h,'นัดที่'+ordinal+prefix+' 45 นาที',[{'index':2,'duration_text':prefix+' 45 นาที'}])
    assert r.status=='preview' and [d['draft_ref'] for d in children(h)]==refs,r
    assert [d['end'] for d in children(h)]==['2026-10-22T10:00:00+07:00','2026-10-23T14:45:00+07:00']


def test_another_appointment_cue_carries_the_one_common_literal_name():
    h=Harness();r=prepare(h,'ซ้อมดนตรีวันที่ ๒๒ ต.ค. ๐๙:๐๐ ถึง ๑๐:๐๐ น. แล้วเพิ่มอีกนัดวันที่ ๒๓ ต.ค. ๑๔:๐๐ ถึง ๑๕:๐๐ น.!',[
        {'title':'ซ้อมดนตรี','date_text':'วันที่ ๒๒ ต.ค.','time_text':'๐๙:๐๐','end_time_text':'๑๐:๐๐ น.'},
        {'title':'ซ้อมดนตรี','date_text':'วันที่ ๒๓ ต.ค.','time_text':'๑๔:๐๐','end_time_text':'๑๕:๐๐ น.'}])
    assert r.status=='preview' and [d['start'] for d in children(h)]==['2026-10-22T09:00:00+07:00','2026-10-23T14:00:00+07:00'],r


def test_time_answer_cannot_erase_unrepresented_original_event_text():
    h=Harness();r=prepare(h,'เพิ่มนัด 22 Oct อบรมภาพดาว พิเศษ',[{'title':'อบรมภาพดาว','date_text':'22 Oct'}])
    assert r.status=='clarification'
    r=prepare(h,'เวลา 8:00-17:00',[{'index':1,'time_text':'8:00','end_time_text':'17:00'}])
    assert r.status=='clarification' and 'calendar_unrepresented_event_text' in children(h)[0]['issues'] and h.provider.posts==0
    r=prepare(h,'ชื่อเต็ม อบรมภาพดาว พิเศษ',[{'index':1,'title':'อบรมภาพดาว พิเศษ'}])
    assert r.status=='preview' and children(h)[0]['facts']['title']=='อบรมภาพดาว พิเศษ'


def test_spaced_thai_afternoon_clock_is_not_unexplained_event_content():
    h=Harness();r=prepare(h,'เพิ่มคุยโครงการฟ้า วันที่ ๒๒ ต.ค. ๐๙:๐๐-๑๐:๐๐ น. กับคุยโครงการเขียว วันที่ ๒๓ ต.ค. เริ่มบ่าย ๒ โมงด้วย',[
        {'title':'คุยโครงการฟ้า','date_text':'วันที่ ๒๒ ต.ค.','time_text':'๐๙:๐๐','end_time_text':'๑๐:๐๐'},
        {'title':'คุยโครงการเขียว','date_text':'วันที่ ๒๓ ต.ค.','time_text':'บ่าย ๒ โมง'}])
    assert r.status=='clarification' and children(h)[1]['missing_fields']==['end'] and not children(h)[1]['issues'],r
    r=prepare(h,'ของคุยโครงการเขียวกำหนดไว้ ๔๕ นาที!',[{'index':2,'duration_text':'๔๕ นาที'}])
    assert r.status=='preview' and children(h)[1]['end']=='2026-10-23T14:45:00+07:00',r


def test_natural_prepare_first_request_retains_two_members_through_time_answer():
    h=Harness();r=prepare(h,'ช่วยใส่อบรมภาพดาววันที่ 22 ตุลาคม กับเวิร์กช็อปหุ่นยนต์วันที่ 23 ตุลาคมไว้ก่อนนะ',[
        {'title':'อบรมภาพดาว','date_text':'วันที่ 22 ตุลาคม'},{'title':'เวิร์กช็อปหุ่นยนต์','date_text':'วันที่ 23 ตุลาคม'}])
    assert r.status=='clarification' and len(children(h))==2
    r=prepare(h,'ค่อยเติมเวลาให้ทั้งคู่เป็น 08:00 ถึง 17:00 น.',[{'index':1,'time_text':'08:00','end_time_text':'17:00'}])
    assert r.status=='preview' and [d['start'] for d in children(h)]==['2026-10-22T08:00:00+07:00','2026-10-23T08:00:00+07:00'],r


def test_another_occurrence_without_an_explicit_initial_round_marker():
    h=Harness();r=prepare(h,'ช่วยเพิ่มซ้อมดนตรีวันที่ 22 ตุลาคม เวลา 09:00 ถึง 10:00 แล้วอีกครั้งวันที่ 23 ตุลาคม เวลา 14:00 ถึง 15:00',[
        {'title':'ซ้อมดนตรี','date_text':'วันที่ 22 ตุลาคม','time_text':'09:00','end_time_text':'10:00'},
        {'title':'ซ้อมดนตรี','date_text':'วันที่ 23 ตุลาคม','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and len(children(h))==2,r


def test_natural_schedule_request_is_not_an_unrepresented_event():
    h=Harness();r=prepare(h,'พรุ่งนี้มีตรวจตา 09:00 ถึง 10:00 น. ส่วนซ้อมไวโอลินเป็น 14:00 ถึง 15:00 น. ช่วยจัดตารางให้หน่อย',[
        {'title':'ตรวจตา','date_text':'พรุ่งนี้','time_text':'09:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview',r


def test_write_down_request_and_shared_time_answer_preserve_every_event():
    h=Harness();r=prepare(h,'จดอบรมภาพดาว ๒๒ ต.ค. และเวิร์กช็อปหุ่นยนต์ ๒๓ ต.ค. ไว้ก่อน!',[
        {'title':'อบรมภาพดาว','date_text':'๒๒ ต.ค.'},{'title':'เวิร์กช็อปหุ่นยนต์','date_text':'๒๓ ต.ค.'}])
    assert r.status=='clarification' and len(children(h))==2
    r=prepare(h,'สองรายการใช้ช่วง ๐๘:๐๐–๑๗:๐๐ น. เหมือนกัน',[{'index':1,'time_text':'๐๘:๐๐','end_time_text':'๑๗:๐๐'}])
    assert r.status=='preview',r


def test_slash_separated_events_share_only_the_leading_date():
    h=Harness();r=prepare(h,'ลงพรุ่งนี้ด้วยนะ: ตรวจตา ๐๙:๐๐–๑๐:๐๐ น. / ซ้อมไวโอลิน ๑๔:๐๐–๑๕:๐๐ น.!',[
        {'title':'ตรวจตา','date_text':'พรุ่งนี้','time_text':'๐๙:๐๐','end_time_text':'๑๐:๐๐'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'๑๔:๐๐','end_time_text':'๑๕:๐๐'}])
    assert r.status=='preview' and [d['start'] for d in children(h)]==['2026-10-17T09:00:00+07:00','2026-10-17T14:00:00+07:00'],r


def test_spaced_date_slash_is_protected_from_event_separator():
    h=Harness();r=prepare(h,'ตรวจตา 25 / 10 9:00-10:00 / ซ้อมไวโอลิน 26 / 10 14:00-15:00',[
        {'title':'ตรวจตา','date_text':'25 / 10','time_text':'9:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'26 / 10','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and len(children(h))==2,r
    assert [d['start'] for d in children(h)]==['2026-10-25T09:00:00+07:00','2026-10-26T14:00:00+07:00']


def test_then_named_event_and_write_down_verb_preserve_missing_duration():
    h=Harness();r=prepare(h,'จดคุยโครงการฟ้า ๒๒ ต.ค. ๐๙:๐๐–๑๐:๐๐ น. แล้วคุยโครงการเขียว ๒๓ ต.ค. เริ่ม ๑๔:๐๐ น.!',[
        {'title':'คุยโครงการฟ้า','date_text':'๒๒ ต.ค.','time_text':'๐๙:๐๐','end_time_text':'๑๐:๐๐'},
        {'title':'คุยโครงการเขียว','date_text':'๒๓ ต.ค.','time_text':'๑๔:๐๐'}])
    assert r.status=='clarification' and len(children(h))==2
    r=prepare(h,'รายการคุยโครงการเขียวกินเวลา ๔๕ นาที',[{'index':2,'duration_text':'๔๕ นาที'}])
    assert r.status=='preview',r


def test_round_date_marker_is_not_an_unrepresented_title_modifier():
    h=Harness();r=prepare(h,'ซ้อมดนตรีรอบวันที่ ๒๒ ต.ค. ๐๙:๐๐–๑๐:๐๐ น. และอีกรอบ ๒๓ ต.ค. ๑๔:๐๐–๑๕:๐๐ น.!',[
        {'title':'ซ้อมดนตรีรอบ','date_text':'วันที่ ๒๒ ต.ค.','time_text':'๐๙:๐๐','end_time_text':'๑๐:๐๐'},
        {'title':'ซ้อมดนตรี','date_text':'๒๓ ต.ค.','time_text':'๑๔:๐๐','end_time_text':'๑๕:๐๐'}])
    assert r.status=='preview' and [d['facts']['title'] for d in children(h)]==['ซ้อมดนตรี','ซ้อมดนตรี'],r


def test_bangkok_zone_slash_never_creates_an_extra_member():
    h=Harness();r=prepare(h,'นัดตรวจตา 22 Oct 9:00-10:00 Asia/Bangkok และซ้อมไวโอลิน 23 Oct 14:00-15:00 Asia/Bangkok',[
        {'title':'ตรวจตา','date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'23 Oct','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and len(children(h))==2,r


def test_slash_inside_literal_title_is_not_a_separator():
    h=Harness();r=prepare(h,'นัด "อ่านบทที่ 2 / 3" 22 Oct 9:00-10:00',[{'title':'อ่านบทที่ 2 / 3','date_text':'22 Oct','time_text':'9:00','end_time_text':'10:00'}])
    assert r.status=='preview' and len(children(h))==1 and children(h)[0]['facts']['title']=='อ่านบทที่ 2 / 3',r


def test_explicitly_omitted_times_are_retained_then_completed_by_shared_answer():
    h=Harness();r=prepare(h,'ช่วยจดอบรมภาพดาววันที่ 22 ตุลาคม กับเวิร์กช็อปหุ่นยนต์วันที่ 23 ตุลาคมไว้ให้หน่อย ยังไม่ต้องระบุเวลา',[
        {'title':'อบรมภาพดาว','date_text':'วันที่ 22 ตุลาคม'},{'title':'เวิร์กช็อปหุ่นยนต์','date_text':'วันที่ 23 ตุลาคม'}])
    assert r.status=='clarification' and len(children(h))==2
    r=prepare(h,'ขอแก้เวลาให้ทั้งสองรายการเป็น 08:00–17:00 น.',[{'index':1,'time_text':'08:00','end_time_text':'17:00'}])
    assert r.status=='preview',r


def test_polite_schedule_request_before_shared_date_does_not_drop_any_member():
    h=Harness();r=prepare(h,'ช่วยจัดนัดพรุ่งนี้ให้หน่อย มีตรวจตา 09:00–10:00 น. และซ้อมไวโอลิน 14:00–15:00 น.',[
        {'title':'ตรวจตา','date_text':'พรุ่งนี้','time_text':'09:00','end_time_text':'10:00'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and len(children(h))==2,r


@pytest.mark.parametrize('whole_range',[True,False])
def test_literal_period_prefix_keeps_start_end_and_missing_second_end(whole_range):
    h=Harness();r=prepare(h,'ลงคุยโครงการฟ้าวันที่ 22 ตุลาคม ช่วง 09:00–10:00 น. ส่วนคุยโครงการเขียววันที่ 23 ตุลาคม เริ่ม 14:00 น.',[
        {'title':'คุยโครงการฟ้า','date_text':'วันที่ 22 ตุลาคม','time_text':'ช่วง 09:00–10:00 น.' if whole_range else 'ช่วง 09:00','end_time_text':None if whole_range else '10:00'},
        {'title':'คุยโครงการเขียว','date_text':'วันที่ 23 ตุลาคม','time_text':'14:00'}])
    assert r.status=='clarification' and children(h)[0]['end']=='2026-10-22T10:00:00+07:00',r
    r=prepare(h,'คุยโครงการเขียวใช้เวลา 45 นาที',[{'index':2,'duration_text':'45 นาที'}]);assert r.status=='preview',r


def test_another_day_marker_stays_outside_the_common_event_name():
    h=Harness();r=prepare(h,'ช่วยลงซ้อมดนตรีวันที่ 22 ตุลาคม 09:00 ถึง 10:00 น. แล้วลงซ้อมดนตรีอีกวันวันที่ 23 ตุลาคม 14:00 ถึง 15:00 น.',[
        {'title':'ซ้อมดนตรี','date_text':'วันที่ 22 ตุลาคม','time_text':'09:00','end_time_text':'10:00'},
        {'title':'ซ้อมดนตรีอีกวัน','date_text':'วันที่ 23 ตุลาคม','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and [d['facts']['title'] for d in children(h)]==['ซ้อมดนตรี','ซ้อมดนตรี'],r

@pytest.mark.parametrize('omission',['ยังไม่ใส่เวลา','ยังไม่ต้องระบุเวลา','เดี๋ยวค่อยบอกเวลา','ค่อยเติมเวลา','ยังไม่ทราบเวลา','ยังไม่ต้องใส่ช่วงเวลา','ยังไม่ทราบช่วงเวลา'])
def test_declared_missing_time_is_metadata_until_owner_supplies_both_clocks(omission):
    h=Harness();r=prepare(h,'ขอเพิ่มอบรมภาพดาว ๒๒ ต.ค. และเวิร์กช็อปหุ่นยนต์ ๒๓ ต.ค. ลงตารางก่อน '+omission+'!',[
        {'title':'อบรมภาพดาว','date_text':'๒๒ ต.ค.'},{'title':'เวิร์กช็อปหุ่นยนต์','date_text':'๒๓ ต.ค.'}])
    assert r.status=='clarification' and len(children(h))==2 and 'confirm_command' not in r.data
    r=prepare(h,'ใส่เวลาให้ทั้งคู่ ๐๘:๐๐-๑๗:๐๐ น. ด้วยนะ',[{'index':1,'time_text':'๐๘:๐๐','end_time_text':'๑๗:๐๐'}])
    assert r.status=='preview' and [d['end'] for d in children(h)]==['2026-10-22T17:00:00+07:00','2026-10-23T17:00:00+07:00'],r
    assert h.provider.posts==0


def test_reserve_time_request_supports_shared_day_and_thai_word_clocks():
    h=Harness();r=prepare(h,'ช่วยกันเวลาในตารางพรุ่งนี้ให้หน่อย ตรวจตาเก้าโมงถึงสิบโมง แล้วซ้อมไวโอลินบ่ายสองถึงบ่ายสาม',[
        {'title':'ตรวจตา','date_text':'พรุ่งนี้','time_text':'เก้าโมง','end_time_text':'สิบโมง'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'บ่ายสอง','end_time_text':'บ่ายสาม'}])
    assert r.status=='preview' and [d['start'] for d in children(h)]==['2026-10-17T09:00:00+07:00','2026-10-17T14:00:00+07:00'],r


def test_another_appointment_suffix_is_separate_from_the_common_name():
    h=Harness();r=prepare(h,'ในวันที่ 22 ตุลาคมมีซ้อมดนตรี 09:00–10:00 น. แล้วก็ขอซ้อมดนตรีอีกนัดวันที่ 23 ตุลาคม 14:00–15:00 น.',[
        {'title':'มีซ้อมดนตรี','date_text':'วันที่ 22 ตุลาคม','time_text':'09:00','end_time_text':'10:00'},
        {'title':'ซ้อมดนตรีอีกนัด','date_text':'วันที่ 23 ตุลาคม','time_text':'14:00','end_time_text':'15:00'}])
    assert r.status=='preview' and [d['facts']['title'] for d in children(h)]==['ซ้อมดนตรี','ซ้อมดนตรี'],r


@pytest.mark.parametrize('answer',['ทั้งสองรายการเริ่ม ๐๘:๐๐ และจบ ๑๗:๐๐ น.!','ทั้งคู่ตั้งแต่เวลา 08:00 และสิ้นสุดเวลา 17:00'])
def test_explicit_separate_start_end_labels_update_every_pending_member(answer):
    h=Harness();two(h);refs=[d['draft_ref'] for d in children(h)]
    r=prepare(h,answer,[{'index':1,'time_text':'๐๘:๐๐' if '๐๘' in answer else '08:00','end_time_text':'๑๗:๐๐' if '๑๗' in answer else '17:00'}])
    assert r.status=='preview' and [d['draft_ref'] for d in children(h)]==refs,r
    assert [d['start'] for d in children(h)]==['2026-10-22T08:00:00+07:00','2026-10-23T08:00:00+07:00']
    assert [d['end'] for d in children(h)]==['2026-10-22T17:00:00+07:00','2026-10-23T17:00:00+07:00']
    assert h.provider.posts==0

@pytest.mark.parametrize('answer',['ทั้งสองรายการ 08:00 และ 17:00','ทั้งสองรายการเริ่ม 08:00 และคุยอีกเรื่องจบ 17:00','ทั้งสามรายการเริ่ม 08:00 และจบ 17:00'])
def test_unlabelled_or_intervening_topic_or_wrong_group_does_not_share_clocks(answer):
    h=Harness();two(h)
    r=prepare(h,answer,[{'index':1,'time_text':'08:00','end_time_text':'17:00'}])
    assert r.status=='clarification' and 'confirm_command' not in r.data and h.provider.posts==0


def test_put_in_schedule_request_keeps_shared_tomorrow_and_both_word_clock_ranges():
    h=Harness();r=prepare(h,'ช่วยใส่ตารางพรุ่งนี้ให้ที ตรวจตาเก้าโมงถึงสิบโมง แล้วซ้อมไวโอลินบ่ายสองถึงบ่ายสาม',[
        {'title':'ตรวจตา','date_text':'พรุ่งนี้','time_text':'เก้าโมง','end_time_text':'สิบโมง'},
        {'title':'ซ้อมไวโอลิน','date_text':'พรุ่งนี้','time_text':'บ่ายสอง','end_time_text':'บ่ายสาม'}])
    assert r.status=='preview' and [d['end'] for d in children(h)]==['2026-10-17T10:00:00+07:00','2026-10-17T15:00:00+07:00'],r
    assert not h.provider.posts

@pytest.mark.parametrize('literal',['45 นาทีพอดี','๔๕ นาทีพอดี','ใช้เวลา 45 นาทีพอดี'])
def test_exact_duration_suffix_completes_only_the_named_pending_member(literal):
    h=Harness();prepare(h,'คุยโครงการฟ้า 22 Oct 09:00-10:00 และคุยโครงการเขียว 23 Oct เริ่ม 14:00',[
        {'title':'คุยโครงการฟ้า','date_text':'22 Oct','time_text':'09:00','end_time_text':'10:00'},
        {'title':'คุยโครงการเขียว','date_text':'23 Oct','time_text':'14:00'}])
    first=deepcopy(children(h)[0]);r=prepare(h,'คุยโครงการเขียว'+literal,[{'index':2,'duration_text':literal}])
    assert r.status=='preview' and children(h)[1]['end']=='2026-10-23T14:45:00+07:00',r
    assert children(h)[0]['version']==first['version'] and children(h)[0]['facts']==first['facts'] and h.provider.posts==0
