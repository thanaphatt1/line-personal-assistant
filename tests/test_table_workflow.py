"""LINE routing with real workflow execution and synthetic model decisions."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest

from test_calendar_mvp import Harness, response
from test_tables import registry, seed
from line_assistant.table_workflow import TableWorkflow
from line_assistant.history_persistence import history_snapshot, restore_history
from line_assistant.history_context import HistoryState
from line_assistant.transport import OutcomeUnknown


def harness(reg=None, state=None):
    h = Harness(state=state)
    h.app.table_flow = TableWorkflow(h.app, reg or registry())
    return h


def call(name, **values):
    def decide(body):
        context = json.loads(body['input'][0]['content'])
        args = {'source_refs': [context['current_source_ref']], **deepcopy(values)}
        result=response(name, args)
        result['output'][0]['call_id']=name+'_'+str(len(body['input']))
        return result
    return decide


def prepare(table='people', operation='insert', record_id=None, draft_ref=None, **values):
    return call('prepare_table_change', table_id=table, operation=operation, record_id=record_id, draft_ref=draft_ref,
                values=[{'field': k, 'value': v[0], 'source_text': v[1]} for k, v in values.items()])


def test_catalog_then_schema_then_add_person_and_replay_once():
    h = harness()
    r = h.send('เพิ่มคุณจำลอง เบอร์ 0000123', call('describe_table', table_id='people'), prepare(name=('คุณจำลอง', 'คุณจำลอง'), phone=('0000123', '0000123')))
    assert r.status == 'preview' and '0000123' in r.message
    assert h.app.table_flow.registry.stores['people'].writes == 0
    first = json.loads(h.model.requests[0]['input'][0]['content'])
    assert {r['id'] for r in first['table_catalog']} == {'people', 'family_income', 'shopping','expenses'}
    assert 'spreadsheet_id' not in json.dumps(first, ensure_ascii=False)
    event = h.event('ยืนยัน')
    saved = h.app.handle(event)
    assert saved.status == 'recorded' and h.app.handle(deepcopy(event)) == saved
    store = h.app.table_flow.registry.stores['people']
    assert store.writes == 1
    assert next(iter(store.records.values()))['values'] == {'name': 'คุณจำลอง', 'phone': '0000123', 'status': 'pending'}
    assert not h.sheets.entries and not h.provider.events


def test_income_uses_metadata_date_default_without_expense_catalog_or_name():
    h = harness()
    r = h.send('วันนี้รายได้ 1000', prepare('family_income', amount=('1000', '1000')))
    assert r.status == 'preview' and r.data['table_draft']['values']['date'] == '2026-10-16'
    assert h.send('ยืนยัน').status == 'recorded'
    assert h.app.table_flow.registry.stores['family_income'].writes == 1 and not h.sheets.entries


def test_partial_draft_and_original_default_date_survive_cold_restore():
    h = harness()
    r = h.send('เพิ่มเบอร์ 0000001', prepare(phone=('0000001', '0000001')))
    assert r.status == 'clarification' and 'ชื่อ' in r.message
    h.state.mark_history_delivered(h.app._active_event_id,r,at=h.now)
    value = history_snapshot(h.state)
    state = HistoryState(); state.clock=lambda:h.now; state.raw_journal=h.state.raw_journal; restore_history(state, value, 'mock-owner')
    second = harness(h.app.table_flow.registry, state)
    second.now = h.now + timedelta(hours=1); second.counter = 50
    r = second.send('ชื่อคุณบี', prepare(draft_ref=r.data['table_draft']['ref'], name=('คุณบี', 'คุณบี')))
    assert r.status == 'preview' and r.data['table_draft']['values']['phone'] == '0000001'
    assert second.send('ยืนยัน').status == 'recorded'


def test_people_query_no_date_and_typed_schema_rejects_numeric_name_sum():
    h = harness(); reg = h.app.table_flow.registry
    seed(reg, 'people', [{'name': 'A', 'phone': None, 'status': 'pending'}, {'name': 'B', 'phone': None, 'status': 'called'}])
    result = h.send('มีใครยังไม่ได้โทรกลับ', call('query_table', table_id='people', filters=[{'field': 'status', 'op': 'eq', 'values': ['pending']}]))
    assert result.data['tool_result']['count'] == 1 and 'A' in result.message
    result = h.send('รวมชื่อ', call('query_table', table_id='people', aggregates=[{'op': 'sum', 'field': 'name', 'alias': 'total'}]))
    assert result.status == 'clarification' and result.data['tool_result']['error_code'] == 'numeric_field_required'


def test_query_edit_same_turn_and_server_checks_before_version():
    h = harness(); reg = h.app.table_flow.registry
    seed(reg, 'people', [{'name': 'A', 'phone': '0001', 'status': 'pending'}])
    result = h.send('แก้เบอร์ A เป็น 0002', call('query_table', table_id='people', for_change=True),
                    prepare(operation='update', record_id='row0', phone=('0002', '0002')))
    assert result.status == 'preview' and '0001' in result.message and '0002' in result.message
    reg.stores['people'].records['row0']['version'] = 'changed'
    assert h.send('ยืนยัน').status == 'conflict' and reg.stores['people'].writes == 0


def test_displayed_query_candidates_can_be_changed_next_turn_but_hidden_rows_cannot():
    h = harness(); reg = h.app.table_flow.registry
    seed(reg, 'people', [{'name': 'A', 'phone': None, 'status': 'pending'}, {'name': 'B', 'phone': None, 'status': 'pending'}])
    h.send('ดูคน', call('query_table', table_id='people', limit=1))
    result = h.send('แก้คนที่ไม่ได้แสดง', prepare(operation='update', record_id='row1', phone=('0002', '0002')))
    assert result.data['tool_result']['error_code'] == 'record_not_offered'
    h.send('ดูคน', call('query_table', table_id='people', limit=1))
    result = h.send('แก้เบอร์ A เป็น 0002', prepare(operation='update', record_id='row0', phone=('0002', '0002')))
    assert result.status == 'preview'
    assert h.send('ยืนยัน').status == 'recorded' and reg.stores['people'].records['row0']['values']['phone'] == '0002'


def test_delete_has_before_review_and_no_effect_until_confirmation():
    h = harness(); reg = h.app.table_flow.registry
    seed(reg, 'people', [{'name': 'A', 'phone': None, 'status': 'pending'}])
    result = h.send('ลบ A', call('query_table', table_id='people', for_change=True), prepare(operation='delete', record_id='row0'))
    assert result.status == 'preview' and 'A' in result.message and 'row0' in reg.stores['people'].records
    assert h.send('ยืนยัน').status == 'recorded' and not reg.stores['people'].records


@pytest.mark.parametrize('failure', ['new_reply', 'expired', 'schema_changed'])
def test_consent_cannot_cross_interruption_expiry_or_schema_change(failure):
    h = harness(); reg = h.app.table_flow.registry
    h.send('เพิ่ม A', prepare(name=('A', 'A')))
    if failure == 'new_reply': h.send('คุยเรื่องอื่น', response(text='ครับ'))
    elif failure == 'expired': h.now += timedelta(hours=25)
    else: reg.tables['people'].description = 'new binding'
    result = h.send('ยืนยัน')
    assert result.status in {'clarification', 'stale', 'expired'} and reg.stores['people'].writes == 0


@pytest.mark.parametrize('field,value,evidence', [('name', 'invented', 'A'), ('phone', '0002', '0001')])
def test_unstated_literals_do_not_become_facts(field, value, evidence):
    h = harness()
    result = h.send('เพิ่ม A เบอร์ 0001', prepare(**{field: (value, evidence)}))
    assert result.status == 'clarification' and not h.app.table_flow.registry.stores['people'].records


def test_uncertain_insert_reconciles_without_retry_after_restart():
    h = harness(); reg = h.app.table_flow.registry; store = reg.stores['people']
    original = store.apply
    def lose(table, change, key):
        original(table, change, key)
        raise OutcomeUnknown('lost acknowledgement')
    store.apply = lose
    h.send('เพิ่ม A', prepare(name=('A', 'A')))
    assert h.send('ยืนยัน').status == 'uncertain' and store.writes == 1
    snapshot = history_snapshot(h.state)
    state = HistoryState(); state.clock=lambda:h.now; state.raw_journal=h.state.raw_journal; restore_history(state, snapshot, 'mock-owner')
    second = harness(reg, state); second.now = h.now; second.counter = 50
    result = second.send('ตรวจผลตาราง', call('check_table_change', table_id='people'))
    assert result.status == 'recorded' and store.writes == 1


def test_read_analysis_removes_all_table_write_tools_and_reports_grounded_results():
    h = harness(); reg = h.app.table_flow.registry
    seed(reg, 'people', [{'name': 'A', 'phone': None, 'status': 'pending'}])
    def finish(body):
        assert 'prepare_table_change' not in {t['name'] for t in body['tools']}
        last = json.loads(body['input'][-1]['output'])
        c = json.loads(body['input'][0]['content'])
        return response('finish_query_analysis', {'query_refs': [last['query_ref']], 'operation': 'report', 'source_refs': [c['current_source_ref']]})
    result = h.send('นับคน', call('query_table', table_id='people', continue_analysis=True), finish)
    assert result.status == 'answer' and '1 รายการ' in result.message and not h.state.actions


def test_failed_provider_read_does_not_become_zero(monkeypatch):
    h = harness()
    def fail(table): raise RuntimeError('sensitive provider message')
    monkeypatch.setattr(h.app.table_flow.registry.stores['people'], 'scan', fail)
    result = h.send('มีกี่คน', call('query_table', table_id='people'))
    assert result.status == 'clarification' and '0 รายการ' not in result.message and 'sensitive' not in result.message


def finish_last(body):
    context=json.loads(body['input'][0]['content']);last=json.loads(body['input'][-1]['output'])
    return response('finish_query_analysis',{'query_refs':[last['query_ref']],'operation':'report','source_refs':[context['current_source_ref']]})


def test_table_semijoin_uses_all_matched_people_beyond_display_limit():
    h=harness();reg=h.app.table_flow.registry
    seed(reg,'people',[{'name':name,'phone':None,'status':'pending'} for name in ['A','B']])
    seed(reg,'family_income',[{'date':'2026-10-16','amount':amount,'customer':customer,'note':None} for amount,customer in [('100','row0'),('200','row1'),('999','other')]])
    def joined(body):
        last=json.loads(body['input'][-1]['output'])
        return call('query_table',table_id='family_income',matching_query_ref=last['query_ref'],matching_field='customer',source_field='id',
                    aggregates=[{'op':'sum','field':'amount','alias':'total'}],continue_analysis=True)(body)
    result=h.send('รายรับจากคนที่ยังไม่โทรกลับทั้งหมด',call('query_table',table_id='people',limit=1,continue_analysis=True),joined,finish_last)
    assert result.status=='answer' and '300' in result.message and '999' not in result.message
    assert h.app._query_reads[next(reversed(h.app._query_reads))]['rows']==[{'total':'300'}]


@pytest.mark.parametrize('complete', [True,False])
def test_calendar_dates_join_any_registered_date_table_and_reject_incomplete_basis(complete):
    h=harness();reg=h.app.table_flow.registry
    seed(reg,'family_income',[{'date':day,'amount':amount,'customer':None,'note':None} for day,amount in [('2026-10-16','60'),('2026-10-17','700')]])
    def joined(body):
        from line_assistant.query_analysis import remember
        output={'domain':'calendar','complete':complete,'ok':True,'count':2,'scope':{'domain':'calendar','start':'2026-10-16','end':'2026-10-17'},
                'analysis_records':[{'read_ref':'c1','all_day':True,'start':'2026-10-16','end':'2026-10-17'},
                                    {'read_ref':'c2','all_day':True,'start':'2026-10-16','end':'2026-10-17'}]}
        h.app._query_reads['synthetic_read']=output
        return call('query_table',table_id='family_income',calendar_query_refs=['synthetic_read'],date_field='date',
                    aggregates=[{'op':'sum','field':'amount','alias':'total'}])(body)
    result=h.send('รายรับวันที่มีนัด',joined)
    if complete:assert result.data['tool_result']['rows']==[{'total':'60'}]
    else:assert result.status=='clarification' and 'total' not in result.data['tool_result']


def test_offered_person_can_be_linked_by_name_and_missing_reference_rejected():
    h=harness();reg=h.app.table_flow.registry
    seed(reg,'people',[{'name':'คุณจำลอง','phone':None,'status':'pending'}])
    result=h.send('วันนี้คุณจำลองจ่าย 1000',call('query_table',table_id='people',for_change=True),
                  prepare('family_income',amount=('1000','1000'),customer=('row0','คุณจำลอง')))
    assert result.status=='preview' and result.data['table_draft']['values']['customer']=='row0'
    assert h.send('ยืนยัน').status=='recorded'
    result=h.send('รายได้ 1000 ลูกค้า ghost',prepare('family_income',amount=('1000','1000'),customer=('ghost','ghost')))
    assert result.data['tool_result']['error_code'].startswith('reference_not_found')


def test_cancel_table_draft_and_read_interruption_cannot_confirm_old_review():
    h=harness();store=h.app.table_flow.registry.stores['people']
    h.send('เพิ่ม A',prepare(name=('A','A')))
    assert h.send('ยกเลิก').status=='cancelled'
    assert h.send('ยืนยัน').status!='recorded' and store.writes==0
    h.send('เพิ่ม A',prepare(name=('A','A')))
    h.send('ดูคน',call('query_table',table_id='people'))
    assert h.send('ยืนยัน').status!='recorded' and store.writes==0


def test_delayed_confirmation_before_newer_review_cannot_select_old_review():
    h=harness();store=h.app.table_flow.registry.stores['people']
    h.send('เพิ่ม A',prepare(name=('A','A')))
    delayed=h.event('ยืนยัน')
    h.send('เพิ่ม B',prepare(name=('B','B')))
    assert h.app.handle(delayed).status!='recorded' and store.writes==0


def test_id_membership_can_select_semantic_candidates_then_code_counts():
    h=harness();reg=h.app.table_flow.registry
    seed(reg,'people',[{'name':v,'phone':None,'status':'pending'} for v in ['A','B','C']])
    result=h.send('เฉพาะสองคน',call('query_table',table_id='people',filters=[{'field':'id','op':'in','values':['row0','row2']}],aggregates=[{'op':'count','alias':'people'}]))
    assert result.data['tool_result']['rows']==[{'people':2}]


def test_legacy_expense_view_uses_same_table_engine_without_changing_write_workflow():
    from line_assistant.contracts import Transaction
    h=harness();h.sheets.entries=[Transaction(item='กาแฟ',amount='0.10',kind='expense',date='2026-10-16'),Transaction(item='กาแฟ',amount='0.20',kind='expense',date='2026-10-16')]
    result=h.send('รวมรายจ่าย',call('query_table',table_id='expenses',aggregates=[{'op':'sum','field':'amount','alias':'total'}]))
    assert result.data['tool_result']['rows']==[{'total':'0.30'}]
    result=h.send('เพิ่มผ่าน table',prepare('expenses',amount=('10','10')))
    assert result.data['tool_result']['error_code']=='operation_disabled' and len(h.sheets.entries)==2


def test_duplicate_names_cannot_be_selected_by_elimination_or_display_truncation():
    h=harness();reg=h.app.table_flow.registry
    seed(reg,'people',[{'name':'A','phone':phone,'status':'pending'} for phone in ['0001','0002']])
    result=h.send('แก้เบอร์ A เป็น 0003',call('query_table',table_id='people',for_change=True,limit=1),
                  prepare(operation='update',record_id='row0',phone=('0003','0003')))
    assert result.data['tool_result']['error_code']=='record_selection_required'
    result=h.send('A จ่าย 1000',call('query_table',table_id='people',for_change=True,limit=1),
                  prepare('family_income',amount=('1000','1000'),customer=('row0','A')))
    assert result.data['tool_result']['error_code'].startswith('reference_selection_required')
    assert not h.state.actions


def test_explicit_date_matches_owner_timestamp_and_amount_cannot_be_invented():
    h=harness()
    result=h.send('เมื่อวานรายได้ 1000',prepare('family_income',date=('2026-10-15','เมื่อวาน'),amount=('1000','1000')))
    assert result.status=='preview' and result.data['table_draft']['values']['date']=='2026-10-15'
    result=h.send('เมื่อวานรายได้ 1000',prepare('family_income',date=('2026-10-16','เมื่อวาน'),amount=('1000','1000')))
    assert result.status=='clarification'
    result=h.send('รายได้ 1000',prepare('family_income',amount=('10000','1000')))
    assert result.data['tool_result']['error_code'].startswith('numeric_evidence_required')


def test_partial_updates_do_not_extend_original_expiry():
    h=harness()
    result=h.send('เพิ่มเบอร์ 0001',prepare(phone=('0001','0001')))
    original=result.data['table_draft']
    h.now+=timedelta(hours=23)
    result=h.send('ชื่อ A',prepare(draft_ref=original['ref'],name=('A','A')))
    assert result.status=='preview' and result.data['table_draft']['expires_at']==original['expires_at']


def test_all_new_tools_have_provider_strict_schemas():
    from test_history_tool_schema import strict_objects
    h=harness()
    for tool in h.app.table_flow.definitions():
        assert tool['strict'] is True
        strict_objects(tool['parameters'])


def test_observed_model_using_thai_labels_can_find_and_prepare_delete():
    h=harness();reg=h.app.table_flow.registry
    seed(reg,'people',[{'name':'คุณเอจำลอง','phone':'0001','status':'pending'}])
    result=h.send('ลบผู้ติดต่อคุณเอจำลอง',call('query_table',table_id='people',filters=[{'field':'ชื่อ','op':'eq','values':['คุณเอจำลอง']}],
                  select=['ชื่อ','เบอร์โทร','สถานะ'],for_change=True),prepare(operation='delete',record_id='row0'))
    assert result.status=='preview' and result.data['table_draft']['operation']=='delete' and reg.stores['people'].writes==0


def test_unknown_field_returns_schema_to_model_and_corrected_call_continues():
    h=harness();reg=h.app.table_flow.registry
    seed(reg,'people',[{'name':'A','phone':None,'status':'pending'}])
    result=h.send('ดูคน',call('query_table',table_id='people',select=['invented_name']),call('query_table',table_id='people',select=['name']))
    assert result.status=='answer' and result.data['tool_result']['rows']==[{'id':'row0','name':'A'}]
    repair=json.loads(h.model.requests[-1]['input'][-1]['output'])
    assert repair['schema_repair'] and repair['schema']['columns'][0]['name']=='name'


def test_labels_are_schema_aliases_for_changes_and_do_not_create_new_columns():
    h=harness()
    result=h.send('เพิ่ม A เบอร์ 0001',prepare(**{'ชื่อ':('A','A'),'เบอร์โทร':('0001','0001')}))
    assert result.status=='preview' and result.data['table_draft']['values']=={'name':'A','phone':'0001','status':'pending'}


def test_observed_shortened_evidence_is_repaired_using_actual_owner_words():
    h=harness();reg=h.app.table_flow.registry
    seed(reg,'shopping',[{'item':'กระดาษ','quantity':2,'purchased':False,'week':None}])
    result=h.send('ซื้อกระดาษแล้ว',call('query_table',table_id='shopping',for_change=True),
                  prepare('shopping',operation='update',record_id='row0',purchased=(True,'ซื้อแล้ว')),
                  prepare('shopping',operation='update',record_id='row0',purchased=(True,'ซื้อกระดาษแล้ว')))
    assert result.status=='preview' and result.data['table_draft']['values']['purchased'] is True
    assert reg.stores['shopping'].writes==0
    repair=json.loads(h.model.requests[-1]['input'][-1]['output'])
    assert repair['fact_repair'] and repair['eligible_owner_sources'][0]['text']=='ซื้อกระดาษแล้ว'


def test_unread_empty_relationship_does_not_become_a_zero_total_and_plan_can_recover():
    h=harness();reg=h.app.table_flow.registry
    seed(reg,'people',[{'name':'A','phone':None,'status':'pending'}])
    seed(reg,'family_income',[{'date':'2026-10-16','amount':'300','customer':'row0','note':None}])
    def joined(body):
        last=json.loads(body['input'][-1]['output'])
        return call('query_table',table_id='family_income',matching_query_ref=last['query_ref'],matching_field='customer',source_field='id',aggregates=[{'op':'sum','field':'amount','alias':'total'}])(body)
    result=h.send('รายรับจากลูกค้าที่ยังไม่ได้โทรกลับรวมเท่าไหร่',
        call('query_table',table_id='family_income',select=['amount'],aggregates=[{'op':'sum','field':'amount','alias':'total'}]),
        call('query_table',table_id='family_income',filters=[{'field':'customer','op':'in','values':[]}],aggregates=[{'op':'sum','field':'amount','alias':'total'}]),
        call('query_table',table_id='people',filters=[{'field':'status','op':'eq','values':['pending']}],continue_analysis=True),joined,finish_last)
    assert result.status=='answer' and '300' in result.message
    assert h.app.trace[0]['result']['error_code']=='aggregate_select_conflict'
    assert h.app.trace[1]['result']['error_code']=='empty_membership_requires_source'


def test_metadata_routing_requires_choice_with_competing_destinations_and_no_default():
    from line_assistant.tables import Registry,MemoryTableStore
    reg=registry();first=reg.tables['family_income'];first.aliases=['รายรับที่บ้าน'];first.default_for_group=False
    other=first.model_copy(deep=True);other.id='freelance_income';other.label='รายรับงานอิสระ';other.aliases=['รายรับงานอิสระ']
    reg=Registry([*reg.tables.values(),other],{**reg.stores,other.id:MemoryTableStore('mock-freelance')})
    h=harness(reg)
    result=h.send('วันนี้รายได้ 1000',prepare('family_income',amount=('1000','1000')))
    assert result.status=='clarification' and result.data['tool_result']['error_code']=='routing_selection_required'
    assert 'รายรับงานอิสระ' in result.message and 'table_draft' not in result.data and not h.state.actions
    reg=h.app.table_flow.registry;reg.tables['family_income'].default_for_group=True
    result=h.send('วันนี้รายได้ 1000',prepare('family_income',amount=('1000','1000')))
    assert result.status=='preview'
    reg.tables['family_income'].default_for_group=False
    reg.tables['family_income'].aliases=['รายได้'];reg.tables['freelance_income'].aliases=['รายได้']
    assert h.send('รายได้ 1000',prepare('family_income',amount=('1000','1000'))).status=='clarification'


def test_unique_owner_alias_overrides_default_destination_without_new_domain_function():
    from line_assistant.tables import Registry,MemoryTableStore
    reg=registry();first=reg.tables['family_income'];first.aliases=['รายรับที่บ้าน']
    other=first.model_copy(deep=True);other.id='freelance_income';other.label='รายรับงานอิสระ';other.aliases=['รายรับงานอิสระ'];other.default_for_group=False
    reg=Registry([*reg.tables.values(),other],{**reg.stores,other.id:MemoryTableStore('mock-freelance')})
    h=harness(reg)
    result=h.send('รายรับงานอิสระ 1000',prepare('family_income',amount=('1000','1000')),prepare('freelance_income',amount=('1000','1000')))
    assert result.status=='preview' and result.data['table_draft']['table_id']=='freelance_income'


def test_metadata_question_can_finish_after_schema_without_claiming_no_tables():
    h=harness()
    result=h.send('ตารางผู้ติดต่อมีคอลัมน์อะไร',call('describe_table',table_id='people'),response(text='มีชื่อ เบอร์โทร และสถานะติดตามครับ'))
    assert result.status=='answer' and 'เบอร์โทร' in result.message and 'ยังไม่มี' not in result.message
    assert result.data['tool_result']['schema']['id']=='people' and not h.state.actions
