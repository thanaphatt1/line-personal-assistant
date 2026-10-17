"""Trusted snapshot identity versus fresh evidence; synthetic provider responses are offline."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json, socket, sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from line_assistant.config import Settings
from line_assistant.history_first import HistoryFirstAssistant,tools
from test_history_first import Harness,data,response,Port

@pytest.fixture(autouse=True)
def offline(monkeypatch):
 def deny(*a,**k):raise AssertionError('Reference fix tests cannot use network')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)

def read_update(h,text,*,refs=None,changes=None,version=None):
 d=h.draft();ref=d['draft_ref'];before=deepcopy(d)
 def prepare(body):
  c=json.loads(body['input'][0]['content']);read=json.loads(body['input'][2]['output'])['drafts'][0]
  e=read['entries'][0]
  args={'draft_ref':ref,'draft_version':read['draft_version'] if version is None else version,
        'entries':[data(entry_ref=e['entry_ref'],item=e['item'],amount=e['amount'],date=e['date'],note=e['note'],
                        source_refs=(e['source_refs'] if refs is None else refs)+[c['current_source_ref']])]}
  args['entries'][0].update(changes or {})
  return response(name='prepare_expenses',args=args)
 result=h.send(text,response(name='read_expense_drafts',args={'draft_ref':ref}),prepare)
 return result,before

def test_read_refs_remain_usable_after_storage_and_actual_request_truncation():
 h=Harness();h.prepare('วันที่ 10 มีค่ากระดาษ 228');oldref=h.draft()['entries'][0]['source_refs'][0]
 for i in range(6):h.send('อีกเรื่อง '+('ก'*900),response(text='ตอบ'))
 assert oldref not in {r.get('source_ref') for r in json.loads(h.port.requests[-1]['input'][0]['content'])['history']}
 r,old=read_update(h,'แก้ยอดค่ากระดาษเป็น 250 บาท',changes={'amount':'250'})
 assert r.status=='preview' and h.draft()['version']==old['version']+1 and h.draft()['entries'][0]['date']=='2026-10-10'
 assert h.draft()['entries'][0]['amount']=='250' and oldref in h.draft()['entries'][0]['source_refs'] and not h.sheets.entries
 assert all(s['utf8_bytes']<=6144 for s in h.service.context_stats)

def test_lost_raw_history_keeps_certified_values_but_true_draft_expiry_cannot_resurrect_them():
 h=Harness();first=h.prepare('ค่ากระดาษ 228 บาท');ref=h.draft()['draft_ref'];h.state.history=[]
 r,_=read_update(h,'อ่านร่างค่ากระดาษ')
 assert r.status=='preview' and h.draft()['entries'][0]['amount']=='228'
 h.now+=timedelta(hours=24,seconds=1)
 r=h.send('อ่านร่างค่ากระดาษ',response(name='read_expense_drafts',args={'draft_ref':ref}),response(text='ร่างหมดอายุ'))
 assert r.status=='clarification' and not h.state.drafts and not h.state.draft_proofs
 assert h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries

@pytest.mark.parametrize('change',[{'amount':'999'},{'item':'กาแฟ'},{'date':'2026-03-10'},{'note':'ข้อความที่เจ้าของไม่เคยให้'}])
def test_certified_old_ref_is_not_evidence_for_an_altered_value(change):
 h=Harness();h.prepare('วันที่ 10 มีค่ากระดาษ 228');r,old=read_update(h,'อ่านร่างค่ากระดาษ',changes=change)
 assert r.status=='clarification' and h.draft()['entries']==old['entries'] and not h.sheets.entries

def test_fabricated_source_ref_is_rejected_even_with_a_valid_draft_and_read():
 h=Harness();h.prepare('ค่ากระดาษ 228');r,old=read_update(h,'แก้ยอดค่ากระดาษเป็น 250 บาท',refs=['msg_fabricated'],changes={'amount':'250'})
 assert r.status=='clarification' and r.data['tool_result']['error']=='source_not_bound_to_entry' and h.draft()['entries']==old['entries']

def test_other_entry_ref_or_provenance_cannot_be_substituted():
 h=Harness();h.prepare('ค่ากระดาษ 228');h.prepare('กาแฟ 50',item='กาแฟ',amount=50)
 other=list(h.state.drafts.values())[1]['entries'][0]
 r,old=read_update(h,'แก้ค่ากระดาษเป็น 250 บาท',refs=other['source_refs'],changes={'amount':'250'})
 assert r.status=='clarification' and r.data['tool_result']['error']=='source_not_bound_to_entry'
 r,_=read_update(h,'แก้ค่ากระดาษเป็น 250 บาท',changes={'entry_ref':other['entry_ref'],'amount':'250'})
 assert r.status=='clarification' and r.data['tool_result']['error']=='entry_reference_missing' and h.draft()['entries']==old['entries']

def test_current_evidence_for_another_batch_entry_does_not_authorize_an_altered_value():
 h=Harness()
 def create(body):
  c=json.loads(body['input'][0]['content'])
  return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[
    data(item='ค่ากระดาษ',amount=228,source_refs=[c['current_source_ref']]),data(item='กาแฟ',amount=50,source_refs=[c['current_source_ref']])]})
 r=h.send('ค่ากระดาษ 228 บาท\nกาแฟ 50 บาท',create);assert r.status=='preview'
 before=deepcopy(h.draft()['entries'])
 def swapped(body):
  c=json.loads(body['input'][0]['content']);d=h.draft()
  entries=[data(entry_ref=e['entry_ref'],item=e['item'],amount='250' if i==0 else e['amount'],date=e['date'],note=e['note'],source_refs=e['source_refs']+[c['current_source_ref']]) for i,e in enumerate(d['entries'])]
  return response(name='prepare_expenses',args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':entries})
 r=h.send('แก้ยอดกาแฟเป็น 250 บาท',swapped,response(text='ขอระบุรายการ'))
 assert r.status=='clarification' and h.draft()['entries']==before and not h.sheets.entries

def test_new_entry_cannot_claim_a_fabricated_existing_entry_id():
 h=Harness();r=h.prepare('ค่ากระดาษ 228',entry_ref='entry_fabricated')
 assert r.status=='clarification' and r.data['tool_result']['error']=='entry_reference_without_draft' and not h.state.drafts

def test_old_version_is_rejected_after_a_valid_update():
 h=Harness();h.prepare('ค่ากระดาษ 228');v=h.draft()['version'];h.update('แก้ยอดค่ากระดาษเป็น 250 บาท',amount=250)
 r,old=read_update(h,'แก้ยอดค่ากระดาษเป็น 260 บาท',version=v,changes={'amount':'260'})
 assert r.status=='clarification' and r.data['tool_result']['error']=='stale_draft_version' and h.draft()['entries']==old['entries']

def test_read_view_is_stale_if_a_snapshot_changes_between_read_and_prepare():
 h=Harness();h.prepare('ค่ากระดาษ 228');ref=h.draft()['draft_ref']
 def changed(body):
  c=json.loads(body['input'][0]['content']);read=json.loads(body['input'][2]['output'])['drafts'][0]
  d=h.draft();d['version']+=1;h.service._seal(d)  # Simulate a trusted concurrent save.
  return response(name='prepare_expenses',args={'draft_ref':ref,'draft_version':read['draft_version'],'entries':[data(entry_ref=d['entries'][0]['entry_ref'],source_refs=[c['current_source_ref']])]})
 r=h.send('อ่านร่างค่ากระดาษ',response(name='read_expense_drafts',args={'draft_ref':ref}),changed)
 assert r.status=='clarification' and r.data['tool_result']['error']=='stale_draft_version'

@pytest.mark.parametrize('tamper',['value','provenance','owner','version'])
def test_persisted_snapshot_tampering_is_not_trusted_by_read_or_confirm(tamper):
 h=Harness();first=h.prepare('ค่ากระดาษ 228');d=h.draft()
 if tamper=='value':d['entries'][0]['amount']='999'
 elif tamper=='provenance':d['entries'][0]['source_refs'].append('msg_fake')
 elif tamper=='owner':d['owner_id']='other-owner'
 else:d['version']+=1
 r=h.send('อ่านร่างค่ากระดาษ',response(name='read_expense_drafts',args={'draft_ref':d['draft_ref']}),response(text='ร่างไม่ผ่านการตรวจสอบ'))
 assert r.status=='clarification' and h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries

def test_state_cannot_be_reused_by_another_owner():
 h=Harness();h.prepare('ค่ากระดาษ 228')
 with pytest.raises(ValueError,match='another owner'):HistoryFirstAssistant(Settings(owner_id='other-owner'),h.sheets,port=Port(),state=h.state,history_first_enabled=True)

def test_category_provenance_does_not_justify_changed_category_without_current_evidence():
 h=Harness();h.prepare('กล่องคราม 20 บาท',item='กล่องคราม',amount=20)
 r=h.update('หมวด Office',category='Office',subcategory='');assert r.status=='preview'
 r,old=read_update(h,'อ่านร่างกล่องคราม',changes={'category':'Food','subcategory':''})
 assert r.status=='clarification' and h.draft()['entries']==old['entries'] and not h.sheets.entries

def test_read_marks_known_items_so_category_lookup_is_not_required():
 h=Harness();h.prepare('ค่ากระดาษ 228')
 r=h.send('อ่านร่างค่ากระดาษ',response(name='read_expense_drafts',args={'draft_ref':h.draft()['draft_ref']}),response(text='มีข้อมูลครบ'))
 d=r.data['tool_result']['drafts'][0]
 assert d['category_lookup_required'] is False and d['draft_version']==h.draft()['version']
 assert 'owner_id' not in d and 'fingerprint' not in d and 'Known item: null' in tools()[0]['parameters']['properties']['entries']['items']['properties']['category']['description']


def test_old_confirmation_is_stale_after_a_read_based_update_and_only_new_review_can_commit_mock():
 h=Harness();first=h.prepare('ค่ากระดาษ 228');r,_=read_update(h,'แก้ยอดค่ากระดาษเป็น 250 บาท',changes={'amount':'250'})
 assert r.status=='preview' and h.send(first.data['confirm_command']).status=='stale' and not h.sheets.entries
 assert h.send(r.data['confirm_command']).status=='recorded' and len(h.sheets.entries)==1

def test_negative_control_trusting_cached_value_changes_is_detected(monkeypatch):
 original=HistoryFirstAssistant._entry
 def check():
  h=Harness();h.prepare('ค่ากระดาษ 228');r,_=read_update(h,'อ่านร่างค่ากระดาษ',changes={'amount':'999'})
  assert r.status=='clarification' and h.draft()['entries'][0]['amount']=='228' and not h.sheets.entries
 def unsafe(self,value,old,index):
  row,missing,invalid,sources=original(self,value,old,index)
  return row,missing,[v for v in invalid if v!='amount_source'],sources
 check()
 with monkeypatch.context() as m:
  m.setattr(HistoryFirstAssistant,'_entry',unsafe)
  with pytest.raises(AssertionError):check()

def test_negative_control_accepting_fabricated_cached_refs_is_detected(monkeypatch):
 def check():
  h=Harness();h.prepare('ค่ากระดาษ 228');r,_=read_update(h,'แก้ยอดค่ากระดาษเป็น 250 บาท',refs=['msg_fabricated'],changes={'amount':'250'})
  assert r.status=='clarification' and h.draft()['entries'][0]['amount']=='228'
 check()
 with monkeypatch.context() as m:
  m.setattr(HistoryFirstAssistant,'_entry_sources',lambda self,value,old:self._owner_sources([self._current['source_ref']]))
  with pytest.raises(AssertionError):check()


@pytest.mark.parametrize('text,changes,field,expected', [
 ('แก้ยอดค่ากระดาษเป็น 250 บาท',{'amount':'250'},'amount','250'),
 ('แก้วันที่ค่ากระดาษเป็น 20 กันยายน 2026',{'date':'2026-09-20','date_text':'20 กันยายน 2026'},'date','2026-09-20'),
])
def test_truncated_source_read_preserves_identity_for_amount_and_date_changes(text,changes,field,expected):
 h=Harness();h.prepare('วันที่ 10 มีค่ากระดาษ 228');original=h.draft()['entries'][0]['source_refs'][0]
 for i in range(6):h.send('ทดสอบหัวข้ออื่น '+('ก'*900),response(text='รับทราบ'))
 assert original not in {r.get('source_ref') for r in json.loads(h.port.requests[-1]['input'][0]['content'])['history']}
 result,before=read_update(h,text,changes=changes)
 assert result.status=='preview' and h.draft()['version']==before['version']+1
 assert h.draft()['entries'][0][field]==expected and original in h.draft()['entries'][0]['source_refs']
 assert not h.sheets.entries
