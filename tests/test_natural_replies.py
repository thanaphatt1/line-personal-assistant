"""Natural owner wording keeps verified values, consent and real uncertainty visible."""
from copy import deepcopy
import json,re,socket
import pytest
from line_assistant import operation_composition as operation
from line_assistant.answer_composition import AnswerError,render
from line_assistant.calendar_workflow import temporal_context
from line_assistant.history_first import planner_failure_message
from line_assistant.state import Result
from test_calendar_mvp import Harness,complete,response
from test_calendar_batch_factory_v13 import proposed,text as batch_text
from test_calendar_text_confirmation_v13 import turn
from test_response_realization import VoiceHosted,generated


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def facts(**values):
    return {'title':'ตรวจฟัน','date_text':'พรุ่งนี้','time_text':'14:00','duration_text':'30นาที',**values}


@pytest.mark.parametrize('text,values,notice',[
    ('นัดตรวจฟันพรุ่งนี้ประมาณ14:00 30นาที',{},'เวลาเริ่มเป็นค่าประมาณ'),
    ('นัดตรวจฟันพรุ่งนี้ราวๆ14:00 30นาที',{},'เวลาเริ่มเป็นค่าประมาณ'),
    ('นัดตรวจฟันพรุ่งนี้ 14:00 ใช้เวลาประมาณชั่วโมงนึง',{'duration_text':'ชั่วโมงนึง'},'ระยะเวลาเป็นค่าประมาณ'),
    # A model may keep the qualifier inside the literal; it is wording, not the value.
    ('นัดตรวจฟันพรุ่งนี้ประมาณ14:00 30นาที',{'time_text':'ประมาณ14:00'},'เวลาเริ่มเป็นค่าประมาณ')])
def test_approximate_owner_time_is_prepared_and_disclosed_before_consent(text,values,notice):
    h=Harness();r=h.prepare(text,**facts(**values))
    assert r.status=='preview' and notice in r.message and h.provider.posts==0
    assert notice in ' '.join(r.data['calendar_notices']) and h.draft()['approximate_fields']
    assert h.send(r.data['confirm_command']).status=='created' and h.provider.posts==1


@pytest.mark.parametrize('text,values',[
    ('นัดตรวจฟันพรุ่งนี้อาจจะ14:00 30นาที',{}),
    ('นัดตรวจฟันพรุ่งนี้ยังไม่แน่ใจ14:00 30นาที',{}),
    ('นัดตรวจฟันพรุ่งนี้ 14:00 หรือ 15:00 30นาที',{}),
    ('นัดตรวจฟันพรุ่งนี้หรือมะรืน 14:00 30นาที',{})])
def test_alternatives_or_hesitation_at_a_date_or_time_still_ask_first(text,values):
    h=Harness();r=h.prepare(text,**facts(**values))
    assert r.status=='clarification' and not h.state.calendar_drafts and h.provider.posts==0
    assert 'วันหรือเวลานัดยังไม่แน่นอน' in r.message


@pytest.mark.parametrize('text',['นัดกินข้าวกับแม่พรุ่งนี้ 14:00 30นาที ร้านเดิมหรือร้านไหนก็ได้',
    'นัดตรวจฟันพรุ่งนี้ 14:00 30นาที ไม่รู้จะต้องเตรียมอะไรหรือเปล่า'])
def test_unrelated_alternatives_elsewhere_in_the_message_do_not_block_preparation(text):
    title='กินข้าวกับแม่' if 'แม่' in text else 'ตรวจฟัน'
    h=Harness();r=h.prepare(text,**facts(title=title))
    assert r.status=='preview' and h.provider.posts==0 and 'ค่าประมาณ' not in r.message


def test_exact_correction_clears_an_earlier_approximation():
    h=Harness();h.prepare('นัดตรวจฟันพรุ่งนี้ประมาณ14:00 30นาที',**facts())
    assert h.draft()['approximate_fields']==['time_text']
    d=h.draft();r=h.prepare('ขอเป็น 15:00 ตรงเลย',draft_ref=d['draft_ref'],draft_version=d['version'],time_text='15:00')
    assert r.status=='preview' and 'approximate_fields' not in h.draft() and 'ค่าประมาณ' not in r.message


def test_adjacency_is_judged_per_literal_occurrence():
    assert temporal_context('ประชุม 10:00 หรือ 11:00','10:00')==(False,True)
    assert temporal_context('ประชุม 10:00 ห้องเดิม ไม่แน่ใจว่าใครมาหรือเปล่า','10:00')==(False,False)
    # Hesitation directly after the time is treated conservatively as about the time.
    assert temporal_context('ประชุม 10:00 ไม่แน่ใจ','10:00')==(False,True)
    assert temporal_context('เจอกันบ่ายสองกว่าๆ','บ่ายสอง')==(True,False)


def test_owner_can_ask_to_see_the_review_again_in_ordinary_words():
    h=Harness();old=complete(h)
    def control(body):
        d=json.loads(body['input'][0]['content'])['calendar_drafts'][0]
        return response('control_calendar_draft',{'draft_ref':d['draft_ref'],'draft_version':d['version'],'operation':'review'})
    again=h.send('โอเค ขอดูอีกรอบ',control)
    assert again.status=='preview' and h.provider.posts==0
    assert h.send(old.data['confirm_command']).status=='stale' and h.send(again.data['confirm_command']).status=='created'


def test_plain_confirmation_after_a_draft_read_shows_fresh_review_then_creates_once():
    h=Harness();old=complete(h)
    def read(body):return response('read_calendar_drafts',{'draft_ref':None})
    listed=h.send('ขอดูนัดที่ค้างไว้',read,response(text='นี่คือนัดที่ค้างไว้ครับ'))
    assert 'ตรวจฟัน · 17 ต.ค. 2026 เวลา 14:00–14:45' in listed.message and not re.search(r'\d{4}-\d{2}-\d{2}',listed.message)
    again=h.send('ยืนยัน')
    assert again.status=='preview' and h.provider.posts==0 and again.data['confirm_command']!=old.data['confirm_command']
    assert 'ยืนยันอีกครั้ง' in again.message and h.send(old.data['confirm_command']).status=='stale'
    assert h.send('ยืนยัน').status=='created' and h.provider.posts==1


def test_calendar_review_and_receipt_text_use_readable_bangkok_dates():
    h=Harness();r=complete(h)
    assert '17 ต.ค. 2026 เวลา 14:00–14:45' in r.message and 'Asia/Bangkok' not in r.message
    assert not re.search(r'\d{4}-\d{2}-\d{2}',r.message) and 'ไม่พบนัดทับในปฏิทินนี้' in r.message
    created=h.send(r.data['confirm_command'])
    assert created.status=='created' and '17 ต.ค. 2026 เวลา 14:00–14:45' in created.message
    assert not re.search(r'\d{4}-\d{2}-\d{2}',created.message) and created.data['title']=='ตรวจฟัน'


def test_calendar_review_document_keeps_status_notice_values_and_instruction():
    h=Harness();r=complete(h);view=operation.for_result(r)
    labels=set(view['fact_labels'].values())
    assert {'status','ปฏิทิน','หมายเหตุ','instruction'}<=labels and len(view['records'])==1
    cells={f['label']:f['display'] for f in view['records'][0]['fields'].values()}
    assert cells=={'ชื่อนัด':'ตรวจฟัน','วันที่':'17 ต.ค. 2026','เวลา':'14:00–14:45'}
    text=operation.fallback(view)
    assert 'ยังไม่ได้สร้างนัดนี้' in text and 'ตรวจฟัน — 14:00–14:45' in text and text.rstrip().endswith('ได้เลยครับ')
    assert text.index('ไม่พบนัดทับ')>text.index('ตรวจฟัน')
    # A natural single paragraph is valid when every value, notice and instruction stays bound.
    ids={label:ref for ref,label in view['fact_labels'].items()}
    natural={'blocks':[{'kind':'text','template':'{{'+ids['status']+'}}'},
        {'kind':'rows','record_refs':['r1'],'heading':'','template':'{{c1}} วัน{{c2}} ช่วง {{c3}} ในปฏิทิน {{'+ids['ปฏิทิน']+'}}'},
        {'kind':'text','template':'{{'+ids['หมายเหตุ']+'}}'},{'kind':'text','template':'{{'+ids['instruction']+'}}'}]}
    assert 'ตรวจฟัน วัน17 ต.ค. 2026 ช่วง 14:00–14:45' in render(natural,view)


@pytest.mark.parametrize('mutation',['drop_notice','success_claim','copied_time'])
def test_calendar_review_document_cannot_hide_notices_or_claim_creation(mutation):
    h=Harness();r=complete(h);view=operation.for_result(r);ids={label:ref for ref,label in view['fact_labels'].items()}
    blocks=[{'kind':'text','template':'{{'+ids['status']+'}} {{'+ids['ปฏิทิน']+'}}'},
            {'kind':'rows','record_refs':['r1'],'heading':'','template':'{{c1}} {{c2}} {{c3}}'},
            {'kind':'text','template':'{{'+ids['หมายเหตุ']+'}}'},{'kind':'text','template':'{{'+ids['instruction']+'}}'}]
    if mutation=='drop_notice':del blocks[2]
    if mutation=='success_claim':blocks[1]['template']+=' สร้างนัดให้เรียบร้อยแล้ว'
    if mutation=='copied_time':blocks[1]['template']='{{c1}} {{c2}} 14:00'
    with pytest.raises(AnswerError):render({'blocks':blocks},view)


def test_created_receipt_document_requires_a_recorded_event_identity():
    h=Harness();created=h.send(complete(h).data['confirm_command']);view=operation.for_result(created)
    assert view and view['facts'][next(k for k,v in view['fact_labels'].items() if v=='status')]=='เพิ่มนัดแล้วครับ'
    legacy=Result('created',created.message,{k:v for k,v in created.data.items() if k!='title'})
    assert operation.for_result(legacy) is None
    partial=Result('created','x',{'domain':'calendar','calendar_label':'ปฏิทินจำลอง','created_events':[{'title':'A','start':'2026-10-17T14:00:00+07:00','end':'2026-10-17T15:00:00+07:00','all_day':False}],
        'created_refs':['a'],'pending_refs':['b']})
    assert operation.for_result(partial) is None


def test_hosted_batch_review_and_receipt_use_operation_documents_and_keep_bound_button():
    api=VoiceHosted();r,_=turn(api,batch_text(),proposed)
    review=json.loads(api.voice_bodies[-1]['input'][0]['content'])
    assert r.status=='preview' and review.get('operation_document') is True and review['outcome']=='preview'
    assert r.data['response_realization']['status']=='llm' and r.data['line_message']['contents']['footer']['contents'][0]['action']['data'].startswith('c2|confirm|')
    assert 'กิจกรรมทดสอบA' in r.message and 'กิจกรรมทดสอบB' in r.message and '22 ต.ค. 2026' in r.message and 'T08:' not in r.message
    saved,_=turn(api,'ยืนยันชุดนัดนี้')
    receipt=json.loads(api.voice_bodies[-1]['input'][0]['content'])
    assert saved.status=='created' and receipt.get('operation_document') is True and receipt['outcome']=='created'
    assert api.calendar.posts==2 and 'เพิ่มนัดครบ 2 รายการแล้วครับ' in saved.message


def test_reply_outage_on_calendar_review_uses_the_complete_readable_document_fallback():
    api=VoiceHosted()
    def broken(body):
        value=deepcopy(generated(body));value['output'][0]['content'][0]['text']='{"blocks":[]}';return value
    api.voice=broken;r,_=turn(api,batch_text(),proposed)
    assert r.status=='preview' and r.data['response_realization']['status']=='fallback'
    assert 'กิจกรรมทดสอบA' in r.message and 'กิจกรรมทดสอบB' in r.message and 'ยืนยันชุดนัด' in r.message
    assert r.data['line_message']['contents']['footer']['contents'][0]['action']['data'].startswith('c2|confirm|') and not api.calendar.posts


@pytest.mark.parametrize('code',['cancelled_fact_source','source_expired_or_removed'])
def test_remaining_evidence_mismatches_return_to_the_planner_before_asking_the_owner(monkeypatch,code):
    from test_catalog_names_v09 import harness,entry,prepare
    from line_assistant.history_first import HistoryFirstAssistant
    h=harness()
    def mismatch(self,refs):raise ValueError(code)
    monkeypatch.setattr(HistoryFirstAssistant,'_owner_sources',mismatch)
    r=prepare(h,'grab bike 37 บาท',[entry()])
    output=r.data['tool_result']
    assert r.status=='clarification' and output['error']==code and output['recovery']['kind']=='assistant_arguments'
    assert len(h.port.requests)==2 and not h.state.drafts and not h.sheets.entries
    assert 'ระบบ' not in r.message


@pytest.mark.parametrize('kind',['timeout','transport','rate_limited','provider_error','invalid_response','response_too_large','output_limit','usage_bound','unknown'])
def test_planner_failure_copy_states_no_effect_without_internal_terms(kind):
    message=planner_failure_message({'kind':kind})
    assert 'ยังไม่ได้บันทึกหรือสร้างอะไร' in message
    assert not re.search(r'โมเดล|ระบบ|ผ่านการตรวจ',message)


def test_missing_title_gets_one_short_question_instead_of_a_diagnostic_list():
    h=Harness();r=h.prepare('พรุ่งนี้ 14:00 30นาที',**facts(title=None))
    assert r.status=='clarification' and r.message=='นัดนี้ชื่ออะไรครับ?' and h.provider.posts==0
