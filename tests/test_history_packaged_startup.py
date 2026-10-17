"""Start the exact source bundle outside the workspace; detect missing imports."""
from pathlib import Path
import importlib.util,json,os,subprocess,sys

def test_isolated_exact_bundle_candidate_and_bridge_startup(tmp_path):
    root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'scripts'))
    from prepare_calendar_source import prepare
    out=tmp_path/'bundle';prepare(root,out)
    script='''import json,socket,sys
from pydantic import SecretStr
sys.path.insert(0,str(__import__('pathlib').Path.cwd()/'scripts'))
from verify_history_signed_startup import run_checks
from line_assistant.pilot_config import MAIN_WORKBOOK_ID
def deny(*a,**k):raise AssertionError('Packaged startup must never use network')
socket.socket.connect=deny;socket.socket.connect_ex=deny
base={'PILOT_MODE':'production','PILOT_LAUNCH_AUTHORIZED':'true','PILOT_CONVERSATION_ENABLED':'true',
'PILOT_SPREADSHEET_ID':MAIN_WORKBOOK_ID,'PILOT_LINE_OWNER_ID':'U'+'a'*32,'PILOT_MODEL_MONTHLY_MICRO_USD':'500000',
'PILOT_MODEL_MAX_CALLS_DAY':'100','PILOT_MODEL_MAX_CALLS_MONTH':'300','PILOT_HISTORY_STATE_ENABLED':'true'}
for enabled in ['false','true']:
 r=run_checks({**base,'PILOT_HISTORY_ENABLED':enabled},secret_loader=lambda n:SecretStr('synthetic-'+n))
 assert r['status']=='passed' and r['provider_requests']==0
from datetime import datetime,timezone,timedelta
from line_assistant.history_first import HistoryFirstAssistant
from line_assistant.history_context import HistoryState
from line_assistant.conversation_mocks import ConversationSheets
from line_assistant.adapters import SheetLayout
from line_assistant.config import Settings
from line_assistant.model_api import MODEL_ID
class Port:
 mocked=True
 def __init__(self,item='รถตู้',values=(30,32),date='วันนี้'):self.calls=0;self.item=item;self.values=values;self.date=date
 def request(self,method,path,*,body):
  self.calls+=1;c=json.loads(body['input'][0]['content'])
  args={'draft_ref':None,'draft_version':None,'entries':[{'entry_ref':None,'item':self.item,'item_text':None,'amount':n,'date_text':self.date,'date_ref':None,'note':None,'source_refs':[c['current_source_ref']],'field_sources':None} for n in self.values]}
  return {'model':MODEL_ID,'status':'completed','usage':{'input_tokens':900,'output_tokens':100},'output':[{'type':'function_call','status':'completed','name':'prepare_expenses','call_id':'synthetic-call','arguments':json.dumps(args,ensure_ascii=False)}]}
now=[datetime(2026,10,16,1,tzinfo=timezone.utc)];p=Port()
sheet=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),[('รถตู้','Transportation','')])
a=HistoryFirstAssistant(Settings(owner_id='mock-owner'),sheet,port=p,state=HistoryState(),clock=lambda:now[0],history_first_enabled=True,semantic_enabled=True,item_enrollment_enabled=True,raw_history_enabled=True)
def event(i,text):
 now[0]+=timedelta(seconds=1)
 return {'webhookEventId':'packaged-'+str(i),'timestamp':int(now[0].timestamp()*1000),'source':{'type':'user','userId':'mock-owner'},'type':'message','message':{'id':str(i),'text':text,'type':'text'}}
preview=a.handle(event(1,'วันนี้มีค่ารถตู้ 2 รอบ 30 กับ 32'));assert preview.status=='preview'
e=event(2,preview.data['confirm_command']);saved=a.handle(e)
assert saved.status=='recorded' and len(sheet.entries)==2 and a.handle(e)==saved and len(sheet.entries)==2 and p.calls==1
for item,text,values in [('MRT','เพิ่ม mrt 56 บาท เมื่อวาน',(56,)),('Café','Cafe\\u0301 30 บาท และ CAFÉ 32 บาท เมื่อวาน',(30,32)),('Straße','STRASSE 30 บาท และ straße 32 บาท เมื่อวาน',(30,32)),('grab bike','grabbike 37 บาท เมื่อวาน',(37,))]:
 p=Port(item,values,'เมื่อวาน');sheet=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),[(item,'Transportation','')])
 a=HistoryFirstAssistant(Settings(owner_id='mock-owner'),sheet,port=p,state=HistoryState(),clock=lambda:now[0],history_first_enabled=True,semantic_enabled=True,item_enrollment_enabled=True,raw_history_enabled=True)
 preview=a.handle(event(1,text));assert preview.status=='preview',preview
 assert all(r['item']==item and r['date']=='2026-10-15' for r in preview.data['tool_result']['entries'])
 for r in preview.data['tool_result']['entries']:
  if r.get('amount_occurrence'):
   start,end=r['amount_occurrence']['span'];assert text[start:end]==r['amount']
 e=event(2,preview.data['confirm_command']);saved=a.handle(e)
 assert saved.status=='recorded' and len(sheet.entries)==len(values) and a.handle(e)==saved and len(sheet.entries)==len(values) and p.calls==1
class NamePort:
 mocked=True
 def __init__(self):self.calls=0
 def request(self,method,path,*,body):
  self.calls+=1;c=json.loads(body['input'][0]['content'])
  if self.calls==1:
   values=[('grab bike','gabr bike',37),('รถรับส่ง',None,26)]
   args={'draft_ref':None,'draft_version':None,'entries':[{'entry_ref':None,'item':name,'item_text':literal,'amount':n,'date_text':None,'date_ref':None,'note':None,'source_refs':[c['current_source_ref']],'field_sources':None} for name,literal,n in values]};tool='prepare_expenses'
  else:
   d=c['available_drafts'][0];args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':d['entries'][0]['entry_ref'],'category':None,'subcategory':None,'existing_item':'grab bike','resolution_step':'use_existing_item'};tool='prepare_item_enrollment'
  return {'model':MODEL_ID,'status':'completed','usage':{'input_tokens':900,'output_tokens':100},'output':[{'type':'function_call','status':'completed','name':tool,'call_id':'synthetic-call','arguments':json.dumps(args,ensure_ascii=False)}]}
p=NamePort();sheet=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),[('grab bike','Transportation',''),('รถรับส่ง','Transportation','')])
a=HistoryFirstAssistant(Settings(owner_id='mock-owner'),sheet,port=p,state=HistoryState(),clock=lambda:now[0],history_first_enabled=True,semantic_enabled=True,item_enrollment_enabled=True,raw_history_enabled=True)
pending=a.handle(event(1,'gabr bike 37 บาท กับ รถรับส่ง 26 บาท'));assert pending.status=='clarification' and not sheet.entries
preview=a.handle(event(2,'ใช่ครับ'));assert preview.status=='preview' and [r['amount'] for r in preview.data['tool_result']['entries']]==['37','26'] and not sheet.entries
e=event(3,preview.data['confirm_command']);saved=a.handle(e)
assert saved.status=='recorded' and len(sheet.entries)==2 and a.handle(e)==saved and p.calls==2 and len(sheet.entries)==2
print('isolated exact bundle imports and signed startup passed both modes')
'''
    env={**os.environ,'PYTHONPATH':str(out/'source/src'),'PYTHONDONTWRITEBYTECODE':'1'}
    p=subprocess.run([sys.executable,'-c',script],cwd=out/'source',env=env,text=True,capture_output=True,timeout=30)
    assert p.returncode==0,p.stderr
    assert 'passed both modes' in p.stdout
