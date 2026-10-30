"""Owner feature tests use signed webhooks and synthetic providers only."""
from copy import deepcopy
import json
import httpx
from fastapi.testclient import TestClient

from expense_storage_hosted import StorageHosted
from test_calendar_mvp import response
from test_deployment_configuration import TARGET


FLAGS=('PILOT_SAVED_EXPENSE_DELETE_ENABLED','PILOT_CATALOG_ALIASES_ENABLED','PILOT_RECORD_ACTIONS_ENABLED')


class OwnerHosted(StorageHosted):
    def __init__(self,backend='sheets'):
        self.owner_features={key:True for key in FLAGS}
        super().__init__(backend)
        self.voice=False
        self.seed_legacy_buttons=False

    def app(self,enabled=True):
        app=super().app(enabled)
        if self.seed_legacy_buttons:
            host=app.state.assistant
            host.record_actions.attach=lambda event,result:legacy_offer(host,event,result)
        return app

    def configuration(self,enabled=True):
        return {**super().configuration(enabled),**{key:str(value).lower() for key,value in self.owner_features.items()}}

    def handle(self,request):
        if request.url.host=='sheets.googleapis.com' and request.method=='POST':
            body=json.loads(request.content);updates=[r.get('updateCells') for r in body.get('requests',[])]
            if len(updates)==1 and updates[0] and len(updates[0]['rows'][0]['values'])==4:
                self.expense_posts+=1;u=updates[0];point=u['start'];row=point['rowIndex']+1
                assert point=={'sheetId':501,'rowIndex':row-1,'columnIndex':0} and u['fields']=='userEnteredValue'
                assert u['rows']==[{'values':[{'userEnteredValue':{'stringValue':''}}]*4}]
                if self.edit_before_failure:raise httpx.ReadTimeout('Synthetic no-effect unknown',request=request)
                for col in range(1,5):self.google.cells[('All Expenses',row,col)]=''
                if self.edit_lost_ack:raise httpx.ReadTimeout('Synthetic removed acknowledgement loss',request=request)
                cells=[{'userEnteredValue':{'numberValue' if type(v) in {float,int} else 'stringValue':v}}
                    for v in [self.google.cells.get(('All Expenses',row,c),'') for c in range(1,6)]]
                return httpx.Response(200,json={'spreadsheetId':TARGET['PILOT_SPREADSHEET_ID'],'replies':[{}],
                    'updatedSpreadsheet':{'sheets':[{'properties':{'sheetId':501},'data':[{'startRow':row-1,'startColumn':0,'rowData':[{'values':cells}]}]}]}})
        return super().handle(request)


def turn(api,text,*answers,postback=False):
    with TestClient(api.app()) as client:return api.turn(client,text,*answers,postback=postback)


def context(body):return json.loads(body['input'][0]['content'])


def alias(operation='set',label='เพลง',item='Spotify'):
    def answer(body):
        c=context(body)
        return response('prepare_catalog_alias',{'operation':operation,'alias':label,'item':item,
            'source_refs':[c['current_source_ref']]})
    return answer


def delete_prepare(body):
    c=context(body)
    return response('prepare_saved_expense_delete',{'candidate_ref':c['saved_record_edits']['candidates'][0]['candidate_ref'],
        'source_refs':[c['current_source_ref']]})


def find_record(*,scope='date_range',name='Spotify',day='2026-10-01',literal='1 ต.ค.'):
    def answer(body):
        c=context(body)
        return response('find_saved_records',{'domain':'expense','scope':scope,'start_date':None if scope=='last' else day,
            'end_date':None if scope=='last' else day,'period_text':None if scope=='last' else literal,
            'name':name,'source_refs':[c['current_source_ref']]})
    return answer


def button(result,operation):
    actions=result.data['line_message']['contents']['footer']['contents']
    return next(row['action']['data'] for row in actions if row['action'].get('data','').startswith('r1|'+operation+'|'))


def review_button(result):
    return next(row['action']['data'] for row in result.data['line_message']['contents']['footer']['contents']
        if row['action'].get('data','').startswith('e1|') and 'cancel' not in row['action']['data'])


def legacy_offer(host,event,result):
    """Frozen v45 receipt wire fixture; never used by the current application."""
    from datetime import timedelta
    from line_assistant.expense_storage import storage_scope
    from line_assistant.history_context import digest
    from line_assistant.sheet_metadata import transaction_id
    from line_assistant.conversation_ui import render_actions
    from line_assistant.state import Result
    claim=host.state.claims.get(event);action=host.state.actions.get(claim.action_key) if claim else None
    if not host.record_actions.enabled or not action or action.status!='completed' or result.status!='recorded':return result
    if action.kind not in {'add_transactions','catalog_expense'}:return result
    records=[{'id':transaction_id(claim.action_key,i),**{k:e[k] for k in ['item','amount','date']}} for i,e in enumerate(action.payload['entries'])]
    value={'owner':host.settings.owner_id,'scope':storage_scope(host.sheets),'event':event,
        'ref':'record_'+digest([event,claim.action_key])[:20],'nonce':'a'*16,'records':records,
        'expires_at':(host.clock()+timedelta(seconds=host.settings.last_action_ttl_seconds)).isoformat()}
    value['seal']=digest(value)
    actions=[host.record_actions.button(op,value,'*' if len(records)>1 else '0') for op in ['amount','date','source','delete']]
    host.state.owner_features_used=True
    return Result(result.status,result.message,{**result.data,'record_actions':value,'line_message':render_actions(result.message,actions)})
