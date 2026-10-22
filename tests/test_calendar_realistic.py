"""Realistic, bounded Calendar matrix. Synthetic data only; not exhaustive."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta
import hashlib,json,socket
import httpx,pytest
from line_assistant.calendar_dates import event_date,event_time,duration
from line_assistant.calendar_provider import CalendarCreationApi,creation_body
from line_assistant.calendar_workflow import payload
from line_assistant.contracts import CreateEvent
from line_assistant.history_context import HistoryState,digest
from line_assistant.history_persistence import LocalHistoryState
from line_assistant.transport import RestClient,WriteBlocked,OutcomeUnknown,NotReady
from line_assistant.adapters import Conflict
from test_calendar_mvp import Harness,FakeCalendar,NOW,TARGET,complete,response,ProviderFailure

@pytest.fixture(autouse=True)
def deny_real_network(monkeypatch):
    def deny(*a,**k):raise AssertionError('Only fake Calendar and business integrations are allowed')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

@pytest.mark.parametrize('at,word,expected',[
    ('2026-12-31T16:50:00+00:00','พรุ่งนี้','2027-01-01'),
    ('2026-12-31T17:00:00+00:00','วันนี้','2027-01-01'),
    ('2026-01-31T10:00:00+00:00','พรุ่งนี้','2026-02-01'),
    ('2028-02-28T10:00:00+00:00','มะรืน','2028-03-01'),
    ('2028-02-28T10:00:00+00:00','พรุ่งนี้','2028-02-29'),
    ('2026-10-16T10:00:00+00:00','อีกสองวัน','2026-10-18'),
    ('2026-12-29T10:00:00+00:00','อีก 1 สัปดาห์','2027-01-05'),
    ('2026-10-16T10:00:00+00:00','วันจันทร์นี้','2026-10-19'),
    ('2026-10-18T10:00:00+00:00','เสาร์นี้','2026-10-24'),
    ('2026-10-16T10:00:00+00:00','ศุกร์นี้','2026-10-16'),
    ('2026-10-16T10:00:00+00:00','ศุกร์หน้า','2026-10-23'),
    ('2026-10-16T10:00:00+00:00','วันที่ 20 ต.ค. 2570','2027-10-20'),
    ('2026-10-16T10:00:00+00:00','20ตุลา2570','2027-10-20'),
    ('2026-10-16T10:00:00+00:00','20 ต.ค.70',None),
    ('2026-10-16T10:00:00+00:00','วันจันทร์ที่ 19 ตุลา 2569','2026-10-19'),
    ('2026-10-16T10:00:00+00:00','วันอังคารที่ 19 ตุลา 2569',None),
    ('2026-10-16T10:00:00+00:00','2028-02-29','2028-02-29'),
    ('2026-10-16T10:00:00+00:00','29/2/2569',None),
    ('2026-10-16T10:00:00+00:00','31/11/2026',None),
    ('2026-10-16T10:00:00+00:00','พรุ่งนี้หรือมะรืน',None),
    ('2026-10-16T10:00:00+00:00','สัปดาห์หน้า',None),
    ('2026-10-16T10:00:00+00:00','วันศุกร์',None),
    ('2026-10-16T10:00:00+00:00','เดือนหน้า',None),
    ('2026-10-16T10:00:00+00:00','1899-01-01',None),
    ('2026-10-16T10:00:00+00:00','2101-01-01',None)])
def test_calendar_dates_rollovers_and_honest_ambiguity(at,word,expected):
    if expected is None:
        with pytest.raises(ValueError):event_date(word,datetime.fromisoformat(at))
    else:assert event_date(word,datetime.fromisoformat(at)).isoformat()==expected

@pytest.mark.parametrize('month,aliases',[(1,['มกราคม','ม.ค.']),(2,['กุมภาพันธ์','ก.พ.']),(3,['มีนาคม','มี.ค.']),
    (4,['เมษายน','เม.ย.']),(5,['พฤษภาคม','พ.ค.']),(6,['มิถุนายน','มิ.ย.']),(7,['กรกฎาคม','ก.ค.']),
    (8,['สิงหาคม','ส.ค.']),(9,['กันยายน','ก.ย.']),(10,['ตุลาคม','ต.ค.']),(11,['พฤศจิกายน','พ.ย.']),(12,['ธันวาคม','ธ.ค.'])])
def test_thai_month_names_and_abbreviations_with_explicit_year(month,aliases):
    for alias in aliases:assert event_date('5 '+alias+' 2570',NOW).isoformat()==f'2027-{month:02d}-05'

@pytest.mark.parametrize('word,expected',[
    ('บ่ายโมง','13:00'),('บ่ายสองโมงครึ่ง','14:30'),('เก้าโมงครึ่ง','09:30'),('หกโมงเย็น','18:00'),
    ('สามทุ่มครึ่ง','21:30'),('เที่ยงครึ่ง','12:30'),('เที่ยงคืนครึ่ง','00:30'),('00:00','00:00'),
    ('23:59น.','23:59'),('14.30 น.','14:30'),('14 น.','14:00'),('บ่ายสองโมง15นาที','14:15'),
    ('เก้าโมงสิบห้า','09:15'),('2:15 pm','14:15'),('12:00am','00:00'),('12:00pm','12:00'),
    ('14:00 เวลาไทย','14:00'),('14:00 ICT','14:00'),('14:00 Asia/Bangkok','14:00'),('14:00+07:00','14:00')])
def test_common_clock_phrasing_and_explicit_bangkok(word,expected):assert event_time(word).strftime('%H:%M')==expected

@pytest.mark.parametrize('word',['สองโมง','บ่าย','เช้า','เย็น','ประมาณบ่ายสอง','14:60','24:00','-1:00','0pm','13pm',
    '10:00 UTC','10:00 JST','10:00+09:00','14:00 หรือ 15:00'])
def test_ambiguous_or_foreign_clock_requires_clarification(word):
    with pytest.raises(ValueError):event_time(word)

@pytest.mark.parametrize('word,expected',[('สิบห้านาที',15),('ยี่สิบนาที',20),('สามสิบนาที',30),('สี่สิบห้านาที',45),
    ('สองชั่วโมง',120),('สองชั่วโมงครึ่ง',150),('1 ชม.',60),('ครึ่งชม.',30),('1.5ชั่วโมง',90),
    ('2 ชั่วโมง 15 นาที',135),('๑๕ นาที',15),('10080 นาที',10080)])
def test_common_explicit_durations(word,expected):assert duration(word)==expected

@pytest.mark.parametrize('word',['0 นาที','-5 นาที','10081 นาที','30','สักพัก','ประมาณหนึ่งชั่วโมง','0.001ชั่วโมง','1.5นาที'])
def test_missing_unit_approximate_or_invalid_duration_is_not_assumed(word):
    with pytest.raises(ValueError):duration(word)

@pytest.mark.parametrize('title',['ตรวจสุขภาพที่โรงพยาบาล 2','ประชุมทีม A / ห้อง 2027','คุยเรื่องงาน ☕️',
    'คุยงาน <script> & ทีม','อ่านหนังสือชื่อ “หรือ”','งานชื่อ “ทุกวัน”','งานชื่อ “พรุ่งนี้”'])
def test_literal_names_and_locations_are_preserved_in_review_and_provider_title(title):
    h=Harness();r=h.prepare('นัด'+title+' พรุ่งนี้14:00 30นาที',title=title,date_text='พรุ่งนี้',time_text='14:00',duration_text='30นาที')
    assert r.status=='preview' and title in r.message and h.provider.posts==0
    assert h.send(r.data['confirm_command']).status=='created'
    value=next(iter(h.provider.events.values()));assert value['summary']==title and not h.sheets.entries

def test_date_inside_quoted_title_is_not_evidence_of_an_appointment_date():
    h=Harness();r=h.prepare('นัดคุยเรื่อง “พรุ่งนี้” 14:00 30นาที',title='คุยเรื่อง “พรุ่งนี้”',date_text='พรุ่งนี้',time_text='14:00',duration_text='30นาที')
    assert r.status=='clarification' and h.provider.posts==0 and not h.state.calendar_drafts

@pytest.mark.parametrize('zone',['UTC','JST','PST','Asia/Tokyo','+09:00','เวลาโตเกียว'])
def test_model_cannot_silently_drop_an_explicit_foreign_timezone(zone):
    h=Harness();r=h.prepare('นัดคุยงานพรุ่งนี้10:00 '+zone+' 30นาที',title='คุยงาน',date_text='พรุ่งนี้',time_text='10:00',duration_text='30นาที')
    assert r.status=='clarification' and not h.provider.posts and not h.state.calendar_drafts

@pytest.mark.parametrize('text',['นัดคุยงานพรุ่งนี้14:00 30นาที ทั้งวัน','นัดคุยงานพรุ่งนี้14:00 30นาที ทุกสัปดาห์',
    'นัดคุยงานพรุ่งนี้14:00 30นาที recurrence weekly','นัดคุยงานพรุ่งนี้14:00 30นาที เชิญคุณสมชาย',
    'นัดคุยงานพรุ่งนี้14:00 30นาที ส่งคำเชิญ a@example.invalid','นัดคุยงานพรุ่งนี้14:00 30นาที เตือนก่อน 15นาที'])
def test_unsupported_event_options_never_produce_a_partial_write(text):
    h=Harness();r=h.prepare(text,title='คุยงาน',date_text='พรุ่งนี้',time_text='14:00',duration_text='30นาที')
    assert r.status=='clarification' and h.provider.posts==0
    if 'ทั้งวัน' in text:assert all(d['status']=='needs_information' and not d['review'] for d in h.state.calendar_drafts.values())
    else:assert not h.state.calendar_drafts

@pytest.mark.parametrize('at,date_word,start,minutes,end',[
    ('2026-12-31T10:00:00+00:00','วันนี้','23:50','20นาที','2027-01-01T00:10:00+07:00'),
    ('2026-01-31T10:00:00+00:00','วันนี้','23:45','30นาที','2026-02-01T00:15:00+07:00'),
    ('2028-02-28T10:00:00+00:00','วันนี้','23:45','30นาที','2028-02-29T00:15:00+07:00')])
def test_explicit_duration_crosses_month_year_and_leap_midnight(at,date_word,start,minutes,end):
    h=Harness();h.now=datetime.fromisoformat(at)
    r=h.prepare('นัดอ่านหนังสือ'+date_word+' '+start+' '+minutes,title='อ่านหนังสือ',date_text=date_word,time_text=start,duration_text=minutes)
    assert r.status=='preview' and h.draft()['end']==end and h.provider.posts==0

def test_missing_facts_are_retained_across_three_natural_answers():
    h=Harness();h.prepare('เพิ่มนัดตรวจฟัน',title='ตรวจฟัน');a=deepcopy(h.draft())
    r=h.prepare('พรุ่งนี้บ่ายสอง',date_text='พรุ่งนี้',time_text='บ่ายสอง')
    assert r.status=='clarification' and h.draft()['facts']['title']==a['facts']['title'] and h.draft()['missing_fields']==['end']
    r=h.prepare('ครึ่งชั่วโมง',duration_text='ครึ่งชั่วโมง');assert r.status=='preview' and h.draft()['end'].endswith('14:30:00+07:00')
    assert h.provider.posts==0 and h.send('ยืนยันครับ').status=='created'

@pytest.mark.parametrize('change,fields,expected_end',[
    ('เปลี่ยนเป็นหนึ่งชั่วโมง',{'duration_text':'หนึ่งชั่วโมง'},'15:00:00+07:00'),
    ('สิ้นสุด15:30',{'end_time_text':'15:30'},'15:30:00+07:00'),
    ('เริ่ม15:00',{'time_text':'15:00'},'15:45:00+07:00')])
def test_correcting_one_time_fact_invalidates_previous_review(change,fields,expected_end):
    h=Harness();old=complete(h);before=deepcopy(h.draft());new=h.prepare(change,**fields)
    assert new.status=='preview' and h.draft()['facts']['title']==before['facts']['title'] and h.draft()['end'].endswith(expected_end)
    assert h.send(old.data['confirm_command']).status=='stale' and h.provider.posts==0
    assert h.send(new.data['confirm_command']).status=='created' and h.provider.posts==1

@pytest.mark.parametrize('operation,text,expected',[('cancel','ไม่เอานัดนี้แล้ว','cancelled'),('cancel','อย่ายกเลิกนัด','clarification'),('review','ตรวจร่างนัด','preview')])
def test_control_tools_require_literal_requested_action(operation,text,expected):
    h=Harness();complete(h)
    def control(body):
        d=json.loads(body['input'][0]['content'])['calendar_drafts'][0]
        return response('control_calendar_draft',{'draft_ref':d['draft_ref'],'draft_version':d['version'],'operation':operation})
    assert h.send(text,control).status==expected and h.provider.posts==0

@pytest.mark.parametrize('nonce',['0'*16,'f'*16])
def test_forged_review_nonce_never_creates(nonce):
    h=Harness();complete(h);d=h.draft()
    assert h.send('ยืนยันนัด '+d['draft_ref']+' '+nonce).status=='stale' and h.provider.posts==0

def test_new_webhook_repeating_a_saved_confirmation_does_not_create_a_new_action():
    h=Harness();r=complete(h);assert h.send(r.data['confirm_command']).status=='created'
    assert h.send(r.data['confirm_command']).status=='stale' and h.provider.posts==1

@pytest.mark.parametrize('same_event',[True,False])
def test_two_sqlite_workers_serialize_simultaneous_confirmations(tmp_path,same_event):
    path=tmp_path/'fake.sqlite';h=Harness(state=LocalHistoryState(path,'mock-owner',clock=lambda:NOW));r=complete(h)
    one=h.event(r.data['confirm_command']);two=deepcopy(one) if same_event else h.event(r.data['confirm_command'])
    fresh=LocalHistoryState(path,'mock-owner',clock=lambda:h.now);other=Harness(state=fresh,provider=h.provider);other.now=h.now
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(h.app.handle,one);b=pool.submit(other.app.handle,two);results=[a.result(),b.result()]
    assert h.provider.posts==1 and len(h.provider.events)==1
    assert [v.status for v in results].count('created')==(2 if same_event else 1)
    assert len(h.model.requests)==1 and not other.model.requests

@pytest.mark.parametrize('phase,expected,posts',[('prepared','created',1),('executing','uncertain',0),('effect_returned','created',1),('action_completed','created',1)])
def test_cold_recovery_at_durable_action_boundaries(tmp_path,phase,expected,posts):
    class Crash(RuntimeError):pass
    path=tmp_path/'fake.sqlite';h=Harness(state=LocalHistoryState(path,'mock-owner',clock=lambda:NOW));r=complete(h)
    def crash(point,key):
        if point==phase:raise Crash('Synthetic worker crash')
    h.app.phase_hook=crash;event=h.event(r.data['confirm_command'])
    with pytest.raises(Crash):h.app.handle(event)
    fresh=LocalHistoryState(path,'mock-owner',clock=lambda:h.now);other=Harness(state=fresh,provider=h.provider);other.now=h.now
    assert other.app.handle(deepcopy(event)).status==expected and h.provider.posts==posts and not other.model.requests

def args():return CreateEvent(title='ทดสอบ provider',start=NOW+timedelta(days=1),end=NOW+timedelta(days=1,minutes=30),reminder_minutes=None)

@pytest.mark.parametrize('match',[True,False])
def test_provider_409_requires_positive_exact_same_id_payload(match):
    calls=[];a=args();key='fake-owner:calendar:collision';body=creation_body(a,key,TARGET)
    def serve(request):
        calls.append(request.method)
        if request.method=='POST':return httpx.Response(409,json={})
        return httpx.Response(200,json={**body,'summary':a.title if match else 'different','etag':'"fake"'})
    adapter=CalendarCreationApi(RestClient('calendar',httpx.MockTransport(serve)),TARGET,writes_enabled=True)
    if match:assert adapter.create(a,key).id==body['id']
    else:
        with pytest.raises(Conflict):adapter.create(a,key)
    assert calls==['POST','GET']

@pytest.mark.parametrize('field,value',[('etag',None),('summary','wrong'),('attendees',[{'email':'fake@example.invalid'}]),
    ('recurrence',['RRULE:FREQ=DAILY']),('reminders',{'useDefault':True}),('status','cancelled'),('extendedProperties',{})])
def test_unproven_or_changed_provider_acknowledgement_is_uncertain(field,value):
    a=args();key='fake-key';body={**creation_body(a,key,TARGET),'etag':'"fake"',field:value}
    adapter=CalendarCreationApi(RestClient('calendar',httpx.MockTransport(lambda r:httpx.Response(200,json=body))),TARGET,writes_enabled=True)
    with pytest.raises(OutcomeUnknown):adapter.create(a,key)

@pytest.mark.parametrize('status',[401,403])
def test_definitive_create_access_denial_is_not_reported_as_saved(status):
    adapter=CalendarCreationApi(RestClient('calendar',httpx.MockTransport(lambda r:httpx.Response(status,json={}))),TARGET,writes_enabled=True)
    with pytest.raises(WriteBlocked):adapter.create(args(),'fake-key')

@pytest.mark.parametrize('status',[404,410])
def test_absent_reconciliation_never_proves_no_effect(status):
    adapter=CalendarCreationApi(RestClient('calendar',httpx.MockTransport(lambda r:httpx.Response(status,json={}))),TARGET,writes_enabled=True)
    assert adapter.reconcile('fake-key',args().model_dump(mode='json')).outcome=='unknown'

def test_incomplete_conflict_pagination_never_authorizes_review():
    calls=[]
    def serve(request):calls.append(request.method);return httpx.Response(200,json={'items':[],'nextPageToken':'same'})
    adapter=CalendarCreationApi(RestClient('calendar',httpx.MockTransport(serve)),TARGET,writes_enabled=True)
    with pytest.raises(NotReady):adapter.conflicts(args().start,args().end)
    assert 1<len(calls)<=3 and set(calls)=={'GET'}

def test_free_and_cancelled_existing_events_are_not_busy_conflicts():
    a=args();busy={'id':'free','start':{'dateTime':a.start.isoformat()},'end':{'dateTime':a.end.isoformat()},'transparency':'transparent'}
    values=[busy,{'id':'cancelled','status':'cancelled'}]
    adapter=CalendarCreationApi(RestClient('calendar',httpx.MockTransport(lambda r:httpx.Response(200,json={'items':values}))),TARGET,writes_enabled=True)
    assert adapter.conflicts(a.start,a.end)==[]

@pytest.mark.parametrize('prefix',['อย่าสร้างนัด','ไม่ต้องนัด','ยังไม่ต้องสร้างนัด','สมมติ นัด','ถ้ามีนัด'])
def test_planner_can_answer_negated_and_hypothetical_requests_without_preparation(prefix):
    h=Harness();h.send(prefix+'ตรวจฟันพรุ่งนี้14:00 30นาที',response(text='ยังไม่เตรียมนัดครับ'))
    assert len(h.model.requests)==1 and not h.state.calendar_drafts and h.provider.posts==0

@pytest.mark.parametrize('word',['ประมาณ','ราวๆ','อาจจะ','ยังไม่แน่ใจ'])
def test_model_cannot_drop_owner_uncertainty_and_create_an_exact_time(word):
    h=Harness();r=h.prepare('นัดตรวจฟันพรุ่งนี้'+word+'14:00 30นาที',title='ตรวจฟัน',date_text='พรุ่งนี้',time_text='14:00',duration_text='30นาที')
    assert r.status=='clarification' and not h.state.calendar_drafts and h.provider.posts==0

def test_invalid_correction_revokes_old_review_without_losing_certified_facts():
    h=Harness();r=complete(h);before=deepcopy(h.draft())
    changed=h.prepare('แก้เป็นประมาณ15:00',time_text='15:00')
    assert changed.status=='clarification' and h.draft()['facts']==before['facts'] and h.draft()['review'] is None
    assert h.send(r.data['confirm_command']).status=='stale' and h.provider.posts==0

@pytest.mark.parametrize('field,value',[('location','ห้อง A'),('timeZone','Asia/Tokyo'),('description','unrequested'),('attendees',[]),('recurrence',[]),('reminders',{})])
def test_model_cannot_add_unimplemented_fields_or_destinations(field,value):
    h=Harness()
    def invalid(body):
        c=json.loads(body['input'][0]['content'])
        return response('prepare_calendar_event',{'draft_ref':None,'draft_version':None,'title':'ตรวจฟัน','date_text':'พรุ่งนี้','time_text':'14:00',
            'duration_text':'30นาที','end_date_text':None,'end_time_text':None,'source_refs':[c['current_source_ref']],field:value})
    assert h.send('นัดตรวจฟันพรุ่งนี้14:00 30นาที',invalid).status=='clarification' and h.provider.posts==0

@pytest.mark.parametrize('size',[1,200,201])
def test_full_title_boundary_is_explicit_instead_of_silent_truncation(size):
    title='ก'*size;h=Harness();r=h.prepare('นัด'+title+' พรุ่งนี้14:00 30นาที',title=title,date_text='พรุ่งนี้',time_text='14:00',duration_text='30นาที')
    if size<=200:assert r.status=='preview' and h.draft()['facts']['title']==title
    else:assert r.status=='clarification' and not h.state.calendar_drafts
    assert h.provider.posts==0

@pytest.mark.parametrize('order',['calendar_first','expense_first'])
def test_switching_domains_explicit_expense_confirmation_preserves_calendar(order):
    h=Harness()
    def expense(body):
        c=json.loads(body['input'][0]['content'])
        return response('prepare_expenses',{'draft_ref':None,'draft_version':None,'entries':[{'entry_ref':None,'item':'กาแฟ','item_text':None,
            'amount':23,'date_text':None,'date_ref':None,'note':None,'source_refs':[c['current_source_ref']],'field_sources':None}]})
    if order=='calendar_first':calendar=complete(h);cost=h.send('กาแฟ23บาท',expense)
    else:cost=h.send('กาแฟ23บาท',expense);calendar=complete(h)
    original=deepcopy(h.draft())
    assert h.send('ยืนยัน').status=='clarification' and h.provider.posts==0 and not h.sheets.entries
    assert h.send(cost.data['confirm_command']).status=='recorded' and len(h.sheets.entries)==1 and h.draft()==original
    assert h.send(calendar.data['confirm_command']).status=='created' and h.provider.posts==1 and len(h.sheets.entries)==1

@pytest.mark.parametrize('status',[403,500,503])
def test_post_failure_is_never_claimed_saved_or_automatically_retried(status):
    class Fault(FakeCalendar):
        def request(self,request):
            if request.method=='POST':self.posts+=1;return httpx.Response(status,json={})
            return super().request(request)
    h=Harness(provider=Fault());r=complete(h);event=h.event(r.data['confirm_command'])
    if status==403:
        assert h.app.handle(event).status=='setup_required' and h.draft()['review'] is None
        assert h.app.handle(deepcopy(event)).status=='setup_required'
    else:
        with pytest.raises(ProviderFailure):h.app.handle(event)
        assert h.app.handle(deepcopy(event)).status=='uncertain'
    assert h.provider.posts==1 and not h.provider.events

def test_unknown_effect_retained_after_history_and_draft_expiry_is_reconciled_only():
    h=Harness();r=complete(h);h.provider.fail_after_create=True
    with pytest.raises(ProviderFailure):h.send(r.data['confirm_command'])
    h.now+=timedelta(days=2);h.provider.fail_after_create=False
    assert h.send('ตรวจผลนัด').status=='created' and h.provider.posts==1

def test_duplicate_webhook_id_with_changed_payload_never_rewrites_saved_event():
    h=Harness();r=complete(h);event=h.event(r.data['confirm_command']);assert h.app.handle(event).status=='created'
    forged=deepcopy(event);forged['message']['text']='ยกเลิกนัดล่าสุด'
    assert h.app.handle(forged).status=='conflict' and h.provider.posts==1 and len(h.provider.events)==1

@pytest.mark.parametrize('day,this_friday,next_friday',[(12,16,16),(13,16,16),(14,16,16),(15,16,16),(16,16,23),(17,23,23),(18,23,23)])
def test_weekday_this_and_next_from_each_day_of_the_week(day,this_friday,next_friday):
    at=datetime.fromisoformat(f'2026-10-{day:02d}T10:00:00+00:00')
    assert event_date('ศุกร์นี้',at).day==this_friday
    assert event_date('วันศุกร์หน้า',at).day==next_friday

@pytest.mark.parametrize('zone',['+0900','UTC+07:30','GMT-5','10:00Z'])
def test_additional_explicit_zone_forms_cannot_be_dropped(zone):
    h=Harness();r=h.prepare('นัดคุยงานพรุ่งนี้10:00 '+zone+' 30นาที',title='คุยงาน',date_text='พรุ่งนี้',time_text='10:00',duration_text='30นาที')
    assert r.status=='clarification' and h.provider.posts==0

@pytest.mark.parametrize('option',['every week','ผู้เข้าร่วม คุณเอ','all-day'])
def test_additional_unsupported_options_are_explained_without_partial_creation(option):
    h=Harness();r=h.prepare('นัดคุยงานพรุ่งนี้14:00 30นาที '+option,title='คุยงาน',date_text='พรุ่งนี้',time_text='14:00',duration_text='30นาที')
    assert r.status=='clarification' and h.provider.posts==0
    if option=='all-day':assert all(d['status']=='needs_information' and not d['review'] for d in h.state.calendar_drafts.values())
    else:assert not h.state.calendar_drafts

@pytest.mark.parametrize('owner,model,expected',[('30นาที','30 นาที','30นาที'),('30 นาที','30นาที','30 นาที'),('๓๐นาที','30 นาที','๓๐นาที')])
def test_harmless_model_spacing_keeps_the_original_owner_literal(owner,model,expected):
    h=Harness();r=h.prepare('นัดอ่านหนังสือพรุ่งนี้20:00 '+owner,title='อ่านหนังสือ',date_text='พรุ่งนี้',time_text='20:00',duration_text=model)
    assert r.status=='preview' and h.draft()['facts']['duration_text']==expected and h.draft()['end'].endswith('20:30:00+07:00') and h.provider.posts==0

def test_complete_single_restatement_ignores_unused_old_recurring_source_but_keeps_originals():
    h=Harness();first='นัดอ่านหนังสือทุกวันพรุ่งนี้20:00 30นาที'
    h.prepare(first,title='อ่านหนังสือทุกวัน',date_text='พรุ่งนี้',time_text='20:00',duration_text='30 นาที')
    assert not h.state.calendar_drafts
    old_ref=next(r['source_ref'] for r in h.state.history if r.get('text')==first)
    def restate(body):
        c=json.loads(body['input'][0]['content'])
        return response('prepare_calendar_event',{'draft_ref':None,'draft_version':None,'title':'อ่านหนังสือ','date_text':'พรุ่งนี้',
            'time_text':'20:00','duration_text':'30นาที','end_date_text':None,'end_time_text':None,'source_refs':[c['current_source_ref'],old_ref]})
    r=h.send('ขอครั้งเดียว นัดอ่านหนังสือพรุ่งนี้20:00 30นาที',restate)
    assert r.status=='preview' and h.provider.posts==0 and all(ref!=old_ref for refs in h.draft()['fact_sources'].values() for ref in refs)
    assert any(row.get('text')==first for row in h.state.history)
    assert h.send(r.data['confirm_command']).status=='created' and h.provider.posts==1

@pytest.mark.parametrize('owner_time,model_time,owner_duration,model_duration',[
    ('14:00','14:00','130นาที','30 นาที'),('14:00','14:00','3 0นาที','30นาที'),
    ('14:00','14:00','ยี่สิบนาที','สิบนาที'),('14:00','14:00','สองชั่วโมง','ชั่วโมง'),
    ('บ่ายสองโมงครึ่ง','บ่ายสอง','30นาที','30นาที'),('9:00pm','9:00','30นาที','30นาที')])
def test_shorter_numeric_or_clock_substrings_are_not_complete_owner_facts(owner_time,model_time,owner_duration,model_duration):
    h=Harness();r=h.prepare('นัดอ่านหนังสือพรุ่งนี้ '+owner_time+' '+owner_duration,title='อ่านหนังสือ',date_text='พรุ่งนี้',time_text=model_time,duration_text=model_duration)
    assert r.status=='clarification' and h.provider.posts==0 and not h.state.calendar_drafts

def test_extra_old_reference_cannot_hide_current_unsupported_request():
    h=Harness();h.prepare('นัดอ่านหนังสือพรุ่งนี้20:00 30นาที',title='อ่านหนังสือ',date_text='พรุ่งนี้',time_text='20:00',duration_text='30นาที')
    old=deepcopy(h.draft());r=h.prepare('เปลี่ยนเป็นทุกวัน')
    assert r.status=='clarification' and h.draft()['review'] is None and h.draft()['facts']==old['facts'] and h.provider.posts==0

@pytest.mark.parametrize('original,shortened',[('วันที่20ธันวา2570','วันที่20'),('20ตุลา2570','20ตุลา')])
def test_partial_date_cannot_drop_an_explicit_month_or_year(original,shortened):
    h=Harness();r=h.prepare('นัดอ่านหนังสือ'+original+' 14:00 30นาที',title='อ่านหนังสือ',date_text=shortened,time_text='14:00',duration_text='30นาที')
    assert r.status=='clarification' and not h.state.calendar_drafts and h.provider.posts==0
