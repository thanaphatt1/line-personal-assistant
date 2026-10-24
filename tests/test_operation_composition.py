"""Natural review layout must preserve verified values and delivered consent."""
from copy import deepcopy
from datetime import datetime
import json,socket
import httpx,pytest
from fastapi.testclient import TestClient

from line_assistant import operation_composition as operation,response_realization as voice
from line_assistant.answer_composition import AnswerError,render
from line_assistant.reply_display import calendar_parts
from line_assistant.state import Result
from line_assistant.persistence import result_from
from line_assistant.line_api import message_hash,rendered_message
from state_records_hosted import RecordsHosted,RecordsProtocol
from owner_features_hosted import OwnerHosted
from test_calendar_created_reschedule import created
from calendar_reschedule_hosted import find_last,move
from test_calendar_text_confirmation_v13 import turn
from test_calendar_mvp import response
from test_response_realization import generated


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def saved_result(status='preview',**changes):
    before={'title':'ประชุมทดสอบ','start':'2026-12-03T15:00:00+07:00','end':'2026-12-03T18:00:00+07:00','all_day':False}
    after={**before,'start':'2026-12-18T15:00:00+07:00','end':'2026-12-18T18:00:00+07:00',**changes}
    output={'status':'saved_edit_review','before':before,'after':after}
    return Result(status,'เลื่อนนัดแล้วครับ' if status=='updated' else 'old review',
        {'saved_record_edit':True,'domain':'calendar','tool_result':output,'before':before,'after':after})


def expense_result(recorded=False):
    entries=[{'item':'ร้านทดสอบA','amount':'11','date':'2026-12-02','note':''},
             {'item':'ร้านทดสอบA','amount':'11','date':'2026-12-02','note':''},
             {'item':'ร้านทดสอบB','amount':'32','date':'2026-12-03','note':'หมายเหตุทดสอบ'}]
    if recorded:return Result('recorded','old receipt',{'refs':('a','b','c'),'entries':entries})
    return Result('preview','old review',{'confirm_command':'internal','tool_result':{'ok':True,'status':'review','entries':entries}})


def natural_document(view):
    # This is an injected presentation decision, not an expected production
    # template. The same facts also support the different layout below.
    rows=view['records'];labels=view['fact_labels']
    if not rows:
        by_label={label:ref for ref,label in labels.items()}
        title=by_label.get('ชื่อนัด');old=by_label.get('วันที่เดิม');new=by_label.get('วันที่ใหม่');time=by_label.get('เวลา')
        if title and old and new and time:
            blocks=[{'kind':'text','template':'{{'+title+'}} จาก {{'+old+'}} เป็น {{'+new+'}} เวลา {{'+time+'}}'}]
            blocks += [{'kind':'text','template':'{{'+ref+'}}'} for ref,label in labels.items() if label in {'status','instruction'}]
            return {'blocks':blocks}
    blocks=[{'kind':'text','template':'{{'+ref+'}}'} for ref,label in labels.items() if label=='status']
    groups=[]
    for row in rows:
        key=tuple((k,f['label']) for k,f in row['fields'].items()),row['fields']['c1']['group_key']
        if groups and groups[-1][0]==key:groups[-1][1].append(row)
        else:groups.append((key,[row]))
    for _,group in groups:
        row=group[0]
        refs=list(row['fields'])
        blocks.append({'kind':'rows','record_refs':[r['id'] for r in group],'heading':'{{c1}}',
            'template':' — '.join('{{'+k+'}}' for k in refs[1:])})
    blocks += [{'kind':'text','template':'{{'+ref+'}}'} for ref,label in labels.items() if label!='status']
    return {'blocks':blocks}


@pytest.mark.parametrize('status',['preview','updated'])
def test_calendar_layout_uses_one_unchanged_time_and_readable_dates(status):
    result=saved_result(status);view=operation.for_result(result)
    text=render(natural_document(view),view)
    assert 'ประชุมทดสอบ จาก 3 ธ.ค. 2026 เป็น 18 ธ.ค. 2026 เวลา 15:00–18:00' in text
    assert text.count('15:00')==text.count('18:00')==1
    assert 'T15:' not in text and '+07:00' not in text
    assert ('ยืนยัน' in text)==(status=='preview')
    assert ('เลื่อนนัดแล้ว' in text)==(status=='updated')
    # A second valid layout demonstrates that no before/after sentence is fixed.
    alternate=operation.fallback(view)
    assert 'วันที่เดิม:' in alternate and 'วันที่ใหม่:' in alternate and 'ประชุมทดสอบ' in alternate
    assert 'T15:' not in alternate


def test_date_only_calendar_ranges_keep_inclusive_last_day_and_all_day_fact():
    value={'all_day':True,'start':'2026-12-03','end':'2026-12-06'}
    assert calendar_parts(value)==('3 ธ.ค. 2026 – 5 ธ.ค. 2026','ทั้งวัน')
    assert calendar_parts({**value,'end':'2026-12-04'})==('3 ธ.ค. 2026','ทั้งวัน')


def test_overnight_and_foreign_offset_times_keep_bangkok_days_and_precision():
    period,hours=calendar_parts({'all_day':False,'start':'2026-12-03T16:00:30.123400+00:00','end':'2026-12-03T20:00:30+00:00'})
    assert period=='3 ธ.ค. 2026 – 4 ธ.ค. 2026' and hours=='23:00:30.1234–03:00:30'


@pytest.mark.parametrize('recorded',[False,True])
def test_grouped_expense_fallback_preserves_duplicate_payments_and_every_date(recorded):
    view=operation.for_result(expense_result(recorded));text=operation.fallback(view)
    assert text.count('2 ธ.ค. 2026')==1 and text.count('3 ธ.ค. 2026')==1
    assert text.count('ร้านทดสอบA')==2 and text.count('11 บาท')==2 and '32 บาท' in text
    assert 'หมายเหตุทดสอบ' in text
    assert ('บันทึกรายจ่ายแล้ว' in text)==recorded
    assert 'ชีตจริง' not in text and 'Database' not in text


def test_new_catalog_category_and_complete_note_are_reviewed_without_raw_source_or_ids():
    result=expense_result();entries=result.data['tool_result']['entries'];entries[0]['mapping']={'category':'หมวดทดสอบ','subcategory':'ย่อยทดสอบ'}
    entries[0]['note']='ข้อความทดสอบยาว '*35
    view=operation.for_result(result);text=operation.fallback(view)
    assert entries[0]['note'] in text and 'หมวดทดสอบ / ย่อยทดสอบ' in text
    assert 'การยืนยันจะเพิ่มชื่อใหม่' in text
    assert 'internal' not in json.dumps(view,ensure_ascii=False)


@pytest.mark.parametrize('mutation',['drop_status','drop_row','drop_cell','invent_amount','claim_saved'])
def test_presentation_cannot_drop_review_facts_or_introduce_business_claims(mutation):
    view=operation.for_result(expense_result());document=natural_document(view)
    if mutation=='drop_status':document['blocks'].pop(0)
    if mutation=='drop_row':document['blocks'].pop(1)
    if mutation=='drop_cell':document['blocks'][1]['template']='{{c2}}'
    if mutation=='invent_amount':document['blocks'].insert(0,{'kind':'text','template':'รวม 999 บาท'})
    if mutation=='claim_saved':document['blocks'].insert(0,{'kind':'text','template':'บันทึกแล้วครับ'})
    with pytest.raises(AnswerError):render(document,view)


class NaturalReplies:
    def __init__(self,*args):super().__init__(*args);self.operations=[];self.unavailable=False;self.documents=[]
    def handle(self,request):
        if request.url.host=='api.openai.com':
            body=json.loads(request.content);payload=json.loads(body['input'][0]['content'])
            if body.get('instructions')==voice.INSTRUCTIONS and payload.get('operation_document'):
                self.model_bodies.append(deepcopy(body));self.operations.append(deepcopy(body))
                if self.unavailable:raise httpx.ReadTimeout('Synthetic optional reply outage',request=request)
                document=self.documents.pop(0) if self.documents else natural_document(payload['operation_facts'])
                return httpx.Response(200,json=generated(body,document))
        return super().handle(request)


class NaturalHosted(NaturalReplies,RecordsHosted):pass


class NaturalExpenseHosted(NaturalReplies,RecordsProtocol,OwnerHosted):
    def __init__(self,backend):super().__init__(backend);self.voice=True


@pytest.mark.parametrize('button',[False,True])
def test_cold_calendar_confirmation_keeps_short_copy_and_bound_button_and_replay(button):
    api=NaturalHosted();created(api)
    review,_=turn(api,'เลื่อนไปวันที่ 14',find_last,move())
    assert review.status=='preview' and review.data['response_realization']['status']=='llm'
    assert review.message.count('16:00')==1 and '4 พ.ย. 2026' in review.message and '14 พ.ย. 2026' in review.message
    assert 'ยืนยันแก้ se_' not in review.message and 'T16:' not in review.message
    body=api.operations[-1];assert voice.validate_body(body)
    assert 'se_' not in json.dumps(json.loads(body['input'][0]['content'])['operation_facts'])
    action=review.data['line_message']['contents']['footer']['contents'][0]['action']
    assert action['displayText']=='ยืนยัน' and action['data'].startswith('e1|confirm|se_')
    if button:
        event=api.event(action['data'],postback=True)
        with TestClient(api.app()) as client:assert api.send(client,event).status_code==200
        saved=result_from(api.snapshot()['receipts'][event['webhookEventId']]['result'])
    else:saved,event=turn(api,'ยืนยัน')
    assert saved.status=='updated' and api.patches==1 and api.calendar.posts==1
    assert saved.message.count('16:00')==1 and 'เลื่อนนัดแล้ว' in saved.message
    calls=len(api.model_bodies)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.patches==1 and len(api.model_bodies)==calls
    state=api.snapshot();reply=state['replies'][event['webhookEventId']]
    assert reply['content_hash']==message_hash(rendered_message(saved))


def test_optional_reply_outage_uses_readable_fallback_and_does_not_repeat_patch():
    api=NaturalHosted();created(api);api.unavailable=True
    review,_=turn(api,'เลื่อนไปวันที่ 14',find_last,move())
    assert review.status=='preview' and review.data['response_realization']['status']=='fallback'
    assert '4 พ.ย. 2026' in review.message and 'T16:' not in review.message and 'se_' not in review.message
    saved,event=turn(api,'ยืนยัน');assert saved.status=='updated' and api.patches==1
    assert 'T16:' not in saved.message and 'เลื่อนนัดแล้ว' in saved.message
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.patches==1


def test_operation_validation_feedback_repairs_once_without_changing_review_or_writing():
    api=NaturalHosted();created(api);before=len(api.operations)
    view=operation.for_result(saved_result())
    # IDs are local; the actual view has the same status/title/date/time shape.
    bad=natural_document(view);bad['blocks'].pop()
    api.documents=[bad]
    review,_=turn(api,'เลื่อนไปวันที่ 14',find_last,move())
    assert review.status=='preview' and api.patches==0
    assert review.data['response_realization']['repair_count']==1
    assert len(api.operations)-before==2 and 'answer_repair' in json.loads(api.operations[-1]['input'][0]['content'])
    saved,_=turn(api,'ยืนยัน');assert saved.status=='updated' and api.patches==1


@pytest.mark.parametrize('backend',['sheets','firestore'])
def test_expense_review_and_saved_receipt_group_one_day_and_keep_all_rows_after_restart(backend):
    api=NaturalExpenseHosted(backend);api.now=datetime.fromisoformat('2026-10-23T13:00:00+07:00')
    for index,item in enumerate(['ร้านทดสอบA','ร้านทดสอบB','ร้านทดสอบC'],2):
        for col,value in enumerate([item,'Food',''],1):api.google.cells[('Database',index,col)]=value
    if backend=='firestore':
        for name in list(api.business.documents):
            if name==api.business_target.parent or name.startswith(api.business_target.parent+'/'):
                del api.business.documents[name]
        api.seed_business_fixture()
    def propose(body):
        context=json.loads(body['input'][0]['content'])
        return response('prepare_expenses',{'draft_ref':None,'draft_version':None,'entries':[
            {'entry_ref':None,'item':item,'amount':amount,'date':None,'date_text':'เมื่อวาน','date_context':None,
             'note':'','category':None,'subcategory':None,'source_refs':[context['current_source_ref']]}
            for item,amount in [('ร้านทดสอบA','11'),('ร้านทดสอบB','22'),('ร้านทดสอบC','33')]]})
    review,_=turn(api,'เมื่อวาน ร้านทดสอบA 11 ร้านทดสอบB 22 ร้านทดสอบC 33',propose)
    assert review.status=='preview' and review.message.count('22 ต.ค. 2026')==1
    assert review.data['response_realization']['status']=='llm'
    for item in ('ร้านทดสอบA','ร้านทดสอบB','ร้านทดสอบC'):assert item in review.message
    saved,event=turn(api,'ยืนยัน');assert saved.status=='recorded',saved
    assert len(saved.data['refs'])==3 and len(saved.data['entries'])==3
    assert saved.message.count('22 ต.ค. 2026')==1 and 'ชีตจริง' not in saved.message
    assert all(entry['date']=='2026-10-22' for entry in saved.data['entries'])
    rows=deepcopy(api.google.cells);calls=len(api.model_bodies);writes=api.expense_writes
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.google.cells==rows and len(api.model_bodies)==calls and api.expense_writes==writes


def test_missing_continuation_date_is_one_question_without_split_connectors():
    from line_assistant.clarifications import compact
    class Host:raw_history_enabled=True
    result=Result('clarification','old explanation',{'tool_result':{'ok':True,'entries':[
        {'item':'รายการทดสอบ','amount':'15','date':None,'date_issue':'continuation_date_required'}],
        'missing_fields':['entries[0].date']}})
    compacted=compact(Host(),result)
    assert compacted.message=='รายการทดสอบ เป็นรายจ่ายวันที่เท่าไรครับ?'
    assert compacted.data['clarification_focus']['field']=='date'
