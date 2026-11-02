"""Timings are bounded, content-free and cannot affect owner operations."""
from line_assistant import action_trace as trace
from line_assistant.performance import summarize
from test_action_trace import pending,enroll


def test_actual_turn_report_counts_model_tools_and_fallbacks_without_content():
    h=pending();rows=[];h.service.action_trace_sink=rows.append
    enroll(h,'เพิ่มใหม่')
    report=summarize(rows)
    assert report['attempts']==1 and report['calls']['model']==1 and report['calls']['tool']==1
    assert 'model_planner' in report['latency_ms'] and report['latency_ms']['total']['p95']>=0
    assert 'เพิ่มใหม่' not in str(report) and 'mock-owner' not in str(report)


def test_metrics_survive_dropped_action_records_and_use_nearest_rank_percentiles(monkeypatch):
    rows=[];now=[0.0];monkeypatch.setattr(trace,'monotonic',lambda:now[0])
    for index in range(1,21):
        collector=trace.Trace(str(index),rows.append)
        collector.emit('model_request',role='planner',run_ref='synthetic',cached=False)
        for _ in range(trace.MAX_RECORDS):collector.emit('checkpoint')
        now[0]+=index/1000
        collector.emit('model_result',role='planner',run_ref='synthetic')
        collector.emit('reply_fallback',reason='timeout')
        collector.emit('turn_finished',final=True,status='answer')
    report=summarize(rows)
    assert report['attempts']==20 and report['reply_fallback_rate']==1
    assert report['latency_ms']['total']=={'samples':20,'p50':10,'p95':19}
    assert report['latency_ms']['model_planner']['p95']==19


def test_webhook_summary_wins_over_nested_turn_even_if_export_is_out_of_order():
    rows=[];collector=trace.Trace('synthetic',rows.append)
    collector.emit('turn_finished',final=True,status='clarification')
    collector.emit('webhook_finished',final=True)
    report=summarize([{'jsonPayload':row} for row in reversed(rows)])
    assert report['attempts']==1 and report['scopes']=={'webhook':1}
    assert report['statuses']=={'clarification':1}


def test_late_worker_cannot_emit_after_turn_is_closed():
    h=pending();rows=[];h.service.action_trace_sink=rows.append
    with trace.turn(h.service,h.event('synthetic')) as collector:
        pass
    collector.emit('reply_fallback',reason='timeout')
    assert len(rows)==1 and trace.ACTIVE.get() is None


def test_fast_replay_does_not_bias_default_latency_report():
    rows=[];collector=trace.Trace('synthetic',rows.append)
    collector.emit('receipt_replay');collector.emit('turn_finished',final=True,status='answer')
    report=summarize(rows)
    assert report['attempts']==0 and report['replay_attempts']==1
    assert summarize(rows,include_replays=True)['attempts']==1


def wrapped(rows,revision):
    return [{'resource':{'labels':{'revision_name':revision}},'jsonPayload':r} for r in rows]


def terminal(event,workflow='expense_read',**extra):
    rows=[];collector=trace.Trace(event,rows.append)
    collector.workflows.add(workflow)
    collector.emit('turn_finished',final=True,status='answer')
    rows[-1]['performance'].update(extra)
    return rows


def test_revision_workflow_and_boundary_groups_and_filters_do_not_mix_populations():
    first=wrapped(terminal('first'),'assistant-v51');second=wrapped(terminal('second','calendar_read'),'assistant-v52')
    report=summarize(first+second)
    assert [(g['revision'],g['workflow'],g['boundary'],g['attempts']) for g in report['groups']]==[
        ('assistant-v51','expense_read','assistant',1),('assistant-v52','calendar_read','assistant',1)]
    assert summarize(first+second,revision='assistant-v51')['attempts']==1
    assert summarize(first+second,workflow='calendar_read')['attempts']==1
    assert summarize(first+second,boundary='webhook')['attempts']==0
    # Conflicting metadata is unknown, not an arbitrary revision selection.
    assert summarize(first+wrapped([first[0]['jsonPayload']],'assistant-v52'))['groups'][0]['revision']=='unknown'


def test_native_read_then_respond_and_supporting_reads_keep_task_workflow():
    import json
    collector=trace.Trace('synthetic',lambda r:None)
    for request in [{'intent':'read','sources':[{'source':'expense'}]}, {'intent':'respond'}]:
        collector.emit('tool_request',tool='act',call_ref=str(request['intent']),
            arguments=trace.arguments({'arguments':json.dumps({'request':request})}))
    assert trace.workflow_name(collector.workflows)=='expense_read'
    assert trace.workflow_name({'expense_read','calendar_read'})=='activity_expense_read'
    assert trace.workflow_name({'expense_read','calendar_prepare'})=='calendar_prepare'
    assert trace.workflow_labels(None,action_kind='create_event')=={'calendar_commit'}
    assert trace.workflow_name(set())=='unknown'


def test_terminal_diagnostics_survive_cap_and_event_duplicates_are_not_counted_twice():
    rows=[];collector=trace.Trace('synthetic',rows.append)
    for _ in range(trace.MAX_RECORDS+trace.MAX_PRIORITY_RECORDS):collector.emit('checkpoint')
    collector.emit('tool_request',tool='act',arguments={'intent':'read','request':{'sources':[{'source':'expense'}]}})
    collector.emit('planning_repair');collector.emit('planning_stopped',reason='repeated_read')
    collector.emit('model_result',failure_kind='timeout');collector.emit('reply_fallback',reason='unavailable')
    collector.emit('turn_finished',final=True,status='clarification')
    report=summarize(rows+list(reversed(rows)))
    assert report['attempts']==1 and report['trace_dropped_attempts']==1
    assert report['groups'][0]['workflow']=='expense_read'
    assert report['repair_calls_per_turn']['p50']==1
    assert report['stop_reasons']=={'repeated_read':1}
    assert report['model_failure_reasons']=={'timeout':1}
    assert report['fallback_reasons']=={'unavailable':1}


def test_old_traces_infer_only_observed_safe_workflows_and_keep_missing_diagnostics_unknown():
    rows=terminal('old');last=rows[-1]
    for key in ['workflow','repairs','stop_reasons','fallback_reasons','model_failures']:last['performance'].pop(key)
    request={**last,'phase':'tool_request','sequence':100,'tool':'query','arguments':{'sources':[{'source':'calendar'}]}}
    assert summarize([request,last])['groups'][0]['workflow']=='calendar_read'
    last['dropped_records']=1
    report=summarize([request,last])
    assert report['groups'][0]['workflow']=='unknown' and report['diagnostics_complete_attempts']==0
    assert report['repair_calls_per_turn']['samples']==0


def test_incomplete_malformed_and_unknown_values_do_not_become_fake_zero_metrics():
    rows=terminal('valid');last=rows[-1];last['performance']={'total_ms':1,'workflow':[],'phase_ms':[]}
    last['resource']=[]
    incomplete={**last,'attempt_ref':'incomplete','phase':'tool_request','arguments':{'sources':None},'tool':'query'}
    invalid={**last,'attempt_ref':'invalid','performance':{'total_ms':-1}}
    report=summarize([None,{'jsonPayload':[]},incomplete,invalid,*rows])
    assert report['incomplete_attempts']==1 and report['invalid_terminal_attempts']==1
    assert report['model_calls_per_turn']['samples']==0 and report['reply_fallback_rate'] is None
    assert report['groups'][0]['revision']=='unknown'
    assert trace.workflow_labels('act',{'intent':'read','sources':[{'source':[]} ]})==set()


def test_markdown_and_cli_accept_cloud_logging_and_redact_invalid_exports(tmp_path):
    import json,subprocess,sys
    from pathlib import Path
    from line_assistant.performance import markdown
    rows=wrapped(terminal('private-attempt'),'assistant-v51')
    assert 'private-attempt' not in markdown(summarize(rows))
    path=tmp_path/'export.json';path.write_text(json.dumps({'entries':rows}))
    script=Path(__file__).resolve().parents[1]/'scripts/summarize_performance.py'
    result=subprocess.run([sys.executable,str(script),str(path),'--format','markdown'],capture_output=True,text=True)
    assert result.returncode==0 and 'assistant-v51 | expense_read' in result.stdout
    path.write_text('private raw invalid payload')
    result=subprocess.run([sys.executable,str(script),str(path)],capture_output=True,text=True)
    assert result.returncode==2 and 'private raw' not in result.stderr


def test_usage_is_numeric_only_survives_trace_cap_and_keeps_unknown_details_explicit():
    from line_assistant import action_trace
    rows=[];collector=trace.Trace('synthetic',rows.append);token=action_trace.ACTIVE.set(collector)
    try:
        for _ in range(trace.MAX_RECORDS):collector.emit('checkpoint')
        collector.emit('model_request',role='planner',run_ref='synthetic')
        action_trace.model_usage({'model':'gpt-6-luna','usage':{'input_tokens':100,'output_tokens':20,
            'input_tokens_details':{'cached_tokens':40,'private':'must not appear'},
            'output_tokens_details':{'reasoning_tokens':10,'content':'private reasoning'}}})
        collector.emit('model_result',role='planner',run_ref='synthetic')
        collector.emit('turn_finished',final=True,status='answer')
    finally:action_trace.ACTIVE.reset(token)
    usage=summarize(rows)['token_usage']
    assert usage['input_tokens']==100 and usage['output_tokens']==20
    assert usage['cached_input_tokens']==40 and usage['reasoning_tokens']==10
    assert usage['cache_write_tokens'] is None and usage['cache_write_tokens_samples']==0
    assert usage['model_calls_without_input_usage']==0
    assert 'private' not in str(rows) and 'reasoning' not in str(usage).replace('reasoning_tokens','')


def test_missing_usage_and_malformed_envelopes_are_unknown_not_zero_tokens():
    from line_assistant import action_trace
    rows=[];collector=trace.Trace('synthetic',rows.append);token=action_trace.ACTIVE.set(collector)
    try:
        collector.emit('model_request',role='planner',run_ref='synthetic')
        action_trace.model_usage({'usage':{'input_tokens':True,'output_tokens':'private value'}})
        collector.emit('turn_finished',final=True,status='model_unavailable')
    finally:action_trace.ACTIVE.reset(token)
    usage=summarize(rows)['token_usage']
    assert usage['input_tokens'] is None and usage['output_tokens'] is None
    assert usage['input_tokens_samples']==0 and usage['model_calls_without_input_usage']==1
    assert 'private value' not in str(rows)


def test_signed_http_runtime_reports_actual_planner_and_reply_usage():
    from fastapi.testclient import TestClient
    from test_response_realization import VoiceHosted
    from test_calendar_factory import prepare
    api=VoiceHosted();app=api.app();rows=[];app.state.assistant.action_trace_sink=rows.append
    with TestClient(app) as client:
        result,_=api.turn(client,'นัดตรวจฟันพรุ่งนี้บ่ายสอง 45 นาที',prepare)
    assert result.status=='preview'
    report=summarize(rows);usage=report['token_usage']
    assert report['calls']['model']==2
    assert usage['input_tokens']==2000 and usage['output_tokens']==200
    assert usage['input_tokens_samples']==2 and usage['model_calls_without_input_usage']==0
