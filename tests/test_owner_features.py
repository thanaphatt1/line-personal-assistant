"""Cold-restored LINE controls, reviewed removal, and durable owner vocabulary."""
from copy import deepcopy
from datetime import timedelta
import socket
import pytest
from fastapi.testclient import TestClient

from line_assistant.pilot_http import rpc_budget
from owner_features_hosted import OwnerHosted,turn,alias,delete_prepare,find_record,button,context,FLAGS
from test_calendar_mvp import response
from test_previous_expense_prices import prepare
from test_saved_record_workflow import prepare as edit


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**kw):raise AssertionError('Owner features use synthetic HTTP only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


@pytest.mark.parametrize('backend',['sheets','firestore'])
def test_reviewed_delete_cold_confirm_duplicate_and_other_fields(backend):
    api=OwnerHosted(backend);before=deepcopy(api.google.cells)
    review,_=turn(api,'ลบรายจ่าย Spotify วันที่ 1 ต.ค.',find_record(),delete_prepare)
    assert review.status=='preview',review
    assert '169.00' in review.message and api.expense_writes==0
    saved,event=turn(api,'ยืนยัน');assert saved.status=='deleted',saved
    assert api.expense_writes==1
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.expense_writes==1 and len(api.model_bodies)==2
    if backend=='sheets':
        assert [api.google.cells[('All Expenses',3,col)] for col in range(1,5)]==['']*4
        assert all(api.google.cells[key]==value for key,value in before.items() if key[:2]!=('All Expenses',3) or key[2]>4)
    else:
        assert api.business_target.parent+'/expenses/synthetic_price_3' not in api.business.documents


@pytest.mark.parametrize('backend',['sheets','firestore'])
@pytest.mark.parametrize('committed',[True,False])
def test_unknown_removal_reconciles_without_second_attempt_even_flag_off(backend,committed):
    api=OwnerHosted(backend)
    turn(api,'ลบ Spotify วันที่ 1 ต.ค.',find_record(),delete_prepare)
    if backend=='sheets':
        api.edit_lost_ack=committed;api.edit_before_failure=not committed
    else:api.business.lost_ack=committed;api.business.before_ack=not committed
    result,_=turn(api,'ยืนยัน');assert result.status=='uncertain',result
    attempts=api.expense_posts if backend=='sheets' else len([r for r in api.business.requests if r[1].endswith(':commit')])
    assert attempts==1
    api.owner_features={key:False for key in FLAGS}
    result,_=turn(api,'ตรวจผล',response('check_saved_record_change',{}))
    assert result.data['tool_result']['status']=='saved_edit_status',result
    actions=list(api.snapshot()['actions'].values())
    assert any(a['status']==('completed' if committed else 'uncertain') for a in actions)
    assert (api.expense_posts if backend=='sheets' else len([r for r in api.business.requests if r[1].endswith(':commit')]))==1


@pytest.mark.parametrize('kind',['undelivered','changed','off','expired'])
def test_removal_requires_delivered_current_review_and_enabled_feature(kind):
    api=OwnerHosted();api.reject_reply=kind=='undelivered'
    turn(api,'ลบ Spotify วันที่ 1 ต.ค.',find_record(),delete_prepare);api.reject_reply=False
    if kind=='changed':api.google.cells[('All Expenses',3,3)]=99
    if kind=='off':api.owner_features[FLAGS[0]]=False
    if kind=='expired':api.now+=timedelta(days=2)
    result,_=turn(api,'ยืนยัน')
    assert result.status!='deleted' and api.expense_writes==0


@pytest.mark.parametrize('backend',['sheets','firestore'])
def test_alias_confirmation_restore_use_and_explicit_forget(backend):
    api=OwnerHosted(backend);review,_=turn(api,'จำว่า เพลง หมายถึง Spotify',alias())
    assert review.status=='preview' and api.expense_writes==0,review
    assert not api.snapshot()['history24']['current']['owner_features_v1']['aliases']
    result,event=turn(api,'ยืนยัน');assert result.status=='recorded',result
    with TestClient(api.app()) as client:assert api.send(client,event).status_code==200
    rows=api.snapshot()['history24']['current']['owner_features_v1']['aliases']
    assert rows['เพลง']['item']=='Spotify' and api.expense_writes==0
    # Literal alias evidence maps to the canonical item without editing source text.
    result,_=turn(api,'เพลง 88 บาท',prepare(amounts={'Spotify':88}))
    assert result.status=='preview',result
    assert 'Spotify' in result.message and api.expense_writes==0
    turn(api,'ยกเลิก')
    review,_=turn(api,'เลิกจำชื่อ เพลง',alias('remove',item=None));assert review.status=='preview'
    result,_=turn(api,'ยืนยัน');assert result.status=='recorded'
    assert not api.snapshot()['history24']['current']['owner_features_v1']['aliases']


@pytest.mark.parametrize('kind',['undelivered','canonical','invented','target_removed','off'])
def test_alias_rejects_unconfirmed_unsupported_or_stale_binding(kind):
    api=OwnerHosted();api.reject_reply=kind=='undelivered'
    label='กาแฟ' if kind=='canonical' else 'เพลง'
    text='จำว่า '+label+' หมายถึง '+('YouTube' if kind=='invented' else 'Spotify')
    review,_=turn(api,text,alias(label=label));api.reject_reply=False
    if kind in {'canonical','invented'}:
        assert review.status!='preview';assert not api.expense_writes;return
    if kind=='target_removed':api.google.cells[('Database',4,1)]='Different'
    if kind=='off':api.owner_features[FLAGS[1]]=False
    result,_=turn(api,'ยืนยัน');assert result.status!='recorded',result
    assert not api.snapshot()['history24']['current']['owner_features_v1']['aliases'] and api.expense_writes==0


def record(api,items=('Spotify',)):
    text=' และ '.join(item+' 88 บาท' for item in items)
    review,_=turn(api,text,prepare(items,amounts={item:88 for item in items}));assert review.status=='preview',review
    result,_=turn(api,review.data['confirm_command']);assert result.status=='recorded',result
    return result


@pytest.mark.parametrize('backend',['sheets','firestore'])
@pytest.mark.parametrize('operation',['amount','date','source','delete'])
def test_saved_buttons_use_stable_record_and_require_separate_write_confirmation(backend,operation):
    api=OwnerHosted(backend);saved=record(api);calls=len(api.model_bodies);writes=api.expense_writes
    result,_=turn(api,button(saved,operation),postback=True)
    assert len(api.model_bodies)==calls and api.expense_writes==writes,result
    if operation=='source':
        assert result.data['read_only'] and 'ต้นทาง' in result.message
    elif operation=='delete':
        assert result.status=='preview',result
        result,_=turn(api,'ยืนยัน');assert result.status=='deleted',result
        assert api.expense_writes==writes+1
    else:
        assert result.data['record_action_focus']==operation,result
        changes={'amount':'99','amount_text':'99'} if operation=='amount' else {'date':'2026-10-19','date_text':'19 ต.ค.'}
        def amend(body):
            assert context(body)['saved_record_edits']['requested_field']==operation
            return edit(**changes)(body)
        review,_=turn(api,'99' if operation=='amount' else '19 ต.ค.',amend)
        assert review.status=='preview',review
        assert api.expense_writes==writes
        result,_=turn(api,'ยืนยัน');assert result.status=='updated',result
        assert api.expense_writes==writes+1 and result.data.get('record_actions')


@pytest.mark.parametrize('kind',['text','forged','undelivered','expired','off'])
def test_invalid_record_button_never_calls_model_or_writes(kind):
    api=OwnerHosted();saved=record(api);command=button(saved,'amount')
    if kind=='forged':command=command[:-1]+('0' if command[-1]!='0' else '1')
    if kind=='undelivered':
        app=api.app()
        with rpc_budget(20),app.state.assistant.state.session():
            state=app.state.assistant.state;state.replies[saved.data['record_actions']['event']].status='failed';state.checkpoint()
        app.state.pilot_http.close()
    if kind=='expired':api.now+=timedelta(days=2)
    if kind=='off':api.owner_features[FLAGS[2]]=False
    calls=len(api.model_bodies);writes=api.expense_writes
    result,_=turn(api,command,postback=kind!='text')
    assert result.status=='stale' and len(api.model_bodies)==calls and api.expense_writes==writes,result


def test_multi_record_buttons_show_delivered_selection_before_field_focus():
    api=OwnerHosted();saved=record(api,('Spotify','YouTube'));command=button(saved,'amount')
    guessed=command.replace('|*|','|1|');calls=len(api.model_bodies);writes=api.expense_writes
    result,_=turn(api,guessed,postback=True);assert result.status=='stale'
    selection,_=turn(api,command,postback=True);assert selection.data.get('record_action_selection'),selection
    chosen,_=turn(api,guessed,postback=True)
    assert chosen.data['record_action_focus']=='amount' and 'YouTube' in chosen.message
    assert len(api.model_bodies)==calls and api.expense_writes==writes


def test_alias_survives_history_expiry_and_disabled_feature_then_reactivates():
    api=OwnerHosted();turn(api,'จำว่า เพลง หมายถึง Spotify',alias());turn(api,'ยืนยัน')
    before=deepcopy(api.snapshot()['history24']['current']['owner_features_v1'])
    api.now+=timedelta(days=2);api.owner_features[FLAGS[1]]=False
    result,_=turn(api,'ทักทาย',response(text='สวัสดีครับ'))
    assert api.snapshot()['history24']['current']['owner_features_v1']==before
    api.owner_features[FLAGS[1]]=True
    result,_=turn(api,'เพลง 88 บาท',prepare(amounts={'Spotify':88}))
    assert result.status=='preview',result
    assert api.expense_writes==0


def test_alias_interruption_requires_redisplay_and_old_button_stays_invalid():
    api=OwnerHosted();first,_=turn(api,'จำว่า เพลง หมายถึง Spotify',alias())
    old=first.data['line_message']['contents']['footer']['contents'][0]['action']['data']
    turn(api,'มีชื่อเรียกอะไรบ้าง',response('list_catalog_aliases',{}))
    stale,_=turn(api,'ยืนยัน');assert stale.status=='stale'
    fresh,_=turn(api,'ตรวจชื่อเรียกใหม่',response('review_catalog_alias',{}));assert fresh.status=='preview',fresh
    stale,_=turn(api,old,postback=True);assert stale.status=='stale'
    # The stale reply itself interrupts confirmation; redisplay before approving.
    turn(api,'ตรวจชื่อเรียกใหม่',response('review_catalog_alias',{}))
    result,_=turn(api,'ยืนยัน');assert result.status=='recorded',result
    assert api.expense_writes==0


def test_recent_multi_record_undo_requires_choosing_one_record():
    api=OwnerHosted();record(api,('Spotify','YouTube'));writes=api.expense_writes
    selection,_=turn(api,'ลบรายการที่เพิ่งบันทึก',find_record(scope='last',name=None))
    assert selection.status=='clarification' and api.expense_writes==writes,selection
    review,_=turn(api,'เอารายการแรกออก',delete_prepare)
    assert review.status=='preview',review
    result,_=turn(api,'ยืนยัน');assert result.status=='deleted'
    assert api.expense_writes==writes+1
    assert api.snapshot()['last']['kind']=='saved_expense' and len(api.snapshot()['last']['refs'])==1


def test_removal_review_can_be_replaced_with_edit_then_only_updates():
    api=OwnerHosted();turn(api,'ลบ Spotify วันที่ 1 ต.ค.',find_record(),delete_prepare)
    review,_=turn(api,'ไม่ลบแล้ว แก้ยอดเป็น 99 บาท',edit(amount='99',amount_text='99'))
    assert review.status=='preview' and 'หลัง:' in review.message,review
    saved,_=turn(api,'ยืนยัน');assert saved.status=='updated',saved
    assert api.google.cells[('All Expenses',3,3)]==99 and api.expense_writes==1


def test_source_button_resolves_row_again_after_external_reorder():
    api=OwnerHosted();saved=record(api);identifier=saved.data['record_actions']['records'][0]['id']
    row=next(key[1] for key,value in api.google.cells.items() if key[0]=='All Expenses' and key[2]==5 and value==identifier)
    values=[api.google.cells.get(('All Expenses',row,col),'') for col in range(1,7)]
    for col,value in enumerate(values,1):
        api.google.cells[('All Expenses',row,col)]=api.google.cells.get(('All Expenses',2,col),'')
        api.google.cells[('All Expenses',2,col)]=value
    result,_=turn(api,button(saved,'source'),postback=True)
    assert result.data['read_only'] and 'แถวที่ 2' in result.message
    uri=result.data['line_message']['contents']['footer']['contents'][0]['action']['uri']
    assert uri.endswith('range=A2:D2')


def test_new_feature_flags_default_off_keep_old_snapshot_format_and_tools():
    api=OwnerHosted();api.owner_features={key:False for key in FLAGS}
    result=record(api)
    assert 'record_actions' not in result.data
    assert api.snapshot()['history24']['current']['format']=='local-history-state-v1'
    app=api.app();assistant=app.state.assistant
    assert not assistant.catalog_aliases.definitions()
    assert not any(t['name']=='prepare_saved_expense_delete' for t in assistant.saved_edits.definitions())
    app.state.pilot_http.close()


@pytest.mark.parametrize('field',['owner','scope','seal'])
def test_foreign_or_modified_record_buttons_are_rejected_before_record_read(field):
    from line_assistant.history_context import digest
    api=OwnerHosted();saved=record(api);app=api.app();calls=len(api.model_bodies);writes=api.expense_writes
    with rpc_budget(20),app.state.assistant.state.session():
        state=app.state.assistant.state;value=state.receipts[saved.data['record_actions']['event']].result.data['record_actions']
        value[field]='foreign'
        if field!='seal':value['seal']=digest({k:v for k,v in value.items() if k!='seal'})
        state.checkpoint()
    app.state.pilot_http.close()
    result,_=turn(api,button(saved,'delete'),postback=True)
    assert result.status=='stale' and len(api.model_bodies)==calls and api.expense_writes==writes


def test_wrong_line_owner_cannot_use_saved_record_button():
    api=OwnerHosted();saved=record(api);writes=api.expense_writes;calls=len(api.model_bodies)
    event=api.event(button(saved,'delete'),postback=True);event['source']['userId']='U'+'b'*32
    with TestClient(api.app()) as client:assert api.send(client,event).status_code==200
    assert api.expense_writes==writes and len(api.model_bodies)==calls


def test_replacing_owner_alias_requires_new_review_and_preserves_recent_expense():
    api=OwnerHosted();saved=record(api);last=deepcopy(api.snapshot()['last'])
    turn(api,'จำว่า เพลง หมายถึง Spotify',alias());turn(api,'ยืนยัน')
    review,_=turn(api,'เปลี่ยนชื่อเรียก เพลง ให้หมายถึง YouTube',alias(item='YouTube'))
    assert review.status=='preview' and 'เดิมหมายถึง Spotify' in review.message
    assert api.snapshot()['history24']['current']['owner_features_v1']['aliases']['เพลง']['item']=='Spotify'
    turn(api,'ยืนยัน')
    assert api.snapshot()['history24']['current']['owner_features_v1']['aliases']['เพลง']['item']=='YouTube'
    assert api.snapshot()['last']==last and api.expense_writes==1


def test_saved_reply_buttons_preserve_certified_recent_amount_date_and_identity():
    from line_assistant.semantic_tools import recorded_result_matches
    from line_assistant.persistence import result_from
    api=OwnerHosted();saved=record(api)
    action=next(a for a in api.snapshot()['actions'].values() if a['kind']=='add_transactions')
    core=result_from(action['result']);assert recorded_result_matches(core,saved)
    corrupt=deepcopy(saved);corrupt.data['refs']=['different-record']
    assert not recorded_result_matches(core,corrupt)


def test_removed_tombstone_is_excluded_from_summary_and_inventory():
    from datetime import date
    api=OwnerHosted();turn(api,'ลบ Spotify วันที่ 1 ต.ค.',find_record(),delete_prepare);turn(api,'ยืนยัน')
    app=api.app()
    with rpc_budget(30),app.state.assistant.state.session():
        rows=app.state.assistant.sheets.classification_entries()
        assert all(r['transaction_id']!='synthetic_price_3' for r in rows)
        assert app.state.assistant.sheets.summary(date(2026,10,1),date(2026,10,1))['expense']=='0.00'
    app.state.pilot_http.close()


def test_one_time_catalog_choice_does_not_create_durable_alias():
    api=OwnerHosted()
    result,_=turn(api,'Spootify 89 บาท',prepare(amounts={'Spotify':89}))
    assert result.status=='clarification',result
    def choose(body):
        d=context(body)['available_drafts'][0]
        return response('prepare_item_enrollment',{'draft_ref':d['draft_ref'],'draft_version':d['version'],
            'entry_ref':d['entries'][0]['entry_ref'],'category':None,'subcategory':None,
            'existing_item':'Spotify','resolution_step':'use_existing_item'})
    result,_=turn(api,'หมายถึง Spotify สำหรับรายการนี้',choose)
    assert result.status=='preview',result
    assert not api.snapshot()['history24']['current'].get('owner_features_v1',{}).get('aliases')
    assert not api.snapshot()['pending'] and api.expense_writes==0


def test_repeated_alias_redisplay_retains_only_relevant_owner_evidence():
    api=OwnerHosted();turn(api,'จำว่า เพลง หมายถึง Spotify',alias())
    for _ in range(18):
        result,_=turn(api,'ตรวจชื่อเรียกใหม่',response('review_catalog_alias',{}))
        assert result.status=='preview',result
        assert len(api.snapshot()['pending']['payload']['source_refs'])<=3
    saved,_=turn(api,'ยืนยัน');assert saved.status=='recorded'


def test_storage_scope_change_preserves_aliases_in_old_workspace_only():
    api=OwnerHosted('sheets');turn(api,'จำว่า เพลง หมายถึง Spotify',alias());turn(api,'ยืนยัน')
    before=deepcopy(api.snapshot()['history24']['current']['owner_features_v1'])
    api.backend='firestore';api.seed_business_fixture()
    for _ in range(2):
        result,_=turn(api,'จำชื่อเรียกอะไรไว้บ้าง',response('list_catalog_aliases',{'page':None}))
        assert 'ยังไม่มีชื่อเรียก' in result.message,result
        snapshot=api.snapshot()['history24'];old=api.configuration()['PILOT_LEGACY_SPREADSHEET_ID']
        assert snapshot['workspaces'][old]['owner_features_v1']==before
        assert not snapshot['current'].get('owner_features_v1',{}).get('aliases')
    assert api.expense_writes==0
