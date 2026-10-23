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
