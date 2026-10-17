"""Synthetic saved-record provider contracts; no live writes or credentials."""
from copy import deepcopy
from datetime import date,datetime
from urllib.parse import unquote
import json,socket
import httpx,pytest
from line_assistant.adapters import Conflict,NotFound
from line_assistant.expense_edits import SavedExpenseEditor,expense_after
from line_assistant.calendar_reschedule import SavedCalendarEditor,rescheduled_times
from line_assistant.sheets_api import SheetsApi,SheetTarget
from line_assistant.sheet_metadata import TransactionIdMetadata
from line_assistant.sheet_dates import date_to_serial
from line_assistant.transport import RestClient,NotReady,OutcomeUnknown,WriteBlocked

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*a,**k):raise AssertionError('Saved edit provider tests are offline')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

class Sheets:
    def __init__(self):
        self.rows=[['Date','List','Cost','Remark','transaction_id','=header-formula'],
            [date_to_serial(date(2026,11,5)),'Metro Token',12.5,'note','synthetic_tx_a','=C2*2'],
            [date_to_serial(date(2026,11,5)),'Garden Lunch',8,'=literal-note-formula','synthetic_tx_b','=C3*2']]
        self.posts=0;self.lost_ack=False;self.before_send_failure=False;self.omit_grid=False;self.forbidden=False
    def handle(self,request):
        if request.method=='GET' and '/values/' not in request.url.path:
            return httpx.Response(200,json={'spreadsheetId':'synthetic-expense-edit','sheets':[
                {'properties':{'title':'All Expenses','sheetId':101,'gridProperties':{'rowCount':20,'columnCount':6}},'data':[{'startColumn':4,'columnMetadata':[{'hiddenByUser':True}]}]},
                {'properties':{'title':'Database','sheetId':102,'gridProperties':{'rowCount':20,'columnCount':3}}}]})
        if request.method=='GET':
            assert request.url.params['valueRenderOption']=='FORMULA'
            return httpx.Response(200,json={'values':[deepcopy(r[:5]) for r in self.rows]})
        assert request.method=='POST' and request.url.path.endswith(':batchUpdate');self.posts+=1
        if self.forbidden:return httpx.Response(403,json={})
        if self.before_send_failure:raise httpx.ReadTimeout('Synthetic unknown before write',request=request)
        body=json.loads(request.content)
        for entry in body['requests']:
            update=entry['updateCells'];start=update['start'];assert update['fields']=='userEnteredValue' and start['columnIndex']<=2
            self.rows[start['rowIndex']][start['columnIndex']]=next(iter(update['rows'][0]['values'][0]['userEnteredValue'].values()))
        if self.lost_ack:raise httpx.ReadTimeout('Synthetic committed then acknowledgement lost',request=request)
        row=body['requests'][0]['updateCells']['start']['rowIndex']
        cells=[{'userEnteredValue':{'numberValue' if type(x) in {float,int} else 'stringValue':x}} for x in self.rows[row][:5]]
        result={'spreadsheetId':'synthetic-expense-edit','replies':[{} for _ in body['requests']]}
        if not self.omit_grid:result['updatedSpreadsheet']={'sheets':[{'properties':{'sheetId':101},'data':[{'startRow':row,'startColumn':0,'rowData':[{'values':cells}]}]}]}
        return httpx.Response(200,json=result)
    def editor(self,enabled=True):
        target=SheetTarget('synthetic-expense-edit',max_rows=20,writes_enabled=True,input_access_verified=True,recovery='approved_id_column',transaction_id_metadata=TransactionIdMetadata('E',schema_approved=True))
        api=SheetsApi(RestClient('sheets',httpx.MockTransport(self.handle)),target)
        return SavedExpenseEditor(api,writes_enabled=enabled,single_writer_reviewed=enabled)

def expense_payload(editor,**changes):
    before=editor.get('synthetic_tx_a')
    return {'domain':'expense','id':before['id'],'before':before,'after':expense_after(before,changes or {'amount':'35.00'})}

def test_expense_update_preserves_id_note_format_and_other_formula_columns():
    fake=Sheets();editor=fake.editor();payload=expense_payload(editor,amount='35.00',date='2026-11-06',item='Garden Lunch');old=deepcopy(fake.rows);bindings=[]
    receipt=editor.apply(payload,'synthetic-action',bind_request=lambda body,proof:bindings.append((body,proof)))
    assert receipt['proof']=='ack_and_readback' and receipt['after']=={'item':'Garden Lunch','amount':'35.00','date':'2026-11-06'}
    assert fake.rows[1][3:]==old[1][3:] and fake.rows[2]==old[2] and len(bindings)==1 and fake.posts==1
    assert {r['updateCells']['start']['columnIndex'] for r in bindings[0][0]['requests']}=={0,1,2}

def test_expense_relocates_by_stable_id_when_rows_move_before_write():
    fake=Sheets();editor=fake.editor();payload=expense_payload(editor);fake.rows[1],fake.rows[2]=fake.rows[2],fake.rows[1]
    receipt=editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    assert receipt['row']==3 and fake.rows[1][2]==8 and fake.rows[2][2]==35

@pytest.mark.parametrize('mutation',['amount','note','id','duplicate','formula'])
def test_stale_missing_duplicate_or_formula_expense_refuses_write(mutation):
    fake=Sheets();editor=fake.editor();payload=expense_payload(editor)
    if mutation=='amount':fake.rows[1][2]=99
    if mutation=='note':fake.rows[1][3]='changed externally'
    if mutation=='id':fake.rows[1][4]='changed_identity'
    if mutation=='duplicate':fake.rows[2][4]=fake.rows[1][4]
    if mutation=='formula':fake.rows[1][2]='=SUM(C3)'
    with pytest.raises((Conflict,NotFound,NotReady)):editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    assert fake.posts==0

def test_legacy_record_without_id_is_not_silently_assigned_an_id():
    fake=Sheets();fake.rows[1][4]='';editor=fake.editor();before=deepcopy(fake.rows)
    result=editor.records(date(2026,11,1),date(2026,11,30))
    assert result['unidentified_count']==1 and len(result['records'])==1 and fake.rows==before and fake.posts==0

def test_expense_flags_and_reviewed_policy_are_required():
    fake=Sheets();off=fake.editor(False);payload=expense_payload(off)
    with pytest.raises(WriteBlocked):off.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    with pytest.raises(ValueError):SavedExpenseEditor(off.sheets,writes_enabled=True,single_writer_reviewed=False)
    assert fake.posts==0

def test_expense_lost_ack_reconciles_desired_state_without_another_write():
    fake=Sheets();editor=fake.editor();payload=expense_payload(editor);fake.lost_ack=True
    with pytest.raises(OutcomeUnknown):editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    proof=editor.reconcile('synthetic-action',payload)
    assert proof.outcome=='committed' and proof.receipt['proof']=='observed_desired_state' and fake.posts==1

def test_old_expense_state_after_timeout_does_not_authorize_retry():
    fake=Sheets();editor=fake.editor();payload=expense_payload(editor);fake.before_send_failure=True
    with pytest.raises(OutcomeUnknown):editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    assert editor.reconcile('synthetic-action',payload).outcome=='unknown' and fake.posts==1

def test_expense_missing_ack_grid_is_uncertain_not_success():
    fake=Sheets();editor=fake.editor();payload=expense_payload(editor);fake.omit_grid=True
    with pytest.raises(OutcomeUnknown):editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    assert editor.reconcile('synthetic-action',payload).outcome=='committed' and fake.posts==1

def test_expense_permission_failure_does_not_expand_access_or_retry():
    fake=Sheets();editor=fake.editor();payload=expense_payload(editor);fake.forbidden=True
    with pytest.raises(WriteBlocked):editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    assert fake.posts==1 and fake.rows[1][2]==12.5

class Calendar:
    def __init__(self,all_day=False):
        self.event={'id':'synthetic_saved_event','etag':'"version-1"','summary':'นัดเตรียมของจำลอง','start':{'date':'2026-11-05'} if all_day else {'dateTime':'2026-11-05T09:00:00+07:00'},'end':{'date':'2026-11-07'} if all_day else {'dateTime':'2026-11-05T10:00:00+07:00'},'reminders':{'useDefault':False,'overrides':[{'method':'popup','minutes':10}]},'extendedProperties':{'private':{'keep':'preserved'},'shared':{'also':'preserved'}}}
        self.patches=0;self.committed=0;self.race=False;self.lost_ack=False;self.before_send_failure=False
    def handle(self,request):
        if request.method=='GET':return httpx.Response(200,json=deepcopy(self.event))
        assert request.method=='PATCH';self.patches+=1
        assert dict(request.url.params)=={'sendUpdates':'none'}
        if self.race:self.event['etag']='"external-version"'
        if request.headers['If-Match']!=self.event['etag']:return httpx.Response(412,json={})
        if self.before_send_failure:raise httpx.ReadTimeout('Synthetic unknown before write',request=request)
        body=json.loads(request.content);assert set(body)=={'start','end','extendedProperties'}
        self.event.update(start=body['start'],end=body['end']);self.event['extendedProperties']['private']=body['extendedProperties']['private'];self.event['etag']='"version-2"';self.committed+=1
        if self.lost_ack:raise httpx.ReadTimeout('Synthetic committed then acknowledgement lost',request=request)
        return httpx.Response(200,json=deepcopy(self.event))
    def editor(self,enabled=True):return SavedCalendarEditor(RestClient('calendar',httpx.MockTransport(self.handle)),'synthetic-calendar',writes_enabled=enabled)

def calendar_payload(editor,**changes):
    before=editor.get('synthetic_saved_event')
    return {'domain':'calendar','id':before['id'],'before':before,'after':rescheduled_times(before,changes or {'date':'2026-11-09'})}

@pytest.mark.parametrize('all_day',[False,True])
def test_conditional_reschedule_preserves_duration_title_reminders_and_other_properties(all_day):
    fake=Calendar(all_day);editor=fake.editor();payload=calendar_payload(editor);original=deepcopy(fake.event);bindings=[]
    result=editor.apply(payload,'synthetic-action',bind_request=lambda *a:bindings.append(a))
    assert result['after']['start'].startswith('2026-11-09') and fake.event['summary']==original['summary']
    assert fake.event['reminders']==original['reminders'] and fake.event['extendedProperties']['shared']==original['extendedProperties']['shared']
    assert fake.event['extendedProperties']['private']['keep']=='preserved' and fake.patches==fake.committed==1
    assert len(bindings)==1 and result['proof']=='conditional_ack'

def test_calendar_etag_race_returns_conflict_and_never_rebases_without_review():
    fake=Calendar();editor=fake.editor();payload=calendar_payload(editor);fake.race=True
    with pytest.raises(Conflict):editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    assert fake.patches==1 and fake.committed==0 and fake.event['start']['dateTime']=='2026-11-05T09:00:00+07:00'

@pytest.mark.parametrize('unsupported',['attendees','recurrence','recurringEventId','conferenceData','eventType','etag'])
def test_unsupported_calendar_features_or_missing_version_fail_closed(unsupported):
    fake=Calendar();fake.event[unsupported]={'attendees':[{'email':'guest@example.test'}],'recurrence':['RRULE:FREQ=DAILY'],'recurringEventId':'synthetic-parent','conferenceData':{'conferenceId':'synthetic'},'eventType':'birthday','etag':None}[unsupported]
    with pytest.raises(NotReady):fake.editor().get('synthetic_saved_event')
    assert fake.patches==0

def test_calendar_unknown_commit_reconciles_positive_marker_without_retry():
    fake=Calendar();editor=fake.editor();payload=calendar_payload(editor);fake.lost_ack=True
    with pytest.raises(OutcomeUnknown):editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    proof=editor.reconcile('synthetic-action',payload)
    assert proof.outcome=='committed' and fake.patches==fake.committed==1

def test_calendar_old_state_after_unknown_attempt_never_proves_not_committed():
    fake=Calendar();editor=fake.editor();payload=calendar_payload(editor);fake.before_send_failure=True
    with pytest.raises(OutcomeUnknown):editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    assert editor.reconcile('synthetic-action',payload).outcome=='unknown' and fake.patches==1 and fake.committed==0

def test_calendar_defaults_off_and_forbids_deletion_or_mode_change():
    fake=Calendar(True);editor=fake.editor(False);payload=calendar_payload(editor)
    with pytest.raises(WriteBlocked):editor.apply(payload,'synthetic-action',bind_request=lambda *a:None)
    with pytest.raises(ValueError):rescheduled_times(payload['before'],{'time':'09:00'})
    with pytest.raises(ValueError):rescheduled_times(payload['before'],{'delete':True})
    assert fake.patches==0
