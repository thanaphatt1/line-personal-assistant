"""Signed, cold-restored saved edits against synthetic HTTP only."""
from copy import deepcopy
from datetime import date,timedelta
from urllib.parse import unquote
import json,socket
import httpx,pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from line_assistant.history_cloud_run import create_history_cloud_run_app,HistoryConfig
from line_assistant.history_context import digest
from line_assistant.sheet_dates import date_to_serial
from line_assistant.pilot_http import rpc_budget
from line_assistant.runtime_config import ConfigError
from line_assistant.transport import LiveDisabled
from test_deployment_configuration import ConfiguredHosted,TARGET
from test_calendar_factory import values,CALENDAR
from test_calendar_mvp import response
from test_saved_record_providers import Calendar

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Saved record workflows use synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

class SavedHosted(ConfiguredHosted):
    def __init__(self):
        self.edit_flags=True;self.expense_posts=0;self.edit_lost_ack=False;self.edit_before_failure=False
        super().__init__();self.saved_calendar=Calendar()
        for col,value in enumerate([date_to_serial(date(2026,11,5)),'กาแฟ',12.5,'=C2*2','synthetic_saved_a','=C2*3'],1):self.google.cells[('All Expenses',2,col)]=value
        for col,value in enumerate(['ชา','Food',''],1):self.google.cells[('Database',3,col)]=value
    def app(self,enabled=True):
        flags={'PILOT_SAVED_EXPENSE_EDITS_ENABLED':'true','PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED':'true','PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY':'reviewed_single_writer'} if self.edit_flags else {}
        return create_history_cloud_run_app({**values(enabled),**TARGET,**flags},transport=httpx.MockTransport(self.handle),secret_loader=lambda name:SecretStr('synthetic-'+name),clock=lambda:self.now)
    def handle(self,request):
        if self.configured and request.url.host=='www.googleapis.com':
            if request.method=='GET' and unquote(request.url.path).endswith('/events'):
                return httpx.Response(200,json={'items':[deepcopy(self.saved_calendar.event)]})
            return self.saved_calendar.handle(request)
        if self.configured and request.url.host=='sheets.googleapis.com' and request.method=='POST':
            body=json.loads(request.content)
            if any('updateCells' in x for x in body.get('requests',[])):
                self.expense_posts+=1
                if self.edit_before_failure:raise httpx.ReadTimeout('Synthetic unknown before effect',request=request)
                row=None
                for entry in body['requests']:
                    u=entry['updateCells'];point=u['start'];assert point['sheetId']==501 and point['columnIndex']<=2 and u['fields']=='userEnteredValue'
                    row=point['rowIndex']+1
                    self.google.cells[('All Expenses',row,point['columnIndex']+1)]=next(iter(u['rows'][0]['values'][0]['userEnteredValue'].values()))
                if self.edit_lost_ack:raise httpx.ReadTimeout('Synthetic acknowledgement lost after effect',request=request)
                cells=[{'userEnteredValue':{'numberValue' if type(x) in {int,float} else 'stringValue':x}} for x in [self.google.cells.get(('All Expenses',row,c),'') for c in range(1,6)]]
                return httpx.Response(200,json={'spreadsheetId':TARGET['PILOT_SPREADSHEET_ID'],'replies':[{} for _ in body['requests']],
                    'updatedSpreadsheet':{'sheets':[{'properties':{'sheetId':501},'data':[{'startRow':row-1,'startColumn':0,'rowData':[{'values':cells}]}]}]}})
        return super().handle(request)

def turn(api,text,*answers):
    with TestClient(api.app()) as client:return api.turn(client,text,*answers)
def context(body):return json.loads(body['input'][0]['content'])
def find(domain='expense',name=None):
    def model(body):
        c=context(body)
        return response('find_saved_records',{'domain':domain,'scope':'date_range','start_date':'2026-11-05','end_date':'2026-11-05','period_text':'5 พ.ย.','name':name,'source_refs':[c['current_source_ref']]})
    return model
def prepare(domain='expense',**changes):
    def model(body):
        c=context(body);view=c['saved_record_edits'];candidate=view['candidates'][0]['candidate_ref']
        fields=['item','amount','date'] if domain=='expense' else ['date','time','end_date','end_time']
        args={**dict.fromkeys(fields+[x+'_text' for x in fields]),'candidate_ref':candidate,'source_refs':[c['current_source_ref']],**changes}
        return response('prepare_saved_expense_edit' if domain=='expense' else 'prepare_calendar_reschedule',args)
    return model
def preview(api,domain='expense'):
    if domain=='expense':return turn(api,'แก้รายจ่ายกาแฟวันที่ 5 พ.ย. เป็น 35 บาท',find(),prepare(amount='35',amount_text='35'))
    return turn(api,'เลื่อนนัดวันที่ 5 พ.ย. ไปวันที่ 9 พ.ย.',find('calendar'),prepare('calendar',date='2026-11-09',date_text='9 พ.ย.'))

@pytest.mark.parametrize('domain',['expense','calendar'])
def test_signed_review_cold_confirm_replay_preserves_other_fields(domain):
    api=SavedHosted();before=deepcopy(api.google.cells);original=deepcopy(api.saved_calendar.event)
    review,_=preview(api,domain);assert review.status=='preview',review
    assert api.expense_posts==api.saved_calendar.patches==0 and len(api.model_bodies)==2
    assert 'ก่อน:' in review.message and 'หลัง:' in review.message
    saved,event=turn(api,'ยืนยัน');assert saved.status=='updated',saved
    assert api.expense_posts+api.saved_calendar.patches==1
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.expense_posts+api.saved_calendar.patches==1 and len(api.model_bodies)==2
    if domain=='expense':
        assert api.google.cells[('All Expenses',2,3)]==35
        assert all(api.google.cells[k]==v for k,v in before.items() if k!=('All Expenses',2,3))
    else:
        assert api.saved_calendar.event['reminders']==original['reminders']
        assert api.saved_calendar.event['extendedProperties']['shared']==original['extendedProperties']['shared']
        assert api.saved_calendar.event['end']['dateTime']=='2026-11-09T10:00:00+07:00'

@pytest.mark.parametrize('domain',['expense','calendar'])
def test_undelivered_or_intervening_reply_cannot_confirm(domain):
    api=SavedHosted();api.reject_reply=True;preview(api,domain);api.reject_reply=False
    result,_=turn(api,'ยืนยัน');assert result.status=='stale' and api.expense_posts==api.saved_calendar.patches==0

@pytest.mark.parametrize('domain',['expense','calendar'])
def test_stale_external_version_needs_new_review(domain):
    api=SavedHosted();preview(api,domain)
    if domain=='expense':api.google.cells[('All Expenses',2,3)]=99
    else:api.saved_calendar.event['etag']='"changed"'
    result,_=turn(api,'ยืนยัน');assert result.status=='conflict',result
    assert api.expense_posts==api.saved_calendar.patches==0
    again,_=turn(api,'ตรวจรายการใหม่',response('review_saved_record_change',{}));assert again.status=='preview',again
    assert ('99.00' in again.message) if domain=='expense' else True
    result,_=turn(api,'ยืนยัน');assert result.status=='updated',result

@pytest.mark.parametrize('domain',['expense','calendar'])
def test_unknown_ack_reconciles_read_only_even_when_feature_is_off(domain):
    api=SavedHosted();preview(api,domain)
    if domain=='expense':api.edit_lost_ack=True
    else:api.saved_calendar.lost_ack=True
    result,_=turn(api,'ยืนยัน');assert result.status=='uncertain',result
    api.edit_flags=False
    result,_=turn(api,'ตรวจผลการแก้ไข',response('check_saved_record_change',{}))
    assert result.data['tool_result']['status']=='saved_edit_status',result
    assert api.expense_posts+api.saved_calendar.patches==1
    assert all(a['status']=='completed' for a in api.snapshot()['actions'].values())

@pytest.mark.parametrize('domain',['expense','calendar'])
def test_old_looking_state_after_unknown_never_retries(domain):
    api=SavedHosted();preview(api,domain)
    if domain=='expense':api.edit_before_failure=True
    else:api.saved_calendar.before_send_failure=True
    result,_=turn(api,'ยืนยัน');assert result.status=='uncertain'
    result,_=turn(api,'ตรวจผลการแก้ไข',response('check_saved_record_change',{}))
    assert api.expense_posts+api.saved_calendar.patches==1
    assert any(a['status']=='uncertain' for a in api.snapshot()['actions'].values())

def test_ambiguous_candidate_requires_delivered_choice_and_current_money_not_ordinal():
    api=SavedHosted()
    for col,value in enumerate([date_to_serial(date(2026,11,5)),'กาแฟ',18,'','synthetic_saved_b'],1):api.google.cells[('All Expenses',3,col)]=value
    selection,_=turn(api,'แก้รายจ่ายกาแฟวันที่ 5 พ.ย. เป็น 35 บาท',find());assert selection.status=='clarification' and len(api.model_bodies)==1
    result,_=turn(api,'รายการที่ 1 เป็น 35 บาท',prepare(amount='35',amount_text='35'));assert result.status=='preview',result
    assert api.expense_posts==0
    bad,_=turn(api,'รายการที่ 1',prepare(amount='1',amount_text='1'));assert bad.status!='preview'
    result,_=turn(api,'ยืนยัน');assert result.status!='updated' and api.expense_posts==0

def test_catalog_change_revokes_confirmation_and_old_nonce_cannot_be_reused():
    api=SavedHosted();review,_=preview(api)
    parts=review.data['line_message']['contents']['footer']['contents'][0]['action']['data'].split('|')
    command='ยืนยันแก้ '+parts[2]+' '+parts[3]
    api.google.cells[('Database',2,2)]='Changed'
    stale,_=turn(api,'ยืนยัน');assert stale.status=='stale' and not api.expense_posts
    fresh,_=turn(api,'ตรวจรายการใหม่',response('review_saved_record_change',{}));assert fresh.status=='preview'
    stale,_=turn(api,command);assert stale.status=='stale' and not api.expense_posts

def test_off_flag_keeps_pending_but_cannot_start_effect():
    api=SavedHosted();preview(api);pending=deepcopy(api.snapshot()['pending']);api.edit_flags=False
    result,_=turn(api,'ยืนยัน');assert result.status=='setup_required' and not api.expense_posts
    assert api.snapshot()['pending']==pending

@pytest.mark.parametrize('domain',['expense','calendar'])
def test_expired_pending_cannot_confirm(domain):
    api=SavedHosted();preview(api,domain);api.now+=timedelta(days=2)
    result,_=turn(api,'ยืนยัน');assert result.status!='updated' and api.expense_posts==api.saved_calendar.patches==0

def test_calendar_atomic_etag_race_is_no_write_conflict():
    api=SavedHosted();preview(api,'calendar');api.saved_calendar.race=True
    result,_=turn(api,'ยืนยัน');assert result.status=='conflict' and api.saved_calendar.patches==1 and not api.saved_calendar.committed

def test_review_amendment_retains_previous_proposed_date_and_revokes_nonce():
    api=SavedHosted()
    first,_=turn(api,'แก้กาแฟวันที่ 5 พ.ย. ไป 6 พ.ย.',find(),prepare(date='2026-11-06',date_text='6 พ.ย.'))
    assert first.status=='preview',first
    second,_=turn(api,'ยอดเป็น 35 บาท',prepare(amount='35',amount_text='35'))
    assert second.status=='preview' and '6 พ.ย. 2026' in second.message and '35.00' in second.message,second
    parts=first.data['line_message']['contents']['footer']['contents'][0]['action']['data'].split('|')
    old='ยืนยันแก้ '+parts[2]+' '+parts[3]
    stale,_=turn(api,old);assert stale.status=='stale' and not api.expense_posts

@pytest.mark.parametrize('domain',['expense','calendar'])
def test_intervening_read_status_is_not_a_delivered_review(domain):
    api=SavedHosted();preview(api,domain)
    turn(api,'ตรวจผล',response('check_saved_record_change',{}))
    stale,_=turn(api,'ยืนยัน');assert stale.status=='stale' and api.expense_posts==api.saved_calendar.patches==0

@pytest.mark.parametrize('change',['owner','workbook','seal'])
def test_foreign_or_modified_persisted_selection_cannot_confirm(change):
    api=SavedHosted();preview(api)
    app=api.app()
    with rpc_budget(20),app.state.assistant.state.session():
        v=app.state.assistant.state.pending.payload
        v[change]='foreign-value';app.state.assistant.state.checkpoint()
    app.state.pilot_http.close()
    stale,_=turn(api,'ยืนยัน');assert stale.status=='stale' and not api.expense_posts

def test_other_uncertain_action_blocks_confirmed_saved_edit():
    from line_assistant.state import Action
    api=SavedHosted();preview(api);app=api.app()
    with rpc_budget(20),app.state.assistant.state.session():
        s=app.state.assistant.state;s.actions['unrelated']=Action('add_transactions',{'entries':[]},{},api.now+timedelta(hours=1),api.now+timedelta(days=1),status='uncertain');s.checkpoint()
    app.state.pilot_http.close()
    result,_=turn(api,'ยืนยัน');assert result.status=='uncertain' and not api.expense_posts

@pytest.mark.parametrize('domain',['expense','calendar'])
def test_model_cannot_turn_prepare_or_find_into_confirmation(domain):
    api=SavedHosted();review,_=preview(api,domain)
    result,_=turn(api,'ช่วยตรวจเฉย ๆ',response('confirm_saved_record_change',{'confirmed':True}))
    assert result.status=='model_unavailable' and api.expense_posts==api.saved_calendar.patches==0

def test_default_flags_expose_no_saved_write_tools():
    api=SavedHosted();api.edit_flags=False
    app=api.app();assert not app.state.assistant.saved_edits.definitions()
    assert not app.state.pilot_http.config.saved_expense_enabled and not app.state.pilot_http.config.saved_calendar_enabled
    app.state.pilot_http.close()

@pytest.mark.parametrize('citation,accepted',[('35 บาท',True),('35',True),('3',False),('5 พ.ย.',False)])
def test_literal_money_citation_can_include_unit_but_not_a_digit_fragment_or_date(citation,accepted):
    api=SavedHosted();r,_=turn(api,'แก้รายจ่ายกาแฟวันที่ 5 พ.ย. เป็น 35 บาท',find(),prepare(amount='35',amount_text=citation))
    assert (r.status=='preview') is accepted and not api.expense_posts

@pytest.mark.parametrize('changes',[{'date':'2026-11-09','date_text':'5 พ.ย.'},
    {'end_date':'2026-11-11','end_date_text':'คงจำนวนวันเท่าเดิม'},
    {'time':'15:00','time_text':'บ่ายสอง'}])
def test_date_and_clock_values_must_match_actual_owner_evidence(changes):
    api=SavedHosted();r,_=turn(api,'เลื่อนนัดวันที่ 5 พ.ย. ไป 9 พ.ย. บ่ายสอง คงจำนวนวันเท่าเดิม',find('calendar'),prepare('calendar',**changes))
    assert r.status!='preview' and not api.saved_calendar.patches

def test_all_day_move_uses_original_duration_without_model_end_calculation():
    api=SavedHosted();api.saved_calendar=Calendar(True)
    r,_=turn(api,'เลื่อนนัดวันที่ 5 พ.ย. ไป 9 พ.ย. คงจำนวนวันเท่าเดิม',find('calendar'),prepare('calendar',date='2026-11-09',date_text='9 พ.ย.'))
    assert r.status=='preview' and api.snapshot()['pending']['payload']['after']['end']=='2026-11-11'

def test_echoed_unchanged_saved_date_does_not_require_a_new_date_citation():
    api=SavedHosted();r,_=turn(api,'เลื่อนนัดวันที่ 5 พ.ย. เป็นบ่ายสองวันเดิม',find('calendar'),prepare('calendar',date='2026-11-05',time='14:00',time_text='บ่ายสอง'))
    assert r.status=='preview' and api.snapshot()['pending']['payload']['after']['end']=='2026-11-05T15:00:00+07:00'

@pytest.mark.parametrize('change',[{'PILOT_SAVED_EXPENSE_EDITS_ENABLED':'true'},
    {'PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED':'true','PILOT_CALENDAR_ENABLED':'false'},
    {'PILOT_SAVED_EXPENSE_EDITS_ENABLED':'yes'},{'PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY':'guess'}])
def test_explicit_config_and_concurrency_policy_required(change):
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**values(),**TARGET,**change})

def test_unconfirmed_mutations_and_headers_denied_before_token_lookup():
    api=SavedHosted();app=api.app();http=app.state.pilot_http;before=len(api.observed)
    with rpc_budget(20),app.state.assistant.state.session():
        for headers in [None,{'If-Match':'"forged"'},{'Authorization':'Bearer forged'}]:
            with pytest.raises(LiveDisabled):http.request('calendar','PATCH','/calendar/v3/calendars/'+CALENDAR+'/events/synthetic_saved_event',body={},params={'sendUpdates':'none'},headers=headers)
        with pytest.raises(LiveDisabled):http.request('sheets','POST','/v4/spreadsheets/'+TARGET['PILOT_SPREADSHEET_ID']+':batchUpdate',body={'requests':[{'updateCells':{}}]})
    assert not api.saved_calendar.patches and not api.expense_posts
    assert not any('calendar.events' in str(s) for s in api.scopes)
