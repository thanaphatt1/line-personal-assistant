from copy import deepcopy
from datetime import timedelta
import base64
import json
import lzma
import random
import string
import zlib

import pytest
from line_assistant.adapters import SheetLayout
from line_assistant.conversation_mocks import ConversationSheets
from line_assistant.conversation_ui import message, version
from line_assistant.firestore_state import FirestoreState
from line_assistant.line_api import message_hash, rendered_message
from line_assistant.mock_api import MockApiServices
from line_assistant.persistence import state_snapshot, restore_state, SQLiteState
from line_assistant.service import ProviderFailure
from line_assistant.snapshot_codec import encode, decode, MAX_EXPANDED_BYTES
from line_assistant.state import MemoryState, Reply, Pending
from line_assistant.transport import OutcomeUnknown, RestClient
from test_conversation import Harness, NOW, decision, response, deny_network

def test_full_multientry_and_emoji_review_never_hides_fields():
    p=Pending('conversation','12345678','origin',1,NOW+timedelta(minutes=30),{'entries':[]})
    text=('😀'*150+' item / note '+('x'*300)+'\n')*5
    result=message(text,p,[('😀'*20,'confirm','')])
    body=result['contents']['body']['contents']
    assert ''.join(c['text'] for c in body)==text
    assert all(len(c['text'].encode('utf-16-le'))//2<=2000 for c in body)
    assert len(result['altText'].encode('utf-16-le'))//2<=400
    assert len(result['contents']['footer']['contents'][0]['action']['label'].encode('utf-16-le'))//2<=20
    with pytest.raises(ValueError):message('ก'*11000,p,[('บันทึก','confirm','')])

def test_compact_flex_roundtrip_preserves_buttons_hash_and_same_replay_result():
    h=Harness(); result=h.expense(); snapshot=state_snapshot(h.state,'mock-owner')
    saved=snapshot['receipts']['conv-1']['result']['data']
    assert 'line_message' not in saved and 'line_actions' in saved
    assert snapshot['model_runs']['conv-1']['decision'] is None
    restored=MemoryState(); restore_state(restored,snapshot,'mock-owner')
    again=restored.receipts['conv-1'].result
    assert again==result and message_hash(rendered_message(again))==message_hash(rendered_message(result))

@pytest.mark.parametrize('large_batch',[False,True])
def test_monthly_cap_300_high_entropy_calls_and_buttons_fit_and_restore_without_lost_proofs(large_batch):
    rng=random.Random(92)
    names=[''.join(rng.choices(string.ascii_letters,k=150)) for _ in range(5 if large_batch else 1)]
    sheets=ConversationSheets(SheetLayout({'date':'A','item':'B','amount':'C','note':'D'},()),[(name,'Synthetic','') for name in names])
    h=Harness(sheets=sheets); last=None
    for i in range(300):
        if i and i%100==0:h.now+=timedelta(days=1)
        notes=[''.join(rng.choices(string.ascii_letters,k=200 if large_batch else 300)) for _ in names]
        text='\n'.join(f'{name} 20 บาท หมายเหตุ {note}' for name,note in zip(names,notes))
        result=h.send(text,'expense',entries=[{'item':name,'amount':'20','note':note} for name,note in zip(names,notes)])
        assert result.status=='preview'
        # Include actual persisted reply markers, not just application receipts.
        ev=h.state.receipts[f'conv-{h.counter}']; h.state.replies[f'conv-{h.counter}']=Reply(message_hash(rendered_message(result)),ev.expires_at,status='sent')
        assert h.click(result,'บันทึก').status=='recorded'
        reply_receipt=h.state.receipts[f'conv-{h.counter}']; h.state.replies[f'conv-{h.counter}']=Reply(message_hash(rendered_message(reply_receipt.result)),reply_receipt.expires_at,status='sent')
        last=h.counter
    snapshot=state_snapshot(h.state,'mock-owner'); wire=encode(snapshot,800000,compact=True)
    assert len(wire.encode())<=800000
    assert len(snapshot['receipts'])==len(snapshot['claims'])==600 and len(snapshot['actions'])==300
    assert h.port.calls==300 and len(h.sheets.entries)==300*len(names)
    restored=MemoryState(); restore_state(restored,decode(wire,800000),'mock-owner')
    assert restored.receipts[f'conv-{last}'].result.status=='recorded'
    assert len(restored.actions)==300 and len(restored.replies)==600
    assert h.send('สรุปวันนี้','summary').status=='model_budget_exhausted' and h.port.calls==300
    # The exact configured Firestore writer can persist/reopen this wire image.
    provider=MockApiServices(); state=FirestoreState(RestClient('firestore',provider.transport()),'mock-owner',project_id='synthetic',
        clock=lambda:h.now,max_snapshot_bytes=800000,compact_snapshots=True)
    with state.session():restore_state(state,snapshot,'mock-owner');state.checkpoint()
    with state.session():assert len(state.actions)==300 and len(state.receipts)==600

@pytest.mark.parametrize('change',['base64','truncated','trailing','declared_size','bomb','encoding','extras'])
@pytest.mark.parametrize('encoding',['zlib-base64-v1','lzma-base64-v1'])
def test_snapshot_decoder_rejects_corruption_and_expansion_without_losing_state(change,encoding):
    raw=json.dumps({'safe':'x'*10000}).encode()
    packed=zlib.compress(raw) if encoding=='zlib-base64-v1' else lzma.compress(raw,preset=3)
    frame={'encoding':encoding,'bytes':len(raw),'data':base64.b64encode(packed).decode()}
    if change=='base64':frame['data']='!bad!'
    elif change=='truncated':frame['data']=base64.b64encode(packed[:-2]).decode()
    elif change=='trailing':frame['data']=base64.b64encode(packed+b'trailing').decode()
    elif change=='declared_size':frame['bytes']=10
    elif change=='bomb':frame['bytes']=MAX_EXPANDED_BYTES+1
    elif change=='encoding':frame['encoding']='unknown'
    else:frame['unreviewed']='bad'
    with pytest.raises(OutcomeUnknown):decode(json.dumps(frame),800000)

def test_snapshot_capacity_is_finite_and_never_prunes_or_erases_proof_to_fit():
    snapshot={'payload':'x'*(MAX_EXPANDED_BYTES+1)}; before=deepcopy(snapshot)
    with pytest.raises(OutcomeUnknown):encode(snapshot,800000,compact=True)
    assert snapshot==before
    assert decode(encode({'version':1},800000),800000)=={'version':1}

def test_decoder_rejects_oversized_dictionary_and_concatenated_xz_streams():
    raw=b'{"safe":true}'
    packed=lzma.compress(raw,filters=[{'id':lzma.FILTER_LZMA2,'dict_size':128*1024*1024}])
    for value in [packed,lzma.compress(raw)+lzma.compress(raw)]:
        frame={'encoding':'lzma-base64-v1','bytes':len(raw),'data':base64.b64encode(value).decode()}
        with pytest.raises(OutcomeUnknown):decode(json.dumps(frame),800000)

@pytest.mark.parametrize('phase',['prepared','executing','effect_returned','action_completed','receipt_completed'])
def test_durable_crash_cold_start_recovers_atomic_pair_once(tmp_path,phase):
    state=SQLiteState(tmp_path/'conversation.sqlite','mock-owner');h=Harness(state=state)
    r=h.expense('ขนม','50');r=h.click(r,'เพิ่มรายการใหม่');r=h.click(r,'Food')
    data=r.data['line_message']['contents']['footer']['contents'][0]['action']['data'];ev=h.event(postback=data)
    class Crash(BaseException):pass
    def hook(current,key):
        if current==phase:raise Crash()
    h.service.phase_hook=hook
    with pytest.raises(Crash):h.service.handle(ev)
    h2=Harness(state=SQLiteState(tmp_path/'conversation.sqlite','mock-owner'),sheets=h.sheets);h2.now=h.now
    assert h2.service.handle(ev).status=='recorded'
    assert h.sheets.atomic_batches==1 and h.sheets.rows.count(('ขนม','Food',''))==1

def test_same_valid_amount_after_edit_reopens_preview_and_old_button_stays_stale():
    h=Harness(); original=h.expense(); h.click(original,'แก้ไข')
    result=h.send('แก้ยอดเป็น 20 บาท','edit_draft',entries=[{'amount':'20'}])
    assert result.status=='preview' and h.click(original,'บันทึก').status=='stale'
    assert h.click(result,'บันทึก').status=='recorded'

def test_numeric_note_needs_owner_delimiter_to_exclude_competing_amount():
    h=Harness()
    assert h.send('รถเมล์ 20 หรือ 330 บาท','expense',entries=[{'item':'รถเมล์','amount':'330','note':'20'}]).status=='invalid'
    result=h.send('รถเมล์ 20 บาท หมายเหตุ invoice 42','expense',entries=[{'item':'รถเมล์','amount':'20','note':'invoice 42'}])
    assert result.status=='preview' and 'invoice 42' in result.message

def test_explicit_note_date_is_not_the_expense_date():
    h=Harness();result=h.send('รถเมล์ 20 บาท หมายเหตุ invoice 2025-01-01','expense',entries=[{'item':'รถเมล์','amount':'20','note':'invoice 2025-01-01'}])
    assert result.status=='preview' and 'วันที่ 2026-10-12' in result.message
