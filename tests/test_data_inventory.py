"""Synthetic, owner-bound metadata/capability observations, without network."""
from copy import deepcopy
import json,socket
from urllib.parse import unquote
import httpx,pytest
from fastapi.testclient import TestClient
from test_expense_query_operations import seed,expense
from test_calendar_semantic_queries import SemanticHosted
from test_query_analysis_loop import read
from test_calendar_text_confirmation_v13 import turn
from test_calendar_mvp import response
from line_assistant.data_inventory import NAME

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Only synthetic metadata')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def inspect(source='all',continue_analysis=False,**extra):
    def choose(body):
        p=json.loads(body['input'][0]['content'])
        return response(NAME,{'source':source,'continue_analysis':continue_analysis,'source_refs':[p['current_source_ref']],**extra})
    return choose

class InventoryHosted(SemanticHosted):
    def __init__(self):super().__init__();self.input_reads=0;self.deny_input=False
    def handle(self,request):
        path=unquote(request.url.path)
        if request.url.host=='sheets.googleapis.com' and "'All Expenses'!A1:E" in path:
            self.input_reads+=1
            if self.deny_input:return httpx.Response(403,json={})
        return super().handle(request)

def test_model_sees_actual_tool_names_scope_and_unknown_extent_before_any_metadata_read():
    api=InventoryHosted()
    def choose(body):
        p=json.loads(body['input'][0]['content'])
        assert p['available_tools']==[t['name'] for t in body['tools']]
        assert NAME in p['available_tools'] and 'execute_sql' not in p['available_tools']
        assert p['data_sources']['expense']['record_date_extent']=='unknown_until_inspected'
        assert p['data_sources']['calendar']['global_date_extent']=='unknown'
        from line_assistant.read_queries import MAX_QUERY_DAYS
        assert p['data_sources']['calendar']['query_window_days']==MAX_QUERY_DAYS
        assert not p['data_source_observations']
        return inspect('capabilities')(body)
    result,_=turn(api,'ตอนนี้ทำอะไรได้ เข้าถึงข้อมูลอะไรบ้าง',choose)
    assert not api.input_reads and not api.extra_requests and not api.list_requests
    assert 'ยังไม่ใช่ทุกปฏิทินในอีเมล' in result.message

def test_expense_inventory_reports_only_verified_counts_dates_and_observed_years():
    api=InventoryHosted();seed(api,[('2024-01-02','private-synthetic-name',100),('2026-09-01','B',200),('2026-09-03','C',300)])
    result,original=turn(api,'มีรายจ่ายของปีไหนบ้าง',inspect('expense'))
    value=result.data['tool_result']['expense']
    assert value['complete_input_read'] and value['record_count']==3
    assert value['years']==[{'year':2024,'record_count':1},{'year':2026,'record_count':2}]
    assert value['first_record_date']=='2024-01-02' and value['last_record_date']=='2026-09-03'
    encoded=json.dumps(result.data,ensure_ascii=False)
    assert 'private-synthetic-name' not in encoded and 'amount' not in encoded and 'transaction_id' not in encoded
    assert 'ยังไม่ยืนยันว่าบันทึกรายจ่ายจริงครบทุกวัน' in result.message
    before=(len(api.model_bodies),api.input_reads)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(original)).status_code==200
    assert before==(len(api.model_bodies),api.input_reads)
    assert not api.google.sheet_writes and not api.calendar.posts and not api.snapshot()['actions']

def test_verified_empty_dataset_is_distinct_from_unknown():
    api=InventoryHosted();result,_=turn(api,'มีข้อมูลรายจ่ายปีไหน',inspect('expense'))
    value=result.data['tool_result']['expense']
    assert value['status']=='verified' and value['record_count']==0 and value['years']==[]
    assert value['first_record_date'] is value['last_record_date'] is None
    assert 'ไม่พบรายการในฐานข้อมูลที่เชื่อมต่อ' in result.message

@pytest.mark.parametrize('fault',['access','schema','capacity'])
def test_unavailable_or_unverified_inventory_never_claims_zero_or_empty_years(fault):
    api=InventoryHosted()
    if fault=='access':api.deny_input=True
    elif fault=='schema':api.google.cells[('All Expenses',1,1)]='wrong-header'
    else:api.google.row_count=1002
    result,_=turn(api,'มีข้อมูลรายจ่ายปีไหน',inspect('expense'))
    value=result.data['tool_result']['expense']
    assert value['status']=='unavailable' and value['coverage_kind']=='unknown'
    assert 'years' not in value and 'record_count' not in value
    assert 'ยังตรวจช่วงข้อมูลและปีที่มีรายการไม่ได้' in result.message

def test_metadata_can_plan_an_expense_read_and_removes_all_write_tools():
    api=InventoryHosted();seed(api,[('2024-01-02','A',100),('2026-09-01','B',200)])
    def choose(body):
        p=json.loads(body['input'][0]['content']);value=p['data_source_observations']['expense']
        assert value['years']==[{'year':2024,'record_count':1},{'year':2026,'record_count':1}]
        assert p['data_sources']['expense']['record_date_extent']=='current_request_observation'
        assert not any(n.startswith('prepare_') for n in p['available_tools'])
        return expense(view='total')(body)
    result,_=turn(api,'ดูว่ามีข้อมูลปีไหน แล้วสรุปรายจ่ายเดือน ก.ย. 2026',inspect('expense',True),choose)
    assert result.data['tool_result']['expense']=='200.00'

def test_metadata_cache_is_request_local_and_rechecked_on_new_owner_turn():
    api=InventoryHosted();seed(api,[('2024-01-02','A',100)])
    result,_=turn(api,'มีข้อมูลปีไหน',inspect('expense',True),inspect('expense'))
    assert result.data['tool_result']['expense']['record_count']==1 and api.input_reads==1
    seed(api,[('2024-01-02','A',100),('2026-09-01','B',200)])
    result,_=turn(api,'ตรวจข้อมูลปีที่มีใหม่',inspect('expense'))
    assert result.data['tool_result']['expense']['record_count']==2 and api.input_reads==2

def test_free_model_prose_after_inventory_cannot_invent_years_or_source_permissions():
    api=InventoryHosted();seed(api,[('2026-09-01','A',100)])
    result,_=turn(api,'รายจ่ายมีปีไหน',inspect('expense',True),response(text='มีทุกข้อมูลทุกปฏิทินตั้งแต่ปี 1980'))
    assert '1980' not in result.message and 'ทุกปฏิทิน' not in result.message
    assert result.data['tool_result']['expense']['years']==[{'year':2026,'record_count':1}]

def test_calendar_discovery_does_not_claim_global_date_extent_or_event_permission():
    api=InventoryHosted();result,_=turn(api,'ปฏิทินมีข้อมูลตั้งแต่วันไหนถึงวันไหน',inspect('calendar'))
    value=result.data['tool_result']['calendar']
    assert value['selected_calendar_count']==3 and value['global_date_extent']=='unknown'
    assert value['per_calendar_event_access']=='not_verified_by_discovery'
    assert len(api.list_requests)==1 and not api.extra_requests and not api.calendar_requests
    assert 'นัดที่เกิดซ้ำอาจไม่มีวันสิ้นสุด' in result.message
    assert 'synthetic-owner@example.invalid' not in json.dumps(result.data)

def test_calendar_inventory_discloses_only_windows_actually_queried_with_their_criteria():
    api=InventoryHosted()
    result,_=turn(api,'เดือน ก.ย. มีคุมสอบอะไรบ้าง แล้วตรวจข้อมูลปฏิทินถึงไหน',
        read('calendar','2026-09-01',date_mode='calendar_month',filter_text='คุมสอบ'),inspect('calendar'))
    value=result.data['tool_result']['calendar']
    assert value['global_date_extent']=='unknown'
    assert value['queried_windows']==[{'start':'2026-09-01','end':'2026-09-30','criteria':'คุมสอบ','complete':True}]

def test_calendar_discovery_failure_remains_unknown_not_zero_sources():
    api=InventoryHosted();api.list_pages['']=403
    result,_=turn(api,'ตรวจขอบเขตปฏิทิน',inspect('calendar'))
    value=result.data['tool_result']['calendar']
    assert value['status']=='unavailable' and 'selected_calendar_count' not in value

@pytest.mark.parametrize('extra',[{'source_refs':['msg_forged']},{'spreadsheet_id':'foreign'},{'sql':'SELECT *'}])
def test_inventory_cannot_change_owner_or_provider_targets(extra):
    api=InventoryHosted();result,_=turn(api,'ตรวจข้อมูล',inspect(**extra))
    assert not result.data['tool_result']['ok'] and not api.input_reads and not api.list_requests

@pytest.mark.parametrize('backend',['sheets','firestore'])
def test_same_expense_metadata_contract_for_both_configured_backends(backend):
    from expense_storage_hosted import StorageHosted
    api=StorageHosted(backend)
    result,_=turn(api,'มีข้อมูลรายจ่ายปีไหน',inspect('expense'))
    value=result.data['tool_result']['expense']
    assert value['status']=='verified' and value['complete_input_read'] and value['record_count']>0
    assert value['years'] and not api.expense_writes
