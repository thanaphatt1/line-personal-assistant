"""Synthetic raw-history workflow adapters; no credential, cloud or paid code."""
from copy import deepcopy
from datetime import datetime,timedelta
import json
from line_assistant.adapters import SheetLayout
from line_assistant.config import Settings
from line_assistant.conversation_mocks import ConversationSheets
from line_assistant.history_context import HistoryState,source_ref
from line_assistant.history_first import HistoryFirstAssistant
from line_assistant.history_persistence import history_snapshot,restore_history
from test_history_first import response,data
ANCHOR=datetime.fromisoformat('2026-10-14T10:27:00+07:00')
CATALOG=[('กาแฟ','Food/Drink','Food'),('ค่ากระดาษ','Work/Office','Supplies'),('รถเมล์','Transportation','Public transportation')]
TURNS={
 'unknown_item':[('ค่าวาฟเฟิลทดสอบ 60 บาทเมื่อวาน',2),('ใหม่',2),('หมวดอาหาร',2),('โอเค บันทึกเลย',0)],
 'spending_followup':[('จ่ายค่าวาฟเฟิลทดสอบไป 60 บาทเมื่อวาน',2),('เอาเป็นชื่อใหม่ครับ',2),('คุยเรื่องดาวฤกษ์ก่อน',2),('กลับมารายการนี้ เพิ่มชื่อใหม่ได้เลย',2),('อาหารครับ',2),('อย่าเพิ่งบันทึก เปลี่ยนยอดเป็น 75 บาท',2),('ไม่เอาแล้ว ยกเลิกร่างนี้ครับ',2)],
 'rolling_summary':[('คุยต่อจากเรื่องเมื่อกี้ครับ',2)],
}
class Port:
    mocked=True
    def __init__(self,generate=None):
        self.values=[];self.records=[];self.requests=[];self.generate=generate;self.limit=2;self.turn_calls=0
    def request(self,method,path,*,body):
        assert (method,path)==('POST','/v1/responses')
        assert self.turn_calls<self.limit,'Unexpected extra scripted response'
        self.turn_calls+=1
        value=self.generate(body) if self.generate else self.values.pop(0)
        value=value(deepcopy(body)) if callable(value) else deepcopy(value)
        self.requests.append(deepcopy(body));self.records.append({'request':deepcopy(body),'response':deepcopy(value)})
        if isinstance(value,Exception):raise value
        return value

class Harness:
    def __init__(self, port, case, *, context_tokens=None):
        self.now=ANCHOR; self.counter=0; self.case=case; self.port=port
        self.state=HistoryState()
        self.sheets=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),CATALOG)
        self.service=HistoryFirstAssistant(Settings(owner_id='synthetic-workflow-owner'),self.sheets,port=port,state=self.state,clock=lambda:self.now,history_first_enabled=True,item_enrollment_enabled=True,semantic_enabled=True,raw_history_enabled=True,raw_context_tokens=context_tokens)
    def event(self,text):
        self.counter+=1; self.now+=timedelta(seconds=1)
        return {'webhookEventId':'synthetic-'+self.case+'-'+str(self.counter),'timestamp':int(self.now.timestamp()*1000),'source':{'type':'user','userId':'synthetic-workflow-owner'},'type':'message','message':{'id':str(self.counter),'type':'text','text':text}}
    def seed_pair(self,number,text,reply,age):
        event='synthetic-seed-'+self.case+'-'+str(number); at=self.now-age
        self.state.append('user',event,at,text=text)
        self.state.append('assistant',event,at,text=reply)
        row=self.state.history[-1];row['delivery_verified']=True;self.state.raw_journal.put(row)
    def run_turn(self,text,limit):
        event=self.event(text);self.port.case=self.case;self.port.turn=text;self.port.limit=limit;self.port.turn_calls=0
        self.port.state=self.state;self.port.now=self.now
        before=len(self.port.records); result=self.service.handle(event)
        return event,result,{'text':text,'status':result.status,'message':result.message,'data':deepcopy(result.data),'calls':len(self.port.records)-before,'trace':deepcopy(self.service.trace),'drafts':deepcopy(self.state.drafts),'fake_commits':len(self.sheets.entries),'fake_catalog_batches':self.sheets.atomic_batches}

def cold_replay(h,event,result):
    """New worker restored from serialized core and the retained raw journal."""
    original_calls=len(h.port.records);writes=len(h.sheets.entries);batches=h.sheets.atomic_batches
    state=HistoryState();state.clock=lambda:h.now;state.enable_raw_history()
    for row in h.state.raw_journal.rows.values():state.raw_journal.put(deepcopy(row))
    restore_history(state,history_snapshot(h.state),'synthetic-workflow-owner')
    assistant=HistoryFirstAssistant(Settings(owner_id='synthetic-workflow-owner'),h.sheets,port=h.port,state=state,clock=lambda:h.now,
        history_first_enabled=True,item_enrollment_enabled=True,semantic_enabled=True,raw_history_enabled=True)
    h.port.limit=0;again=assistant.handle(deepcopy(event))
    return {'identical_result':again==result,'model_calls':len(h.port.records)-original_calls,
            'fake_rows_added':len(h.sheets.entries)-writes,'fake_catalog_batches_added':h.sheets.atomic_batches-batches,
            'raw_originals_retained':len(state.raw_journal.rows)==len(h.state.raw_journal.rows)}

def facts(h,amount='60'):
    ds=[d for d in h.state.drafts.values() if d['status']!='recorded']
    return len(ds)==1 and len(ds[0]['entries'])==1 and ds[0]['entries'][0]['item']=='วาฟเฟิลทดสอบ' and ds[0]['entries'][0]['amount']==amount and ds[0]['entries'][0]['date']=='2026-10-13'

def draft(h):return next(iter(h.state.drafts.values()))
def proposal_arguments(h,kind,body=None):
    """Fresh proposals bound to the current synthetic event and owned draft."""
    if kind=='history_summary':return {'summary':'คุยเรื่องต้นไม้ในวันที่ 13 ตุลาคม 2026 ไม่มีสิทธิ์บันทึก','source_refs':[r['source_ref'] for r in h.state.history if r['role']=='user'][:9]}
    d=draft(h) if h.state.drafts else None
    ref=json.loads(body['input'][0]['content'])['current_source_ref'] if body else source_ref('synthetic-'+h.case+'-'+str(h.counter+1))
    if kind=='prepare_unknown':return {'draft_ref':None,'draft_version':None,'entries':[data(item='วาฟเฟิลทดสอบ',amount=60,date='2026-10-13',date_text='เมื่อวาน',source_refs=[ref])]}
    if kind in ('unsupported_item_lookup','category_lookup'):return {'kind':'category' if kind=='category_lookup' else 'item','query':'อาหาร' if kind=='category_lookup' else 'วาฟเฟิลทดสอบ'}
    if kind=='begin_new_item':return {'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':None,'category':None,'subcategory':None,'existing_item':None,'resolution_step':'begin_new_item'}
    if kind=='read_draft':return {'draft_ref':d['draft_ref']}
    if kind=='amount_update_with_category':return {'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[data(entry_ref=d['entries'][0]['entry_ref'],item=d['entries'][0]['item'],amount=75,date='2026-10-13',date_text='เมื่อวาน',category='Food/Drink',source_refs=d['entries'][0]['source_refs']+[ref])]}
    if kind=='cancel_draft':return {'operation':'cancel','draft_ref':d['draft_ref'],'draft_version':d['version']}

    raise AssertionError('Unknown synthetic proposal')
def scripted(h,kind):
    if kind=='conversation_reply':return response(text='คุยต่อได้ครับ')
    name='prepare_expenses' if kind in ('prepare_unknown','amount_update_with_category') else 'prepare_item_enrollment' if kind=='begin_new_item' else 'read_expense_drafts' if kind=='read_draft' else 'control_expense_draft' if kind=='cancel_draft' else 'roll_history_summary' if kind=='history_summary' else 'lookup_catalog'
    return lambda body:response(name=name,args=proposal_arguments(h,kind,body))
