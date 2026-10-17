"""Disposable local process for SQLite/OS-exit tests; model port is scripted."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json,os,re,socket,sys
from line_assistant.history_persistence import local_history_assistant

def deny(*a,**k):raise AssertionError('Persistence worker cannot use network')
socket.socket.connect=deny;socket.socket.connect_ex=deny

class ScriptPort:
    mocked=True
    def __init__(self,phase_hook=None):self.requests=[];self.phase_hook=phase_hook or (lambda phase,key:None)
    def request(self,method,path,*,body):
        self.requests.append(deepcopy(body));c=json.loads(body['input'][0]['content']);text=c['message']
        self.phase_hook('model_started',c['current_source_ref'])
        call=None;answer=None
        if 'ดาวฤกษ์' in text or text.startswith('เรื่องอื่น'):answer='ตอบอีกหัวข้อได้โดยไม่เปลี่ยนร่าง'
        elif c['available_drafts'] and len(body['input'])==1:
            call=('read_expense_drafts',{'draft_ref':c['available_drafts'][0]['draft_ref']})
        else:
            if len(body['input'])>1:
                d=json.loads(body['input'][2]['output'])['drafts'][0];e=d['entries'][0];ref=d['draft_ref'];version=d['draft_version']
            else:e={'entry_ref':None,'item':'ค่ากระดาษ','amount':'228','date':None,'note':''};ref=None;version=None
            amount=re.search(r'(?:ยอด.*?เป็น|ค่ากระดาษ)\s*(\d+)\s*บาท',text)
            entry={k:e[k] for k in ['entry_ref','item','amount','date','note']}
            if amount:entry['amount']=amount[1]
            entry.update(date_text=None,category=None,subcategory=None,source_refs=[c['current_source_ref']])
            call=('prepare_expenses',{'draft_ref':ref,'draft_version':version,'entries':[entry]})
        out=[{'type':'message','status':'completed','content':[{'type':'output_text','text':answer}]}] if answer else [{'type':'function_call','status':'completed','call_id':'local_sqlite','name':call[0],'arguments':json.dumps(call[1],ensure_ascii=False)}]
        return {'model':'gpt-6-luna','status':'completed','usage':{'input_tokens':800,'output_tokens':100},'output':out}

def main():
    directory,raw,phase=sys.argv[1:4];value=json.loads(Path(raw).read_text());at=datetime.fromisoformat(value['now'])
    def stop(name,key):
        if name==phase:os._exit(73)
    port=ScriptPort(stop)
    service=local_history_assistant(Path(directory),port=port,clock=lambda:at,enabled=True,
        reconciliation_available=value.get('available',True),phase_hook=stop)
    if phase in {'state_staged','state_committed'}:
        service.state.phase_hook=stop  # Constructor/schema setup is not the fault.
    result=service.handle(value['event'])
    print(json.dumps({'status':result.status,'message':result.message,'data':result.data,'model_requests':len(port.requests)},ensure_ascii=False))

if __name__=='__main__':main()
