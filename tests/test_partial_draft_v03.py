"""Synthetic partial proposals, partial facts, persisted null drafts and cancel."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest
import behavior_support as ev
from line_assistant import semantic_tools as semantic
from line_assistant.history_first import HistoryFirstAssistant
from line_assistant.history_persistence import LocalHistoryState,history_snapshot,restore_history
from line_assistant.config import Settings
from test_history_first import response,data
from test_raw_workflow_v02 import Port,turn

TEXTS={'expense':'ค่าวาฟเฟิลทดสอบ 60 บาทเมื่อวาน','begin':'เอาเป็นชื่อใหม่ครับ','cancel':'ยกเลิกร่างนี้ครับ'}
def decision(h,kind):
 if kind=='unsupported_lookup':return response(name='lookup_catalog',args={'kind':'item','query':'วาฟเฟิลทดสอบ'})
 if kind=='empty_expense':return lambda b:response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[data(item=None,amount=None,source_refs=[json.loads(b['input'][0]['content'])['current_source_ref']])]})
 if kind=='topic_reply':return response(text='คุยต่อได้ครับ')
 if kind=='cancel':return response(name='control_expense_draft',args={'operation':'cancel','draft_ref':draft(h)['draft_ref'],'draft_version':draft(h)['version']})
 raise AssertionError(kind)
def harness(state=None):
    h=ev.Harness(Port(),'spending_followup')
    if state is not None:
        h.state=state;state.clock=lambda:h.now
        h.service=HistoryFirstAssistant(Settings(owner_id='synthetic-workflow-owner'),h.sheets,port=h.port,state=state,clock=lambda:h.now,history_first_enabled=True,item_enrollment_enabled=True,semantic_enabled=True,raw_history_enabled=True)
    return h
def prepare(h,text,entry,ref=None,version=None):
    def value(body):
        c=json.loads(body['input'][0]['content']);e=deepcopy(entry)
        if not e.get('source_refs'):e['source_refs']=[c['current_source_ref']]
        return response(name='prepare_expenses',args={'draft_ref':ref,'draft_version':version,'entries':[e]})
    return turn(h,text,value,response(text='ขอข้อมูลเพิ่มครับ'))
def draft(h):return next(iter(h.state.drafts.values()))

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('No live/model provider access')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def test_initial_double_lookup_is_unadvertised_and_not_converted_to_expense_permission():
    h=harness();result=turn(h,TEXTS['expense'],decision(h,'unsupported_lookup'),decision(h,'unsupported_lookup'))
    assert result.status=='model_unavailable' and not h.state.drafts and not h.sheets.entries
    assert not {'lookup_catalog','read_expense_drafts'}&{t['name'] for t in h.port.records[0]['request']['tools']}

def test_synthetic_all_null_new_snapshot_is_rejected_and_original_refs_remain_available():
    h=harness();turn(h,TEXTS['expense'],decision(h,'unsupported_lookup'))
    result=turn(h,TEXTS['begin'],decision(h,'empty_expense'),response(text='ต้องใช้ข้อมูลเดิม'))
    out=result.data['tool_result'];assert out['error']=='expense_facts_missing'
    assert ev.source_ref('synthetic-'+h.case+'-1') in out['available_source_refs'] and not h.state.drafts and not h.sheets.entries

@pytest.mark.parametrize('entry',[{},data(item=None,amount=None),data(item='',amount=None),data(item=None,amount=None,note='')])
def test_missing_empty_and_null_new_entries_do_not_allocate_drafts(entry):
    h=harness();result=prepare(h,'เอาเป็นชื่อใหม่ครับ',entry)
    assert result.data['tool_result']['error']=='expense_facts_missing' and not h.state.drafts and not h.sheets.entries

def test_prior_expense_source_can_be_explicitly_prepared_after_topic_without_template_or_inferred_defaults():
    h=harness();turn(h,TEXTS['expense'],decision(h,'unsupported_lookup'))
    turn(h,'ดาวฤกษ์คืออะไร',decision(h,'topic_reply'))
    original=ev.source_ref('synthetic-'+h.case+'-1')
    result=prepare(h,'กลับมาเรื่องรายจ่ายเมื่อกี้',data(item='วาฟเฟิลทดสอบ',amount=60,date='2026-10-13',date_text='เมื่อวาน',date_context=None,source_refs=[original]))
    assert result.status=='clarification' and draft(h)['entries'][0]['item']=='วาฟเฟิลทดสอบ' and draft(h)['entries'][0]['amount']=='60' and draft(h)['entries'][0]['date']=='2026-10-13'
    assert not h.sheets.entries

def test_amount_then_name_then_date_partials_retain_facts_and_ask_only_missing_fields():
    h=harness();result=prepare(h,'จ่าย 75 บาท',data(item=None,amount=75,date_context=None))
    assert result.status=='clarification' and draft(h)['entries'][0]['amount']=='75' and draft(h)['entries'][0]['item'] is None
    d=deepcopy(draft(h));result=prepare(h,'เป็นค่ากระดาษครับ',data(entry_ref=d['entries'][0]['entry_ref'],item='ค่ากระดาษ',amount=None,date_context=None),d['draft_ref'],d['version'])
    assert draft(h)['entries'][0]['item']=='ค่ากระดาษ' and draft(h)['entries'][0]['amount']=='75'
    d=deepcopy(draft(h));result=prepare(h,'เมื่อวาน',data(entry_ref=d['entries'][0]['entry_ref'],item=None,amount=None,date='2026-10-13',date_text='เมื่อวาน',date_context=None),d['draft_ref'],d['version'])
    assert result.status=='preview' and draft(h)['entries'][0]['amount']=='75' and draft(h)['entries'][0]['date']=='2026-10-13' and not h.sheets.entries

def install_legacy_null(h):
    if isinstance(h.state,LocalHistoryState):
        temporary=harness();install_legacy_null(temporary)
        with h.state.session():
            for row in temporary.state.raw_journal.rows.values():h.state.raw_journal.put(deepcopy(row))
            restore_history(h.state,history_snapshot(temporary.state),'synthetic-workflow-owner')
        h.now=temporary.now;h.counter=temporary.counter
        return
    # Create a valid current partial, then model a supported nullable old snapshot.
    prepare(h,'จ่าย 75 บาท',data(item=None,amount=75,date_context=None))
    d=draft(h);d['entries'][0]['amount']=None
    d['missing_fields']=['entries[0].item','entries[0].amount'];d['review']=None
    h.service._seal(d)
    h.port.records.clear();h.port.requests.clear()
    h.counter=2;h.now=ev.ANCHOR+timedelta(seconds=2)
    for n,text in [(2,TEXTS['begin'])]:
        h.state.append('user','synthetic-'+h.case+'-'+str(n),ev.ANCHOR+timedelta(seconds=n),text=text)


def test_synthetic_cancel_of_synthetic_legacy_null_entry_succeeds_and_replays_once():
    h=harness();install_legacy_null(h);original=deepcopy(draft(h))
    event=h.event(TEXTS['cancel']);h.port.case=h.case;h.port.turn=TEXTS['cancel'];h.port.limit=1;h.port.turn_calls=0;h.port.state=h.state;h.port.now=h.now;h.port.values=[decision(h,'cancel')]
    result=h.service.handle(event);assert result.status=='cancelled' and 'ยังไม่มีชื่อ' in result.message
    assert result.data['tool_result']['cancelled_draft']==original and not h.state.drafts and not h.sheets.entries
    count=len(h.port.records);assert h.service.handle(deepcopy(event))==result and len(h.port.records)==count

def test_read_and_choose_adjacent_renderers_handle_missing_names_without_issuing_consent():
    h=harness();install_legacy_null(h);d=draft(h)
    assert 'ยังไม่มีชื่อ' in h.service._result({'ok':True,'drafts':[d]}).message
    assert 'ยังไม่มีชื่อ' in h.service._result({'ok':True,'status':'draft_read','entries':d['entries']}).message
    assert 'ยังไม่มีชื่อ' in semantic.result(h.service,{'status':'choose_draft','targets':[{'draft_ref':d['draft_ref'],'draft_version':1,'token':'f'*16,'items':[None]}]}).message
    assert not h.sheets.entries

@pytest.mark.parametrize('repeat',[False,True])
def test_cancel_null_draft_across_sqlite_restore_preserves_raw_and_financial_ledgers(tmp_path,repeat):
    state=LocalHistoryState(tmp_path/'partial.sqlite','synthetic-workflow-owner',clock=lambda:ev.ANCHOR)
    h=harness(state)
    install_legacy_null(h)
    reopened=LocalHistoryState(tmp_path/'partial.sqlite','synthetic-workflow-owner',clock=lambda:h.now)
    h2=harness(reopened);h2.now=h.now;h2.counter=2
    result=turn(h2,TEXTS['cancel'],decision(h2,'cancel'));assert result.status=='cancelled' and not h2.state.drafts and not h2.sheets.entries
    final=LocalHistoryState(tmp_path/'partial.sqlite','synthetic-workflow-owner',clock=lambda:h2.now)
    assert not final.drafts and any(r['text']=='จ่าย 75 บาท' for r in final.history)
    assert not final.actions and not h2.sheets.atomic_batches
    if repeat:
        h3=harness(final);h3.now=h2.now-timedelta(seconds=1);h3.counter=2
        assert h3.run_turn(TEXTS['cancel'],0)[1]==result and not h3.port.records

def test_partial_null_name_can_gain_an_amount_before_a_name_without_typeerror_or_write():
    h=harness();install_legacy_null(h);d=deepcopy(draft(h))
    result=prepare(h,'75 บาท',data(entry_ref=d['entries'][0]['entry_ref'],item=None,amount=75,date_context=None),d['draft_ref'],d['version'])
    assert result.status=='clarification' and draft(h)['entries'][0]['amount']=='75' and draft(h)['entries'][0]['item'] is None and not h.sheets.entries

@pytest.mark.parametrize('change',[{'draft_version':999},{'draft_ref':'draft_foreign'},{'entries':[data(entry_ref='entry_foreign',item=None,amount=75)]}])
def test_partial_update_foreign_or_stale_references_do_not_change_state(change):
    h=harness();install_legacy_null(h);d=deepcopy(draft(h));before=deepcopy(h.state.drafts)
    value={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':[data(entry_ref=d['entries'][0]['entry_ref'],item=None,amount=75,date_context=None)]};value.update(change)
    turn(h,'75 บาท',response(name='prepare_expenses',args=value),response(text='ตรวจข้อมูลก่อน'))
    assert h.state.drafts==before and not h.sheets.entries

@pytest.mark.parametrize('field',['item','amount','date','date_basis','source_refs'])
def test_missing_persisted_entry_fields_are_rejected_off_to_the_side(field):
    from line_assistant.history_context import draft_fingerprint
    h=harness();install_legacy_null(h);snapshot=history_snapshot(h.state)
    ref=next(iter(snapshot['drafts']));snapshot['drafts'][ref]['entries'][0].pop(field)
    snapshot['draft_proofs'][ref]['fingerprint']=draft_fingerprint(snapshot['drafts'][ref])
    before=deepcopy(h.state.drafts)
    with pytest.raises(ValueError,match='Missing persisted'):restore_history(h.state,snapshot,'synthetic-workflow-owner')
    assert h.state.drafts==before and not h.sheets.entries

@pytest.mark.parametrize('value',[42,False,[],{}])
def test_nonstring_persisted_item_cannot_reach_rendering_or_cancel(value):
    from line_assistant.history_context import draft_fingerprint
    h=harness();install_legacy_null(h);snapshot=history_snapshot(h.state);ref=next(iter(snapshot['drafts']))
    snapshot['drafts'][ref]['entries'][0]['item']=value;snapshot['draft_proofs'][ref]['fingerprint']=draft_fingerprint(snapshot['drafts'][ref])
    before=deepcopy(h.state.drafts)
    with pytest.raises(ValueError,match='Invalid persisted partial'):restore_history(h.state,snapshot,'synthetic-workflow-owner')
    assert h.state.drafts==before
