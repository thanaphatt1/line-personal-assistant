"""Normal-context cancellation and restored replay with synthetic providers."""
from copy import deepcopy
from behavior_support import Harness,Port,cold_replay
from test_expense_interface_v05 import prepare,draft,turn
from test_history_first import response

def test_single_cancel_normal_context_and_restored_duplicate_are_safe():
 h=Harness(Port(),'cancel');prepare(h)
 d=draft(h)
 h.port.values=[response(name='control_expense_draft',args={'operation':'cancel','draft_ref':d['draft_ref'],'draft_version':d['version']})]
 event,result,out=h.run_turn('ยกเลิกร่างกาแฟนี้ครับ',1)
 assert result.status=='cancelled' and out['calls']==1 and not h.state.drafts and not h.sheets.entries and not h.state.actions
 proof=cold_replay(h,event,result)
 assert proof['identical_result'] and proof['raw_originals_retained']
 assert proof['model_calls']==proof['fake_rows_added']==proof['fake_catalog_batches_added']==0

def test_foreign_target_never_cancels_owned_pending_work_or_generates_a_second_call():
 h=Harness(Port(),'foreign-cancel');prepare(h);before=deepcopy(h.state.drafts);d=draft(h)
 result=turn(h,'ยกเลิกร่างกาแฟนี้ครับ',response(name='control_expense_draft',args={'operation':'cancel','draft_ref':'draft_foreign','draft_version':d['version']}))
 assert result.status=='clarification' and h.state.drafts==before and len(h.port.records)==2 and not h.sheets.entries
