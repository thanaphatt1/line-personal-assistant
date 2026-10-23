"""Provider atomicity, source evidence, and persisted vocabulary boundaries."""
from copy import deepcopy
from datetime import date,timedelta
import json,socket
import httpx,pytest

from line_assistant.adapters import Conflict,NotFound
from line_assistant.catalog import normalized
from line_assistant.expense_edits import SavedExpenseEditor
from line_assistant.firestore_expenses import FirestoreSavedExpenseEditor,unfields
from line_assistant.history_context import HistoryState,digest
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.runtime_config import ConfigError
from line_assistant.history_cloud_run import HistoryConfig
from line_assistant.transport import OutcomeUnknown,NotReady,WriteBlocked
from owner_features_hosted import OwnerHosted,FLAGS,turn,alias
from test_saved_record_providers import Sheets
from test_expense_storage import setup,transaction
from test_calendar_factory import OWNER
from line_assistant.pilot_http import rpc_budget
from line_assistant.state import Result
from line_assistant.catalog_aliases import CatalogAliases


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Provider boundaries use synthetic HTTP only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


class RemoveSheets(Sheets):
    def handle(self,request):
        if request.method!='POST':return super().handle(request)
        self.posts+=1;body=json.loads(request.content);u=body['requests'][0]['updateCells'];start=u['start'];row=start['rowIndex']
        assert len(body['requests'])==1 and start['columnIndex']==0 and u['fields']=='userEnteredValue'
        assert u['rows']==[{'values':[{'userEnteredValue':{'stringValue':''}}]*4}]
        if self.before_send_failure:raise httpx.ReadTimeout('Synthetic unknown before effect',request=request)
        self.rows[row][:4]=['']*4
        if self.lost_ack:raise httpx.ReadTimeout('Synthetic unknown after effect',request=request)
        cells=[{'userEnteredValue':{'stringValue':v}} for v in self.rows[row][:5]]
        result={'spreadsheetId':'synthetic-expense-edit','replies':[{}]}
        if not self.omit_grid:result['updatedSpreadsheet']={'sheets':[{'properties':{'sheetId':101},'data':[{'startRow':row,'startColumn':0,'rowData':[{'values':cells}]}]}]}
        return httpx.Response(200,json=result)


def sheets_setup():
    fake=RemoveSheets();base=fake.editor()
    return fake,SavedExpenseEditor(base.sheets,writes_enabled=True,single_writer_reviewed=True,delete_enabled=True)


def removal(editor,identifier):
    before=editor.get(identifier)
    return {'domain':'expense','id':identifier,'operation':'delete','before':before,'after':None,'changes':{'operation':'delete'}}


def test_sheet_removal_preserves_tombstone_other_formulas_and_relocates_by_id():
    fake,editor=sheets_setup();p=removal(editor,'synthetic_tx_a');other=deepcopy(fake.rows[2]);formula=fake.rows[1][5]
    fake.rows[1],fake.rows[2]=fake.rows[2],fake.rows[1]
    result=editor.apply(p,'remove',bind_request=lambda *args:None)
    assert result['after'] is None and result['row']==3 and fake.rows[1]==other
    assert fake.rows[2]==['','','','','synthetic_tx_a',formula]
    assert editor.records(date(2026,11,1),date(2026,11,30))['records'][0]['id']=='synthetic_tx_b'


@pytest.mark.parametrize('mode',['missing','duplicate','changed','formula','disabled'])
def test_sheet_removal_rejects_stale_invalid_or_disabled_target_without_write(mode):
    fake,editor=sheets_setup();p=removal(editor,'synthetic_tx_a')
    if mode=='missing':fake.rows.pop(1)
    elif mode=='duplicate':fake.rows[2][4]='synthetic_tx_a'
    elif mode=='changed':fake.rows[1][3]='changed note'
    elif mode=='formula':fake.rows[1][1]='=B3'
    else:editor.delete_enabled=False
    with pytest.raises((Conflict,NotFound,NotReady,WriteBlocked)):editor.apply(p,'remove',bind_request=lambda *args:None)
    assert fake.posts==0


def test_missing_row_after_unknown_is_not_positive_removal_evidence():
    fake,editor=sheets_setup();p=removal(editor,'synthetic_tx_a');fake.before_send_failure=True
    with pytest.raises(OutcomeUnknown):editor.apply(p,'remove',bind_request=lambda *args:None)
    fake.rows.pop(1)
    assert editor.reconcile('remove',p).outcome=='unknown' and fake.posts==1


@pytest.mark.parametrize('mode',['lost_ack','omit_grid'])
def test_sheet_positive_removal_recovery_never_posts_again(mode):
    fake,editor=sheets_setup();p=removal(editor,'synthetic_tx_a');setattr(fake,mode,True)
    with pytest.raises(OutcomeUnknown):editor.apply(p,'remove',bind_request=lambda *args:None)
    proof=editor.reconcile('remove',p)
    assert proof.outcome=='committed' and proof.receipt['after'] is None and fake.posts==1


def test_firestore_delete_updates_count_and_durable_receipt_atomically():
    fake,store=setup();identifier,keep=store.add([transaction(),transaction(amount='2')],'append')
    editor=FirestoreSavedExpenseEditor(store,writes_enabled=True,delete_enabled=True);p=removal(editor,identifier)
    result=editor.apply(p,'remove',bind_request=lambda *args:None)
    assert result['after'] is None and result['proof']=='transaction_receipt'
    assert unfields(fake.documents[store.target.parent])['expense_count']==1
    assert store.classification_entries()[0]['transaction_id']==keep
    assert editor.reconcile('remove',p).receipt==result and fake.commits==2


@pytest.mark.parametrize('field',['expense','catalog','metadata'])
def test_firestore_delete_conflict_keeps_expense_metadata_and_receipt_atomic(field):
    fake,store=setup();identifier=store.add([transaction()],'append')[0]
    editor=FirestoreSavedExpenseEditor(store,writes_enabled=True,delete_enabled=True);p=removal(editor,identifier)
    name=store.target.parent+('/expenses/'+identifier if field=='expense' else '/catalog/'+store.catalog_id('Spotify') if field=='catalog' else '')
    def change(server):server.put(name,unfields(server.documents[name]))
    fake.before_commit=change
    with pytest.raises(Conflict):editor.apply(p,'remove',bind_request=lambda *args:None)
    assert store.target.parent+'/expenses/'+identifier in fake.documents
    assert unfields(fake.documents[store.target.parent])['expense_count']==1
    assert editor.reconcile('remove',p).outcome=='unknown' and fake.commits==1


@pytest.mark.parametrize('mode',['before_ack','lost_ack','corrupt_ack'])
def test_firestore_removal_unknown_requires_receipt_and_never_repeats_commit(mode):
    fake,store=setup();identifier=store.add([transaction()],'append')[0]
    editor=FirestoreSavedExpenseEditor(store,writes_enabled=True,delete_enabled=True);p=removal(editor,identifier);setattr(fake,mode,True)
    with pytest.raises(OutcomeUnknown):editor.apply(p,'remove',bind_request=lambda *args:None)
    assert editor.reconcile('remove',p).outcome==('unknown' if mode=='before_ack' else 'committed')
    before=fake.commits
    if mode=='before_ack':
        with pytest.raises(OutcomeUnknown):editor.apply(p,'remove',bind_request=lambda *args:None)
    else:assert editor.apply(p,'remove',bind_request=lambda *args:None)['after'] is None
    assert fake.commits==before


@pytest.mark.parametrize('field',['owner','scope','seal','alias_key','time','format'])
def test_invalid_persisted_alias_extension_is_rejected_atomically(field):
    state=HistoryState();state.owner_id=OWNER;state.workbook_id='synthetic-book';state.owner_features_used=True
    state.catalog_aliases={'เพลง':{'alias':'เพลง','item':'Spotify','source_refs':['synthetic-source'],'confirmed_at':'2026-10-23T10:00:00+07:00'}}
    original=history_snapshot(state);bad=deepcopy(original);v=bad['owner_features_v1']
    if field in {'owner','scope','seal'}:v[field]='foreign'
    elif field=='alias_key':v['aliases']['different']=v['aliases'].pop('เพลง')
    elif field=='time':v['aliases']['เพลง']['confirmed_at']='2026-10-23T10:00:00'
    else:bad['format']='local-history-state-v1'
    if field not in {'seal','format'}:v['seal']=digest({k:x for k,x in v.items() if k!='seal'})
    with pytest.raises(ValueError):restore_history(state,bad,OWNER)
    assert history_snapshot(state)==original


@pytest.mark.parametrize('key',FLAGS)
@pytest.mark.parametrize('value',['yes',True,'1'])
def test_new_flags_require_explicit_boolean_strings(key,value):
    api=OwnerHosted()
    with pytest.raises(ConfigError):HistoryConfig.from_mapping({**api.configuration(),key:value})


@pytest.mark.parametrize('key',[FLAGS[0],FLAGS[2]])
def test_delete_and_record_buttons_require_existing_saved_edit_policy(key):
    api=OwnerHosted();configuration=api.configuration()
    configuration.update({k:'false' for k in FLAGS});configuration[key]='true'
    configuration['PILOT_SAVED_EXPENSE_EDITS_ENABLED']='false'
    with pytest.raises(ConfigError):HistoryConfig.from_mapping(configuration)


def test_confirmed_alias_action_recovers_without_turn_local_context(monkeypatch):
    api=OwnerHosted();turn(api,'จำว่า เพลง หมายถึง Spotify',alias())
    original=CatalogAliases.resume
    with monkeypatch.context() as patch:
        patch.setattr(CatalogAliases,'resume',lambda self,key:Result('uncertain','synthetic stopped after prepared checkpoint'))
        turn(api,'ยืนยัน')
    app=api.app();host=app.state.assistant
    assert not host._current
    with rpc_budget(30),host.state.session():
        key=next(k for k,a in host.state.actions.items() if a.kind=='change_catalog_alias')
        expected=host.state.actions[key].metadata['confirmed_at']
        result=original(host.catalog_aliases,key)
        assert result.status=='recorded' and host.state.catalog_aliases['เพลง']['confirmed_at']==expected
        assert original(host.catalog_aliases,key)==result
    app.state.pilot_http.close()
    assert api.expense_writes==0


def test_alias_list_is_paginated_and_line_text_remains_within_utf16_limit():
    api=OwnerHosted();app=api.app();host=app.state.assistant
    with rpc_budget(20),host.state.session():
        for i in range(100):
            label=str(i)+'😀'*190
            host.state.catalog_aliases[normalized(label)]={'alias':label,'item':'🧪'*199,'source_refs':['synthetic'],
                'confirmed_at':'2026-10-23T00:00:00+07:00'}
        all_rows=[]
        for page in range(1,11):
            value=host.catalog_aliases.tool('list_catalog_aliases',{'page':page})
            assert value['count']==100 and value['page_count']==10 and len(value['aliases'])==10
            assert len(value['message'].encode('utf-16-le'))//2<=5000
            all_rows+=value['aliases']
        assert len({r['alias'] for r in all_rows})==100
    app.state.pilot_http.close()
