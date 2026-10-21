"""Operational evidence has no owner content and never controls an effect."""
from copy import deepcopy
import json

import pytest
from line_assistant import action_trace as trace
from test_catalog_followup_focus import pending,enroll
from test_catalog_names_v09 import harness,prepare,entry
from test_history_first import response


def test_tool_trace_records_omitted_ref_actual_selected_ref_and_version_change():
    h=pending();rows=[];h.service.action_trace_sink=rows.append
    old=h.draft()['version'];target=h.draft()['entries'][0]['entry_ref']
    r=enroll(h,'ไม่แน่ใจว่าในรายการมั้ย ถ้ายังไม่มีก็เพิ่มใหม่')
    assert r.status=='clarification'
    request=next(v for v in rows if v['phase']=='tool_request')
    result=next(v for v in rows if v['phase']=='tool_result')
    assert request['tool']=='prepare_item_enrollment'
    assert request['arguments']['entry_ref_supplied'] is False
    assert request['arguments']['resolution_step']=='begin_new_item'
    assert request['state']['drafts'][0]['version']==old
    assert result['result']['entry_ref']==trace.ref(target)
    assert result['result']['next_field']=='category'
    assert result['state']['drafts'][0]['version']==old+1
    assert rows[0]['phase']=='turn_started' and rows[-1]['phase']=='turn_finished'
    assert [v['sequence'] for v in rows]==list(range(len(rows)))
    assert len({v['turn_ref'] for v in rows})==len({v['attempt_ref'] for v in rows})==1
    encoded=json.dumps(rows,ensure_ascii=False)
    for private in ['หอยทอด','หมูย่าง','ไม่แน่ใจ','mock-owner','history-2',target,h.draft()['draft_ref'],'source_quote','50']:
        # Digits can occur by chance in hashes/timings; money is checked by
        # explicit absence of fields below instead of a substring assertion.
        if private!='50':assert private not in encoded
    assert '"amount":' not in encoded and '"text":' not in encoded


def test_trace_retains_actual_error_code_without_generic_diagnosis_or_raw_arguments():
    h=pending();rows=[];h.service.action_trace_sink=rows.append
    r=enroll(h,'เลือกชื่อไหนก็ได้',entry_ref='private-bad-entry')
    assert r.status=='clarification'
    result=next(v for v in rows if v['phase']=='tool_result')['result']
    assert result['ok'] is False and result['error']=='ambiguous_unknown_item'
    assert 'private-bad-entry' not in json.dumps(rows)


def test_replay_has_same_turn_new_attempt_and_no_model_or_tool_execution():
    h=pending();rows=[];h.service.action_trace_sink=rows.append
    args={'draft_ref':h.draft()['draft_ref'],'draft_version':h.draft()['version'],'entry_ref':None,
          'resolution_step':'begin_new_item','category':None,'subcategory':None,'existing_item':None}
    event=h.event('เพิ่มใหม่');h.port.values=[response(name='prepare_item_enrollment',args=args)]
    first=h.service.handle(event);before=len(h.port.requests);first_rows=deepcopy(rows);rows.clear()
    assert h.service.handle(deepcopy(event))==first and len(h.port.requests)==before
    assert rows[0]['turn_ref']==first_rows[0]['turn_ref']
    assert rows[0]['attempt_ref']!=first_rows[0]['attempt_ref']
    assert not any(v['phase'] in {'tool_request','model_request'} for v in rows)
    assert rows[-1]['phase']=='turn_finished'


def test_provider_failure_and_process_interruption_are_visible_without_exception_messages():
    h=harness();rows=[];h.service.action_trace_sink=rows.append
    r=h.send('สวัสดี',RuntimeError('synthetic-secret-provider-body'))
    assert r.status=='model_unavailable'
    assert any(v['phase']=='model_result' and v['status']=='model_unavailable' for v in rows)
    assert 'synthetic-secret-provider-body' not in json.dumps(rows)
    rows.clear()
    def crash(*args):raise RuntimeError('synthetic-secret-snapshot')
    h.service._route=crash
    with pytest.raises(RuntimeError):h.send('ต่อครับ')
    assert rows[-1]['phase']=='turn_interrupted'
    assert 'synthetic-secret-snapshot' not in json.dumps(rows)
    assert trace.ACTIVE.get() is None


def test_trace_sink_failure_cannot_change_preparation_or_consent():
    h=pending()
    def broken(row):raise OSError('synthetic logging unavailable')
    h.service.action_trace_sink=broken
    r=enroll(h,'เพิ่มใหม่')
    assert r.data['tool_result']['item_enrollment']['field']=='category'
    assert not h.sheets.entries and trace.ACTIVE.get() is None


def test_trace_stops_recording_at_bound_but_reports_dropped_records():
    rows=[];collector=trace.Trace('synthetic',rows.append)
    for _ in range(trace.MAX_RECORDS+8):collector.emit('checkpoint')
    collector.emit('turn_finished',final=True,dropped_records=collector.dropped)
    assert len(rows)==trace.MAX_RECORDS+1 and rows[-1]['dropped_records']==8


def test_hosted_default_emits_json_logs_for_owner_only(capsys):
    h=pending();h.service.production=True
    # A failed delivered-question gate still produces exact error evidence.
    enroll(h,'เพิ่มใหม่')
    records=[json.loads(v) for v in capsys.readouterr().out.splitlines()]
    assert records and records[-1]['event']=='assistant_action_trace'
    event=h.event('private foreign text');event['source']['userId']='foreign-private-owner'
    assert h.service.handle(event).status=='denied' and not capsys.readouterr().out


def test_trace_links_prepared_executing_and_completed_action_without_write_payload():
    h=harness();rows=[];h.service.action_trace_sink=rows.append
    r=prepare(h,'กาแฟ 37 บาท',[entry(item='กาแฟ',amount=37)])
    assert r.status=='preview';rows.clear()
    saved=h.service.handle(h.event(r.data['confirm_command']))
    assert saved.status=='recorded'
    checkpoints=[v for v in rows if v['phase']=='checkpoint' and v['action_ref']]
    assert {'prepared','executing','action_completed'}<={v['checkpoint'] for v in checkpoints}
    assert len({v['action_ref'] for v in checkpoints})==1
    assert not any(k in json.dumps(rows,ensure_ascii=False) for k in ['กาแฟ','"amount":','"payload":','"nonce":'])


def test_argument_redaction_covers_generic_table_values_and_physical_coordinates():
    args={'table_id':'private-people','storage':'private-workbook','fields':{'secret_person':'private-person'},
          'source_text':'private phrase','source_refs':['private-source'],'amount':'private-amount',
          'draft_ref':'private-draft','entry_ref':'private-row'}
    result=trace.arguments({'arguments':json.dumps(args)})
    assert not any(value in json.dumps(result) for value in ['private-person','private-workbook','private-amount','private-source','private-draft','private-row','secret_person'])
