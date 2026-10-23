"""Additional payments reuse owner facts, never prior consent or pending targets."""
from copy import deepcopy
from datetime import timedelta
import json
import httpx
import pytest

from line_assistant.transport import ApiError,OutcomeUnknown
from test_raw_history import Harness,context
from test_history_first import response
from test_catalog_names_v09 import prepare,entry
from test_pending_records import patch


def setup_saved():
    h=Harness();h.sheets.rows.append(('Laptop Orion','Gadget',''))
    prepare(h,'อุปกรณ์ทดสอบค้าง 19 บาท',[entry(item='อุปกรณ์ทดสอบค้าง',amount=19)])
    background=deepcopy(h.draft())
    review=prepare(h,'Laptop Orion 1370',[entry(item='Laptop Orion',amount=1370)])
    source=h.service._current['source_ref']
    assert h.send(review.data['confirm_command']).status=='recorded'
    return h,source,background


def additional(source,*,amount='1370',current_source=True,field_sources=True):
    def decide(body):
        c=context(body);now=c['current_source_ref']
        value=entry(item='Laptop Orion',amount=amount,date_text='27 sep',
            source_refs=[source,*([now] if current_source else [])],
            field_sources={'item':[source],'amount':[source],'date':[now],'note':[]} if field_sources else None)
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'entries':[value]})
    return decide


@pytest.mark.parametrize('text',['วันที่ 27 sep ด้วย ยอดนี้','เพิ่ม laptop นี้ในวันที่ 27 sep ด้วย'])
def test_additional_payment_reuses_verified_history_and_keeps_background_unchanged(text):
    h,source,background=setup_saved();saved=deepcopy(h.sheets.entries)
    r=h.send(text,additional(source))
    assert r.status=='preview',r
    assert h.state.drafts[background['draft_ref']]==background
    assert h.sheets.entries==saved
    e=r.data['tool_result']['entries'][0]
    assert (e['item'],e['amount'],e['date'])==('Laptop Orion','1370','2026-09-27')
    assert e['fact_sources']['amount']==[source]
    event=h.event(r.data['confirm_command']);assert h.service.handle(event).status=='recorded'
    assert len(h.sheets.entries)==2
    assert h.service.handle(deepcopy(event)).status=='recorded' and len(h.sheets.entries)==2


@pytest.mark.parametrize('damage',['no_current','no_field_sources','invented_amount','expired'])
def test_reuse_does_not_accept_unbound_or_invented_history(damage):
    h,source,_=setup_saved();saved=deepcopy(h.sheets.entries)
    if damage=='expired':h.now+=timedelta(hours=25)
    r=h.send('วันที่ 27 sep ด้วย ยอดนี้',additional(source,amount='999' if damage=='invented_amount' else '1370',
        current_source=damage!='no_current',field_sources=damage!='no_field_sources'),response(text='ขอตรวจข้อมูลก่อน'))
    assert r.status!='preview' and h.sheets.entries==saved


def test_current_explicit_payment_works_with_lowercase_and_date_typo_despite_unrelated_pending():
    h,_,background=setup_saved()
    r=prepare(h,'ค่า laptop orion 1370 วันทีา 27 sep',[entry(item='Laptop Orion',amount=1370,date_text='27 sep')])
    assert r.status=='preview',r
    assert r.data['tool_result']['entries'][0]['date']=='2026-09-27'
    assert h.state.drafts[background['draft_ref']]==background and len(h.sheets.entries)==1


def test_catalog_case_variant_is_valid_literal_patch_evidence():
    h=Harness();h.sheets.rows.append(('Laptop Orion','Gadget',''))
    prepare(h,'ค่ากระดาษ 51',[entry(item='ค่ากระดาษ',amount=51)])
    r=h.send('เปลี่ยนชื่อเป็น laptop orion',patch({0:[('item','Laptop Orion','laptop orion')]}))
    assert r.status=='preview',r
    assert h.draft()['entries'][0]['item']=='Laptop Orion' and not h.sheets.entries


def test_new_payment_repair_cannot_switch_to_an_unrelated_pending_patch():
    h,_,background=setup_saved()
    def wrong(body):
        c=context(body)
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[
            entry(item=None,amount=1370,date_text='27 sep',source_refs=[c['current_source_ref']])]})
    def corrected(body):
        offered={t['name'] for t in body['tools']}
        assert 'prepare_expenses' in offered and 'patch_pending_records' not in offered
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[
            entry(item='Laptop Orion',amount=1370,date_text='27 sep',source_refs=[context(body)['current_source_ref']])]})
    r=h.send('ค่า laptop orion 1370 วันทีา 27 sep',wrong,corrected)
    assert r.status=='preview' and h.state.drafts[background['draft_ref']]==background
    assert len(h.sheets.entries)==1


def test_explicit_current_amount_cannot_be_overridden_by_reused_history():
    h,source,_=setup_saved()
    r=h.send('เพิ่ม Laptop Orion 1480 วันที่ 27 sep',additional(source),response(text='ต้องตรวจยอดใหม่'))
    assert r.status!='preview' and len(h.sheets.entries)==1


def test_same_explicit_current_amount_uses_current_proof_even_when_model_cites_history():
    h,source,background=setup_saved()
    r=h.send('ค่า laptop orion 1370 วันทีา 27 sep',additional(source))
    assert r.status=='preview',r
    e=r.data['tool_result']['entries'][0]
    assert e['fact_sources']['amount']==[h.service._current['source_ref']]
    assert h.state.drafts[background['draft_ref']]==background and len(h.sheets.entries)==1


def test_null_historical_values_get_actionable_creation_repair():
    h,source,background=setup_saved()
    def incomplete(body):
        c=context(body);now=c['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[
            entry(item=None,amount=None,date_text='วันที่ 27 sep',source_refs=[now,source],
                field_sources={'item':[],'amount':[source],'date':[now],'note':[]})]})
    def repair(body):
        assert 'patch_pending_records' not in {t['name'] for t in body['tools']}
        feedback=[v for v in body['input'] if v.get('type')=='function_call_output'][-1]
        result=json.loads(feedback['output']);assert result['error']=='reused_expense_fields_required'
        assert 'entries[0].item' in result['missing_fields']
        return additional(source)(body)
    r=h.send('วันที่ 27 sep ด้วย ยอดนี้',incomplete,repair)
    assert r.status=='preview' and h.state.drafts[background['draft_ref']]==background
    assert len(h.sheets.entries)==1


def test_null_values_can_copy_one_explicitly_selected_recorded_fact():
    h,source,background=setup_saved()
    def choose(body):
        now=context(body)['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[
            entry(item=None,amount=None,date_text='วันที่ 27 sep',source_refs=[now,source],
                field_sources={'item':[source],'amount':[source],'date':[now],'note':[]})]})
    before=len(h.port.requests);r=h.send('วันที่ 27 sep ด้วย ยอดนี้',choose)
    assert r.status=='preview' and len(h.port.requests)==before+1
    e=r.data['tool_result']['entries'][0]
    assert (e['item'],e['amount'],e['date'])==('Laptop Orion','1370','2026-09-27')
    assert h.state.drafts[background['draft_ref']]==background and len(h.sheets.entries)==1


def test_pending_sources_cannot_be_reused_as_an_additional_saved_payment():
    h=Harness();h.sheets.rows.append(('Laptop Orion','Gadget',''))
    prepare(h,'Laptop Orion 1370',[entry(item='Laptop Orion',amount=1370)])
    source=h.service._current['source_ref'];before=deepcopy(h.draft())
    r=h.send('วันที่ 27 sep ด้วย ยอดนี้',additional(source),response(text='ขอตรวจรายการที่รอบันทึกก่อน'))
    assert r.status!='preview' and h.draft()==before and not h.sheets.entries


def test_latest_recorded_context_is_distinct_from_editable_background_and_expires():
    h,source,background=setup_saved()
    def inspect(body):
        c=context(body);facts=c['recent_recorded_expenses']
        assert facts[0]['item']=='Laptop Orion' and facts[0]['field_sources']['amount']==[source]
        assert facts[0]['state']=='recorded' and facts[0]['editable_as_pending'] is False
        assert {v['draft_ref'] for v in c['pending_records']}=={background['draft_ref']}
        return response(text='ได้ครับ')
    h.send('ขอดูบริบท',inspect)
    h.now+=timedelta(hours=25)
    def expired(body):
        assert context(body)['recent_recorded_expenses']==[]
        return response(text='ได้ครับ')
    h.send('ขอดูบริบท',expired)


def test_signed_cold_restart_additional_payment_after_new_catalog_enrollment():
    from test_response_realization import VoiceHosted
    from test_calendar_text_confirmation_v13 import turn
    from fastapi.testclient import TestClient
    api=VoiceHosted()
    for col,value in enumerate(['อุปกรณ์หมวดทดสอบ','Gadget',''],1):api.google.cells[('Database',3,col)]=value
    def create(name,amount):
        def decide(body):
            c=context(body)
            return response(name='prepare_expenses',args={'draft_ref':None,'entries':[
                entry(item=name,amount=amount,source_refs=[c['current_source_ref']])]})
        return decide
    turn(api,'อุปกรณ์เก่าค้าง 19 บาท',create('อุปกรณ์เก่าค้าง',19))
    turn(api,'Tablet Lyra 1370',create('Tablet Lyra',1370))
    def classify(body):
        c=context(body);pending=next(v for v in c['pending_records'] if v['records'][0]['values']['item']=='Tablet Lyra')
        return response(name='patch_pending_records',args={'draft_ref':pending['draft_ref'],'draft_version':pending['version'],
            'records':[{'record_ref':pending['records'][0]['record_ref'],'changes':[
                {'field':'category','value':'Gadget','source_text':'Gadget','source_ref':c['current_source_ref']}]}]})
    review,_=turn(api,'เพิ่มใหม่หมวด Gadget',classify);assert review.status=='preview'
    saved,_=turn(api,review.data['confirm_command']);assert saved.status=='recorded'
    def copy_saved(body):
        c=context(body);facts=c['recent_recorded_expenses'][0]
        assert facts['item']=='Tablet Lyra' and facts['editable_as_pending'] is False
        refs=facts['field_sources'];now=c['current_source_ref']
        return response(name='prepare_expenses',args={'draft_ref':None,'entries':[
            entry(item=facts['item'],amount=facts['amount'],date_text='27 sep',
                source_refs=list(dict.fromkeys([now,*refs['item'],*refs['amount']])),
                field_sources={**refs,'date':[now],'note':[]})]})
    before=api.google.sheet_writes
    review,event=turn(api,'วันที่ 27 sep ด้วย ยอดนี้',copy_saved)
    assert review.status=='preview' and review.data['tool_result']['entries'][0]['date']=='2026-09-27'
    assert api.google.sheet_writes==before
    saved,event=turn(api,review.data['confirm_command']);assert saved.status=='recorded'
    after=api.google.sheet_writes;calls=len(api.model_bodies)
    with TestClient(api.app()) as client:assert api.send(client,deepcopy(event)).status_code==200
    assert api.google.sheet_writes==after and after==before+1 and len(api.model_bodies)==calls
    assert any(d['entries'][0]['item']=='อุปกรณ์เก่าค้าง' and d['status']!='recorded' for d in api.snapshot()['history24']['current']['drafts'].values())


@pytest.mark.parametrize('error,kind',[(OutcomeUnknown('private detail',reason='timeout'),'timeout'),
    (httpx.ReadTimeout('private detail'),'timeout'),(ApiError(429),'rate_limited'),
    (ApiError(503),'provider_error'),(ValueError('private detail'),'invalid_response')])
def test_planner_failure_has_safe_specific_diagnosis_and_does_not_retry(error,kind):
    h=Harness();r=h.send('Gadget',error)
    assert r.status=='model_unavailable' and r.data['model_failure']['kind']==kind
    assert 'private detail' not in json.dumps(r.data) and 'private detail' not in r.message
    assert len(h.port.requests)==1 and not h.sheets.entries
