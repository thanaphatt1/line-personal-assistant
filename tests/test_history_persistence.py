"""Actual local SQLite/process tests; atomic fake provider is not real Sheets."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json,os,socket,subprocess,sys,tempfile,threading
import pytest
from line_assistant.history_persistence import local_history_assistant,LocalHistoryState,history_snapshot,restore_history
from line_assistant.history_context import HistoryState
from history_persistence_worker import ScriptPort

NOW=datetime(2026,10,13,12,tzinfo=timezone.utc);ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def offline(monkeypatch):
 def deny(*a,**k):raise AssertionError('No network in history persistence tests')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def event(text,key,n=1,at=NOW,owner='mock-owner'):
 return {'webhookEventId':key,'timestamp':int(at.timestamp()*1000)+n,'source':{'type':'user','userId':owner},'type':'message','message':{'id':key,'type':'text','text':text}}
def runtime(path,now=NOW,**kw):return local_history_assistant(path,port=ScriptPort(),clock=lambda:now,enabled=True,**kw)
def prepare(path):
 r=runtime(path).handle(event('ค่ากระดาษ 228 บาท','create',1));assert r.status=='preview';return r
def worker(path,ev,phase='none',now=NOW,available=True):
 with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,suffix='.json',delete=False) as f:
  json.dump({'event':ev,'now':now.isoformat(),'available':available},f);name=f.name
 try:return subprocess.run([sys.executable,str(ROOT/'tests/history_persistence_worker.py'),str(path),name,phase],
              env={**os.environ,'PYTHONPATH':str(ROOT/'src')},text=True,capture_output=True,timeout=30)
 finally:Path(name).unlink()
def result(p,status):
 assert p.returncode==0,p.stderr
 assert json.loads(p.stdout)['status']==status,p.stdout

def test_default_off_and_local_snapshot_restart_preserves_all_history_not_six_turns(tmp_path):
 with pytest.raises(ValueError,match='defaults off'):local_history_assistant(tmp_path,port=ScriptPort())
 r=prepare(tmp_path);a=runtime(tmp_path)
 for i in range(12):a.handle(event('เรื่องอื่น '+str(i),'chat'+str(i),i+2))
 before=history_snapshot(a.state);b=runtime(tmp_path)
 assert history_snapshot(b.state)==before and len(b.state.history)>20
 assert b.handle(event(r.data['confirm_command'],'confirm',30)).status=='recorded'
 c=runtime(tmp_path);assert len(c.sheets.entries)==1 and next(iter(c.state.drafts.values()))['status']=='recorded'

def test_bangkok_midnight_and_exact_rolling_window_survive_restart(tmp_path):
 at=datetime(2026,10,13,16,59,tzinfo=timezone.utc)
 r=runtime(tmp_path,at).handle(event('ค่ากระดาษ 228 บาท','create',1,at));b=runtime(tmp_path,at+timedelta(minutes=2))
 b.handle(event('เรื่องอื่นข้ามเที่ยงคืน','night',1,at+timedelta(minutes=2)))
 assert next(iter(b.state.drafts.values()))['entries'][0]['date']=='2026-10-13' and any(x['event']=='create' for x in b.state.history)
 later=at+timedelta(hours=24,seconds=1);c=runtime(tmp_path,later)
 assert c.handle(event(r.data['confirm_command'],'expired',1,later)).status=='stale'
 assert not c.state.drafts and not c.state.draft_proofs and not any(x['event']=='create' for x in c.state.history)

def test_no_cross_owner_restore_or_provider_access(tmp_path):
 prepare(tmp_path)
 with pytest.raises(ValueError,match='owner'):runtime(tmp_path,owner_id='other-owner')
 a=runtime(tmp_path);assert a.handle(event('ค่ากระดาษ 228 บาท','foreign',owner='other-owner')).status=='denied'
 assert not a.sheets.entries

@pytest.mark.parametrize('part',['owner','format','core_owner','draft_version','provenance','review_hash'])
def test_corrupt_snapshot_is_rejected_without_partially_replacing_state(tmp_path,part):
 prepare(tmp_path);a=runtime(tmp_path);v=history_snapshot(a.state);old=deepcopy(v);d=next(iter(v['drafts'].values()))
 if part=='owner':v['owner_id']='foreign'
 elif part=='format':v['format']='future-unverified'
 elif part=='core_owner':v['core']['owner_id']='foreign'
 elif part=='draft_version':d['version']+=1
 elif part=='provenance':d['entries'][0]['source_refs'].append('fake')
 else:d['review']['payload_hash']='0'*64
 with pytest.raises(ValueError):restore_history(a.state,v,'mock-owner')
 assert history_snapshot(a.state)==old

def test_older_correction_cannot_replace_a_newer_validated_version_after_restart(tmp_path):
 prepare(tmp_path);a=runtime(tmp_path);r=a.handle(event('แก้ยอดค่ากระดาษเป็น 250 บาท','edit',20));assert r.status=='preview'
 newest_command=r.data['confirm_command']
 b=runtime(tmp_path);r=b.handle(event('แก้ยอดค่ากระดาษเป็น 260 บาท','older',10))
 assert r.status=='clarification' and r.data['tool_result']['error']=='older_draft_update'
 assert next(iter(runtime(tmp_path).state.drafts.values()))['entries'][0]['amount']=='250'
 assert runtime(tmp_path).handle(event(newest_command,'latest-confirm',30)).status=='recorded'
 assert runtime(tmp_path).sheets.entries[0].amount=='250'

def test_snapshot_compare_and_swap_rejects_stale_writer_and_out_of_session_checkpoint(tmp_path):
 state=LocalHistoryState(tmp_path/'state.sqlite','mock-owner')
 with pytest.raises(RuntimeError,match='active'):state.checkpoint()
 with pytest.raises(RuntimeError,match='Stale'):
  with state.session():
   with state.store.transaction() as c:
    record=state.store.read(c,'history_state');record['revision']+=1;state.store.write(c,'history_state',record)
   state.history_removed=999;state.checkpoint()
 fresh=LocalHistoryState(tmp_path/'state.sqlite','mock-owner');assert fresh.history_removed==0

@pytest.mark.parametrize('phase',['state_staged','state_committed','history_review_pre_receipt','receipt_completed','history_turn_completed'])
def test_actual_exit_during_review_checkpoint_recovers_same_ingress_without_losing_state(tmp_path,phase):
 path=tmp_path/'runtime';runtime(path);ev=event('ค่ากระดาษ 228 บาท','create')
 stopped=worker(path,ev,phase);assert stopped.returncode==73,stopped.stderr
 resumed=worker(path,ev);result(resumed,'preview')
 a=runtime(path);assert len(a.state.drafts)==1 and not a.sheets.entries
 b=a.handle(ev);assert b.status=='preview' and b.data==json.loads(resumed.stdout)['data']

def test_unknown_model_attempt_survives_restart_and_is_not_retried_or_refunded(tmp_path):
 path=tmp_path/'runtime';runtime(path);ev=event('ค่ากระดาษ 228 บาท','create')
 assert worker(path,ev,'model_started').returncode==73
 before=runtime(path);budget=deepcopy(before.state.model_budgets)
 restarted=worker(path,ev);result(restarted,'model_budget_exhausted')
 assert json.loads(restarted.stdout)['model_requests']==0 and runtime(path).state.model_budgets==budget
 assert not runtime(path).sheets.entries

def test_private_local_files_and_logical_checkpoint_rollback_are_not_cloud_guarantees(tmp_path):
 path=tmp_path/'runtime';prepare(path)
 assert path.stat().st_mode&0o777==0o700
 for p in path.glob('*.sqlite'):assert p.stat().st_mode&0o777==0o600
 state=runtime(path).state
 with pytest.raises(RuntimeError,match='staged fault'):
  with state.session():
   previous=state.revision
   def fail(phase,revision):
    if phase=='state_staged':raise RuntimeError('staged fault')
   state.phase_hook=fail;state.history_removed=999;state.checkpoint()
 with state.store.transaction() as c:
  record=state.store.read(c,'history_state')
 assert record['revision']==previous and record['snapshot']['history_removed']!=999
 # Opening another owner session itself checkpoints an unchanged snapshot.
 assert runtime(path).state.history_removed!=999

@pytest.mark.parametrize('phase',['prepared','executing','provider_staged','provider_committed','effect_returned','action_completed','receipt_completed','history_turn_completed'])
def test_actual_exit_around_confirmed_write_reconciles_one_atomic_fake_effect(tmp_path,phase):
 path=tmp_path/'runtime';r=prepare(path);ev=event(r.data['confirm_command'],'confirm',2)
 stopped=worker(path,ev,phase);assert stopped.returncode==73,stopped.stderr
 result(worker(path,ev),'recorded');a=runtime(path);assert len(a.sheets.entries)==len(a.sheets.write_log)==1
 assert next(iter(a.state.drafts.values()))['status']=='recorded' and next(iter(a.state.drafts.values()))['review'] is None
 assert any(x['event']=='confirm' and x['role']=='assistant' and 'บันทึกรายจ่าย' in x['text'] for x in a.state.history)
 assert a.handle(ev).status=='recorded' and len(runtime(path).sheets.entries)==1

@pytest.mark.parametrize('phase',['executing','provider_committed'])
def test_missing_external_receipt_proof_stays_uncertain_across_restarts_and_expiry(tmp_path,phase):
 path=tmp_path/'runtime';r=prepare(path);ev=event(r.data['confirm_command'],'confirm',2)
 assert worker(path,ev,phase,available=False).returncode==73
 expected=0 if phase=='executing' else 1
 for now in [NOW,NOW+timedelta(days=2)]:
  a=runtime(path,now,reconciliation_available=False)
  assert a.handle(ev).status=='uncertain' and len(runtime(path,now).sheets.entries)==expected
 # Atomic fake receipts can resolve the ambiguity; absence in real Sheets cannot.
 assert runtime(path,NOW+timedelta(days=2)).handle(ev).status==('expired' if expected==0 else 'recorded')
 assert len(runtime(path).sheets.entries)==expected

def test_concurrent_actual_processes_duplicate_create_and_confirm_once(tmp_path):
 path=tmp_path/'runtime';runtime(path);ev=event('ค่ากระดาษ 228 บาท','create')
 with ThreadPoolExecutor(max_workers=4) as pool:processes=list(pool.map(lambda _:worker(path,ev),range(6)))
 for p in processes:result(p,'preview')
 commands={json.loads(p.stdout)['data']['confirm_command'] for p in processes};assert len(commands)==1
 confirm=event(commands.pop(),'confirm',2)
 with ThreadPoolExecutor(max_workers=4) as pool:processes=list(pool.map(lambda _:worker(path,confirm),range(6)))
 for p in processes:result(p,'recorded')
 assert len(runtime(path).sheets.entries)==1 and len(runtime(path).state.actions)==1

def test_independent_workers_load_latest_state_for_overlapping_corrections_and_old_confirm(tmp_path):
 path=tmp_path/'runtime';first=prepare(path);a,b,c=[runtime(path) for _ in range(3)]
 entered=threading.Event();release=threading.Event()
 def pause(phase,key):
  if phase=='history_review_pre_receipt':entered.set();assert release.wait(5)
 a.phase_hook=pause
 with ThreadPoolExecutor(max_workers=3) as pool:
  edit=pool.submit(a.handle,event('แก้ยอดค่ากระดาษเป็น 250 บาท','edit',20));assert entered.wait(5)
  confirm=pool.submit(b.handle,event(first.data['confirm_command'],'old-confirm',30))
  older=pool.submit(c.handle,event('แก้ยอดค่ากระดาษเป็น 260 บาท','older-edit',10))
  release.set();new=edit.result(timeout=5);assert new.status=='preview'
  assert confirm.result(timeout=5).status=='stale' and older.result(timeout=5).status=='clarification'
 final=runtime(path);assert next(iter(final.state.drafts.values()))['entries'][0]['amount']=='250' and not final.sheets.entries

def test_confirmation_first_is_terminal_and_correction_cannot_reopen_the_recorded_draft(tmp_path):
 path=tmp_path/'runtime';r=prepare(path);assert runtime(path).handle(event(r.data['confirm_command'],'confirm',3)).status=='recorded'
 a=runtime(path)
 # Explicit pointer to the already recorded object, not a new user expense.
 ref=next(iter(a.state.drafts));e=next(iter(a.state.drafts.values()))['entries'][0]
 from test_history_first import response,data,Port
 p=Port();a.port=p;p.values=[response(name='prepare_expenses',args={'draft_ref':ref,'draft_version':1,'entries':[data(entry_ref=e['entry_ref'],amount='250')]}),response(text='ร่างบันทึกแล้ว')]
 r=a.handle(event('แก้ยอดค่ากระดาษเป็น 250 บาท','edit',4))
 assert r.status=='clarification' and len(runtime(path).sheets.entries)==1

def test_negative_control_omitting_history_and_drafts_from_checkpoint_is_detected(tmp_path,monkeypatch):
 import line_assistant.history_persistence as storage
 original=storage.history_snapshot
 def check(path):
  prepare(path);assert runtime(path).state.history and runtime(path).state.drafts
 check(tmp_path/'good')
 def unsafe(state):
  value=original(state);value.update(history=[],drafts={},draft_proofs={});return value
 with monkeypatch.context() as m:
  m.setattr(storage,'history_snapshot',unsafe)
  with pytest.raises(AssertionError):check(tmp_path/'bad')
