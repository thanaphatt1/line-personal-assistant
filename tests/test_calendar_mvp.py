"""Offline Calendar MVP fixtures. All owners, appointments and providers are fake."""
from copy import deepcopy
from datetime import datetime,timedelta
import hashlib,json,socket
import httpx,pytest
from line_assistant.adapters import SheetLayout
from line_assistant.calendar_dates import event_date,event_time,duration
from line_assistant.calendar_provider import CalendarCreationApi
from line_assistant.config import Settings
from line_assistant.conversation_mocks import ConversationSheets
from line_assistant.history_context import HistoryState,source_ref
from line_assistant.history_first import HistoryFirstAssistant
from line_assistant.history_persistence import history_snapshot,restore_history,LocalHistoryState
from line_assistant.model_api import MODEL_ID
from line_assistant.transport import RestClient
from line_assistant.service import ProviderFailure

NOW=datetime.fromisoformat('2026-10-16T08:00:00+00:00')
TARGET='synthetic-calendar'

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*a,**k):raise AssertionError('Calendar tests cannot access real networks or credentials')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def response(name=None,args=None,text=None):
    output=[]
    if name:output.append({'type':'function_call','status':'completed','name':name,'call_id':'fixture_call','arguments':json.dumps(args,ensure_ascii=False)})
    if text:output.append({'type':'message','status':'completed','content':[{'type':'output_text','text':text}]})
    return {'model':MODEL_ID,'status':'completed','usage':{'input_tokens':1000,'output_tokens':100},'output':output}

class FakeCalendar:
    def __init__(self):self.events={};self.calls=[];self.posts=0;self.fail_after_create=False;self.fail_before_create=False;self.forbidden=False;self.hide_reads=False
    def request(self,request):
        assert request.url.host=='www.googleapis.com'
        assert request.url.path.startswith('/calendar/v3/calendars/'+TARGET+'/events')
        suffix=request.url.path.split('/events',1)[1].lstrip('/');self.calls.append((request.method,suffix))
        if self.forbidden:return httpx.Response(403,json={})
        if request.method=='GET':
            if not suffix:return httpx.Response(200,json={'items':list(deepcopy(self.events).values())})
            value=self.events.get(suffix) if not self.hide_reads else None
            return httpx.Response(200,json=deepcopy(value)) if value else httpx.Response(404,json={})
        assert request.method=='POST' and not suffix
        self.posts+=1
        if self.fail_before_create:raise httpx.ReadTimeout('Synthetic unknown before acknowledgement',request=request)
        body=json.loads(request.content)
        if body['id'] in self.events:return httpx.Response(409,json={})
        self.events[body['id']]={**body,'etag':'"fixture-1"'}
        if self.fail_after_create:raise httpx.ReadTimeout('Synthetic committed but lost acknowledgement',request=request)
        return httpx.Response(200,json=deepcopy(self.events[body['id']]))
    def adapter(self,state,target=TARGET):
        return CalendarCreationApi(RestClient('calendar',httpx.MockTransport(self.request)),target,writes_enabled=True,effect_guard=state.before_effect)

class FakeModel:
    mocked=True
    def __init__(self):self.requests=[];self.values=[]
    def request(self,method,path,*,body):
        assert (method,path)==('POST','/v1/responses');self.requests.append(deepcopy(body))
        value=self.values.pop(0)
        return value(body) if callable(value) else deepcopy(value)

class Harness:
    def __init__(self,*,state=None,provider=None,enabled=True):
        self.now=NOW;self.counter=0;self.state=state or HistoryState();self.provider=provider or FakeCalendar();self.model=FakeModel()
        self.sheets=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),[('กาแฟ','Food','')])
        self.app=HistoryFirstAssistant(Settings(owner_id='mock-owner'),self.sheets,port=self.model,state=self.state,clock=lambda:self.now,
            history_first_enabled=True,item_enrollment_enabled=True,semantic_enabled=True,raw_history_enabled=True,
            calendar_enabled=enabled,calendar_adapter=self.provider.adapter(self.state) if enabled else None,calendar_id=TARGET if enabled else None,calendar_label='ปฏิทินจำลอง')
    def event(self,text,*,postback=False):
        self.counter+=1;self.now+=timedelta(seconds=1)
        return {'webhookEventId':'cal-fixture-'+str(self.counter),'timestamp':int(self.now.timestamp()*1000),'source':{'type':'user','userId':'mock-owner'},
            'type':'postback' if postback else 'message',**({'postback':{'data':text}} if postback else {'message':{'id':str(self.counter),'type':'text','text':text}})}
    def send(self,text,*values):
        self.model.values=list(values);return self.app.handle(self.event(text))
    def draft(self):return next(d for d in self.state.calendar_drafts.values() if d['status'] not in {'recorded','cancelled'})
    def prepare(self,text,**facts):
        def call(body):
            c=json.loads(body['input'][0]['content']);old=c['calendar_drafts'][0] if c['calendar_drafts'] else None
            args={'draft_ref':old['draft_ref'] if old else None,'draft_version':old['version'] if old else None,
                **dict.fromkeys(['title','date_text','time_text','end_date_text','end_time_text','duration_text']),'source_refs':[c['current_source_ref']],**facts}
            return response('prepare_calendar_event',args)
        return self.send(text,call)

def complete(h,title='ตรวจฟัน'):
    return h.prepare(f'นัด{title}พรุ่งนี้บ่ายสอง 45 นาที',title=title,date_text='พรุ่งนี้',time_text='บ่ายสอง',duration_text='45 นาที')

@pytest.mark.parametrize('word,day',[('พรุ่งนี้','2026-10-17'),('มะรืน','2026-10-18'),('วันศุกร์หน้า','2026-10-23'),('ศุกร์หน้า','2026-10-23'),('วันจันทร์นี้','2026-10-19'),('วันที่ 20 ตุลา 2569','2026-10-20')])
def test_calendar_date_policy_supports_future_and_original_bangkok_anchor(word,day):assert event_date(word,NOW).isoformat()==day

@pytest.mark.parametrize('word,clock',[('บ่ายสอง','14:00'),('สองทุ่ม','20:00'),('ตีห้า','05:00'),('เก้าโมงเช้า','09:00'),('ห้าโมงเย็น','17:00'),('บ่ายสามโมงครึ่ง','15:30'),('เที่ยงคืน','00:00'),('๑๔:๓๐','14:30'),('2:15pm','14:15')])
def test_literal_thai_clock_forms(word,clock):assert event_time(word).strftime('%H:%M')==clock

@pytest.mark.parametrize('word,minutes',[('45 นาที',45),('ครึ่งชั่วโมง',30),('1ชั่วโมง30นาที',90),('ชั่วโมงครึ่ง',90),('หนึ่งชั่วโมง',60)])
def test_explicit_duration_forms(word,minutes):assert duration(word)==minutes

@pytest.mark.parametrize('word',['สองโมง','25:00','14:99','14'])
def test_ambiguous_or_invalid_time_cannot_be_assumed(word):
    with pytest.raises(ValueError):event_time(word)

def test_natural_partial_dialogue_asks_only_duration_and_confirm_replay_is_once():
    h=Harness();r=h.prepare('นัดตรวจฟันพรุ่งนี้บ่ายสอง',title='ตรวจฟัน',date_text='พรุ่งนี้',time_text='บ่ายสอง')
    assert r.status=='clarification' and r.data['calendar_view']['missing_fields']==['end'] and not h.provider.events
    old=deepcopy(h.draft());r=h.prepare('45 นาที',duration_text='45 นาที')
    assert r.status=='preview' and '2026-10-17 14:45' in r.message and not h.provider.events
    d=h.draft();assert d['facts']['title']==old['facts']['title'] and d['fact_sources']['date_text']==old['fact_sources']['date_text']
    event=h.event('ยืนยัน');calls=len(h.model.requests);saved=h.app.handle(event)
    assert saved.status=='created' and len(h.provider.events)==h.provider.posts==1 and len(h.model.requests)==calls
    assert h.app.handle(deepcopy(event))==saved and h.provider.posts==1 and len(h.model.requests)==calls
    value=next(iter(h.provider.events.values()));assert value['reminders']=={'useDefault':False,'overrides':[]} and 'attendees' not in value and 'recurrence' not in value
    assert not h.sheets.entries

def test_missing_date_and_time_are_not_today_all_day_or_one_hour_defaults():
    h=Harness();r=h.prepare('เพิ่มนัดตรวจฟัน',title='ตรวจฟัน')
    assert r.status=='clarification' and set(r.data['calendar_view']['missing_fields'])=={'date','time','end'} and h.draft()['review'] is None

def test_source_owner_and_arbitrary_calendar_fields_are_rejected():
    h=Harness()
    def injected(body):
        c=json.loads(body['input'][0]['content'])
        return response('prepare_calendar_event',{'draft_ref':None,'draft_version':None,'title':'ตรวจฟัน','date_text':'พรุ่งนี้','time_text':'บ่ายสอง','end_date_text':None,'end_time_text':None,'duration_text':'45 นาที','source_refs':[c['current_source_ref']],'calendar_id':'another-calendar'})
    r=h.send('นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',injected)
    assert r.status=='clarification' and not h.state.calendar_drafts and not h.provider.calls
    r=h.prepare('นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',title='เรื่องที่เจ้าของไม่ได้ระบุ',date_text='พรุ่งนี้',time_text='บ่ายสอง',duration_text='45 นาที')
    assert r.status=='clarification' and not h.state.calendar_drafts

def test_changed_time_invalidates_old_confirmation_and_preserves_other_facts():
    h=Harness();first=complete(h);old=deepcopy(h.draft());updated=h.prepare('แก้เป็นบ่ายสาม',time_text='บ่ายสาม')
    assert updated.status=='preview' and h.draft()['facts']['title']==old['facts']['title']
    assert h.send(first.data['confirm_command']).status=='stale' and not h.provider.events
    assert h.send(updated.data['confirm_command']).status=='created'

def test_read_and_other_topic_cannot_become_plain_confirmation():
    h=Harness();r=complete(h);before=deepcopy(h.draft())
    h.send('สวัสดี',response(text='ช่วยเรื่องรายจ่ายหรือนัดได้ครับ'))
    assert h.draft()==before and h.send('ยืนยัน').status=='stale' and not h.provider.events
    assert h.send(r.data['confirm_command']).status=='created'

def test_calendar_and_expense_pending_require_domain_selection():
    h=Harness();calendar=complete(h)
    def expense(body):
        c=json.loads(body['input'][0]['content'])
        return response('prepare_expenses',{'draft_ref':None,'draft_version':None,'entries':[{'entry_ref':None,'item':'กาแฟ','item_text':None,'amount':23,'date_text':None,'date_ref':None,'note':None,'source_refs':[c['current_source_ref']],'field_sources':None}]})
    r=h.send('กาแฟ 23 บาท',expense);assert r.status=='preview'
    assert h.send('ยืนยัน').status=='clarification' and not h.provider.events and not h.sheets.entries
    assert h.send(calendar.data['confirm_command']).status=='created' and not h.sheets.entries

@pytest.mark.parametrize('text',['นัดตรวจฟันทุกวันพรุ่งนี้บ่ายสอง 45 นาที','นัดตรวจฟันพรุ่งนี้ทั้งวัน','นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที เตือนก่อน 30 นาที','นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที ส่งคำเชิญ'])
def test_unsupported_scope_does_not_create_partial_appointment(text):
    h=Harness();r=h.prepare(text,title='ตรวจฟัน',date_text='พรุ่งนี้',time_text='บ่ายสอง',duration_text='45 นาที')
    assert r.status!='preview' and not h.provider.events and not h.state.calendar_drafts

def test_positive_readback_after_lost_acknowledgement_completes_without_second_post():
    h=Harness();r=complete(h);h.provider.fail_after_create=True;event=h.event(r.data['confirm_command'])
    with pytest.raises(ProviderFailure):h.app.handle(event)
    assert h.provider.posts==1 and len(h.provider.events)==1 and any(a.status=='uncertain' for a in h.state.actions.values())
    h.provider.fail_after_create=False;again=h.app.handle(deepcopy(event))
    assert again.status=='created' and h.provider.posts==1 and len(h.provider.events)==1

def test_absent_event_after_unknown_attempt_is_not_permission_to_retry_or_cancel():
    h=Harness();r=complete(h);h.provider.fail_before_create=True;event=h.event(r.data['confirm_command'])
    with pytest.raises(ProviderFailure):h.app.handle(event)
    assert h.provider.posts==1 and any(a.status=='uncertain' for a in h.state.actions.values())
    h.provider.fail_before_create=False
    assert h.app.handle(deepcopy(event)).status=='uncertain' and h.provider.posts==1

def test_conflict_changes_require_a_new_review_and_all_day_busy_is_included():
    h=Harness();r=complete(h)
    h.provider.events['another']={'id':'another','start':{'date':'2026-10-17'},'end':{'date':'2026-10-18'},'etag':'"1"'}
    changed=h.send(r.data['confirm_command']);assert changed.status=='preview' and '1 รายการ' in changed.message and h.provider.posts==0
    assert h.send(r.data['confirm_command']).status=='stale'
    assert h.send(changed.data['confirm_command']).status=='created'

def test_cancel_pending_does_not_delete_saved_event():
    h=Harness();r=complete(h);data=r.data['line_message']['contents']['footer']['contents'][1]['action']['data']
    assert h.app.handle(h.event(data,postback=True)).status=='cancelled' and not h.provider.events
    r=complete(h,'ประชุมทีม');assert h.send(r.data['confirm_command']).status=='created'
    assert h.send('ยกเลิกนัดล่าสุด').status=='clarification' and len(h.provider.events)==1 and h.provider.posts==1

def test_cold_restore_validates_calendar_facts_and_confirm_binding():
    h=Harness();r=complete(h);snapshot=history_snapshot(h.state)
    fresh=HistoryState();fresh.raw_journal=h.state.raw_journal;fresh.clock=lambda:h.now
    restore_history(fresh,snapshot,'mock-owner')
    other=Harness(state=fresh,provider=h.provider);other.now=h.now;other.counter=50
    assert other.send(r.data['confirm_command']).status=='created' and h.provider.posts==1
    corrupt=deepcopy(snapshot);next(iter(corrupt['calendar_v1']['drafts'].values()))['facts']['title']='forged'
    with pytest.raises(ValueError):restore_history(fresh,corrupt,'mock-owner')

def test_feature_off_retains_v11_tools_and_cannot_claim_calendar_success():
    h=Harness(enabled=False);r=h.send('สวัสดี',response(text='สวัสดีครับ'))
    body=h.model.requests[-1];assert 'calendar_drafts' not in json.loads(body['input'][0]['content'])
    assert not any(t['name'] in h.app.calendar_flow.tool_names for t in body['tools'])
    assert 'calendar_v1' not in history_snapshot(h.state)

def test_feature_on_rejects_model_text_claim_without_a_server_creation():
    h=Harness();r=h.send('นัดตรวจฟัน',response(text='สร้างนัดตรวจฟันแล้ว'))
    assert r.status=='clarification' and not h.provider.events

@pytest.mark.parametrize('word,clock',[('2pm','14:00'),('12am','00:00'),('12pm','12:00')])
def test_explicit_meridiem(word,clock):assert event_time(word).strftime('%H:%M')==clock

@pytest.mark.parametrize('word',['14:00pm','14:00ครึ่ง','2','24'])
def test_invalid_or_ambiguous_bound_time(word):
    with pytest.raises(ValueError):event_time(word,allow_bare=True)

def test_decimal_hours_must_resolve_to_whole_minutes():
    assert duration('1.5ชั่วโมง')==90
    with pytest.raises(ValueError):duration('0.001ชั่วโมง')

def test_bare_answers_require_the_delivered_missing_field_question():
    h=Harness();r=h.prepare('นัดตรวจฟันพรุ่งนี้',title='ตรวจฟัน',date_text='พรุ่งนี้')
    assert set(r.data['calendar_view']['missing_fields'])=={'time','end'}
    r=h.prepare('14',time_text='14');assert r.status=='clarification' and h.draft()['start'].endswith('14:00:00+07:00')
    r=h.prepare('45',duration_text='45');assert r.status=='preview' and h.draft()['end'].endswith('14:45:00+07:00')
    h=Harness();h.prepare('นัดตรวจฟันพรุ่งนี้บ่ายสอง',title='ตรวจฟัน',date_text='พรุ่งนี้',time_text='บ่ายสอง')
    h.send('สวัสดี',response(text='สวัสดีครับ'))
    r=h.prepare('45',duration_text='45');assert r.status=='clarification' and h.draft()['review'] is None and not h.provider.posts

def test_overnight_end_requires_an_explicit_date_instead_of_guessing():
    h=Harness();r=h.prepare('นัดตรวจฟันพรุ่งนี้ 23:00 ถึง 01:00',title='ตรวจฟัน',date_text='พรุ่งนี้',time_text='23:00',end_time_text='01:00')
    assert r.status=='clarification' and 'calendar_end_order' in h.draft()['issues']
    r=h.prepare('สิ้นสุดมะรืน 01:00',end_date_text='มะรืน',end_time_text='01:00')
    assert r.status=='preview' and h.draft()['end'].startswith('2026-10-18T01:00:00')

def test_original_relative_date_remains_anchored_across_bangkok_midnight():
    h=Harness();h.now=datetime.fromisoformat('2026-10-16T16:55:00+00:00')
    h.prepare('นัดตรวจฟันพรุ่งนี้บ่ายสอง',title='ตรวจฟัน',date_text='พรุ่งนี้',time_text='บ่ายสอง')
    h.now+=timedelta(minutes=10);r=h.prepare('45 นาที',duration_text='45 นาที')
    assert r.status=='preview' and h.draft()['start'].startswith('2026-10-17T14:00:00')

def test_alternative_owner_dates_cannot_be_arbitrarily_selected():
    h=Harness();r=h.prepare('นัดตรวจฟันพรุ่งนี้หรือมะรืนบ่ายสอง 45 นาที',title='ตรวจฟัน',date_text='พรุ่งนี้',time_text='บ่ายสอง',duration_text='45 นาที')
    assert r.status=='clarification' and not h.state.calendar_drafts and not h.provider.calls

def test_manual_result_check_is_positive_read_only_reconciliation_without_model():
    h=Harness();r=complete(h);h.provider.fail_after_create=True
    with pytest.raises(ProviderFailure):h.send(r.data['confirm_command'])
    calls=len(h.model.requests);h.provider.fail_after_create=False
    result=h.send('ตรวจสอบผลนัด')
    assert result.status=='created' and h.provider.posts==1 and len(h.model.requests)==calls
    assert all(method=='GET' for method,_ in h.provider.calls[h.provider.calls.index(('POST',''))+1:])

def test_even_not_committed_adapter_result_cannot_retry_an_uncertain_calendar_post():
    from line_assistant.adapters import Reconciliation
    h=Harness();r=complete(h);h.provider.fail_before_create=True
    with pytest.raises(ProviderFailure):h.send(r.data['confirm_command'])
    h.app.calendar.reconcile=lambda *args:Reconciliation('not_committed')
    assert h.send('ตรวจผลนัด').status=='uncertain' and h.provider.posts==1

def test_two_explicitly_confirmed_identical_appointments_have_distinct_ids():
    h=Harness();r=complete(h);assert h.send(r.data['confirm_command']).status=='created'
    first=set(h.provider.events);r=complete(h);assert h.send(r.data['confirm_command']).status=='created'
    assert len(h.provider.events)==2 and h.provider.posts==2 and first<set(h.provider.events)

def test_stale_cancel_and_typed_postback_cannot_change_the_current_draft():
    h=Harness();r=complete(h);old_cancel=r.data['line_message']['contents']['footer']['contents'][1]['action']['data']
    r=h.prepare('แก้เป็นบ่ายสาม',time_text='บ่ายสาม');before=deepcopy(h.draft())
    assert h.app.handle(h.event(old_cancel,postback=True)).status=='stale'
    data=r.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    assert h.send(data).status=='stale' and h.draft()==before and h.provider.posts==0

def test_calendar_read_forbidden_preserves_facts_and_never_authorizes_write():
    h=Harness();h.provider.forbidden=True;r=complete(h)
    assert r.status=='clarification' and h.draft()['facts']['title']=='ตรวจฟัน' and h.draft()['review'] is None
    h.provider.forbidden=False
    def review(body):
        c=json.loads(body['input'][0]['content']);d=c['calendar_drafts'][0]
        return response('control_calendar_draft',{'draft_ref':d['draft_ref'],'draft_version':d['version'],'operation':'review'})
    r=h.send('ตรวจร่างนัด',review);assert r.status=='preview'
    h.provider.forbidden=True
    assert h.send(r.data['confirm_command']).status=='setup_required' and h.provider.posts==0

def test_sqlite_cold_worker_recovers_unknown_write_with_positive_read_only_proof(tmp_path):
    path=tmp_path/'synthetic.sqlite';state=LocalHistoryState(path,'mock-owner',clock=lambda:NOW)
    h=Harness(state=state);r=complete(h);h.provider.fail_after_create=True
    with pytest.raises(ProviderFailure):h.send(r.data['confirm_command'])
    fresh=LocalHistoryState(path,'mock-owner',clock=lambda:h.now)
    other=Harness(state=fresh,provider=h.provider);other.now=h.now;other.counter=50
    h.provider.fail_after_create=False
    assert other.send('ตรวจผลนัด').status=='created' and h.provider.posts==1

def test_expired_review_never_writes_and_calendar_why_uses_no_expense_today_default():
    h=Harness();r=h.prepare('นัดตรวจฟัน',title='ตรวจฟัน')
    reason=h.send('ทำไม');assert reason.status=='answer' and 'วันนัด' in reason.message and 'วันนี้' not in reason.message
    h=Harness();r=complete(h);h.now+=timedelta(minutes=31)
    assert h.send(r.data['confirm_command']).status=='stale' and h.provider.posts==0

def test_owner_and_destination_drift_cannot_confirm_a_review():
    h=Harness();r=complete(h);event=h.event(r.data['confirm_command']);event['source']['userId']='different-owner'
    assert h.app.handle(event).status=='denied' and h.provider.posts==0
    h.app.calendar.calendar_id='different-calendar'
    assert h.send(r.data['confirm_command']).status=='stale' and h.provider.posts==0

def test_foreign_source_and_empty_patch_cannot_mutate_calendar_facts():
    h=Harness();complete(h);before=deepcopy(h.draft())
    def forged(body):
        c=json.loads(body['input'][0]['content']);d=c['calendar_drafts'][0]
        return response('prepare_calendar_event',{'draft_ref':d['draft_ref'],'draft_version':d['version'],
            **dict.fromkeys(['title','date_text','time_text','end_date_text','end_time_text','duration_text']),
            'title':'invented','source_refs':['src_foreign']})
    assert h.send('แก้ชื่อ',forged).status=='clarification' and h.draft()['facts']==before['facts'] and h.draft()['review'] is None
    revoked=deepcopy(h.draft())
    assert h.prepare('ตรวจร่างนัด').status=='clarification' and h.draft()==revoked
