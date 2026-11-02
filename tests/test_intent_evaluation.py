"""Calibrate independent intent oracles against native, signed synthetic flows."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from semantic_oracle import aggregate,expected_rows,fingerprint,rows_key,score_turn
from semantic_harness import run_case,AttemptFailure,received_usage
from reference_intent_plans import ReferenceModel
from evaluate_intents import CORPUS,Replay,verify_manifest,validate

FIXTURES=json.loads((CORPUS/'fixtures.json').read_text())
CASES=json.loads((CORPUS/'development.json').read_text())
PLANS=json.loads((CORPUS/'reference-plans.json').read_text())


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('No model/provider network or credentials in calibration')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def run(id,*,sql=None):
    case=next(c for c in CASES if c['id']==id);plans=deepcopy(PLANS[id])
    if sql:plans[0]['sql']=sql
    return run_case(case,FIXTURES,ReferenceModel(plans))


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_scripted_positive_controls_match_independent_oracle(case):
    result=run(case['id'])
    assert all(v is not False for t in result['turns'] for v in t['scores'].values())
    for turn in result['turns']:
        assert turn['scores']['human_answer_review']=='pending'
    assert result['request_hashes']


@pytest.mark.parametrize('id,sql',[
    ('food-exclude-drink',"SELECT SUM(amount) AS total FROM e WHERE category='Food'"),
    ('bts-total','SELECT SUM(amount) AS total FROM e'),
    ('duplicate-payments',"SELECT COUNT(DISTINCT date) AS n FROM e WHERE item='BTS'"),
    ('grouped-item-total','SELECT item,SUM(amount) AS total FROM e GROUP BY item ORDER BY total DESC,item'),
    ('activity-dates-no-duplicate-money',"SELECT SUM(e.amount) AS total FROM e JOIN c ON e.date=c.start_date WHERE c.title='สอน'"),
])
def test_valid_sql_and_successful_response_cannot_self_award_semantic_pass(id,sql):
    scores=run(id,sql=sql)['turns'][0]['scores']
    assert scores=={'intent_outcome':False,'execution_contract':True,'structural_grounding':True,
                    'authorized_effects':True,'human_answer_review':'pending'}


def test_projection_aliases_and_order_do_not_require_a_specific_tool_or_sql_string():
    result=run('bts-total',sql="WITH chosen AS (SELECT amount FROM e WHERE item='BTS') SELECT SUM(amount) AS renamed FROM chosen")
    assert result['turns'][0]['scores']['intent_outcome'] is True
    assert rows_key([{'renamed':'0.30'}])==rows_key([['0.3']])
    assert rows_key([['BTS',40],['BTS',40]])!=rows_key([['BTS',40]])
    assert rows_key([['A',1],['B',2]],ordered=True)!=rows_key([['B',2],['A',1]],ordered=True)


def test_unreported_success_cannot_hide_a_failed_or_unrelated_final_selection():
    turn=run('bts-total')['turns'][0];observation=turn['observation']
    assert observation['reported_results']
    observation['reported_results']=[]
    scores=score_turn(FIXTURES,next(c for c in CASES if c['id']=='bts-total')['turns'][0]['gold'],observation)
    assert scores['intent_outcome'] is False and scores['execution_contract'] is False


def test_exact_business_effects_are_not_inferred_from_status_or_net_write_count():
    result=run('calendar-confirm-replay');turn=result['turns'][1];observation=turn['observation']
    key=next(k for k in observation['after']['calendar'] if k not in observation['before']['calendar'])
    observation['after']['calendar'][key]['title']='wrong appointment'
    gold=next(c for c in CASES if c['id']=='calendar-confirm-replay')['turns'][1]['gold']
    assert score_turn(FIXTURES,gold,observation)['authorized_effects'] is False
    # Preview content is checked independently even when no write occurred.
    preview=result['turns'][0]['observation'];preview['result']['data']['calendar_view']['facts']['title']='wrong'
    gold=next(c for c in CASES if c['id']=='calendar-confirm-replay')['turns'][0]['gold']
    assert score_turn(FIXTURES,gold,preview)['intent_outcome'] is False


def test_model_callback_gets_runtime_context_only_and_request_bound_replay_is_exact():
    case=next(c for c in CASES if c['id']=='bts-total');reference=ReferenceModel(PLANS[case['id']]);tape=[]
    def model(body):
        encoded=json.dumps(body)
        assert '"gold"' not in encoded and 'reference-plans' not in encoded and 'intent_outcome' not in encoded
        value=reference(body);tape.append({'request_sha256':fingerprint(body),'response':value});return value
    first=run_case(case,FIXTURES,model);replay=Replay(tape)
    second=run_case(case,FIXTURES,replay);replay.finish()
    assert first['request_hashes']==second['request_hashes']
    assert first['turns'][0]['scores']==second['turns'][0]['scores']
    with pytest.raises(ValueError,match='mismatch'):Replay(tape)({'changed':True})
    with pytest.raises(ValueError,match='Unused'):Replay(tape).finish()


def test_callback_failure_cannot_turn_into_a_passing_clarification_or_hidden_retry():
    case=next(c for c in CASES if c['id']=='ambiguous-period');calls=[]
    def unavailable(body):calls.append(1);raise TimeoutError('synthetic failure')
    with pytest.raises(AttemptFailure) as failure:run_case(case,FIXTURES,unavailable)
    assert failure.value.partial['callback_failures']==['TimeoutError']
    assert failure.value.partial['received_usage']==[None]
    assert len(calls)==1


def test_aggregate_counts_repetitions_separately_and_na_is_not_a_zero():
    first=run('ambiguous-period');second=deepcopy(first)
    second['turns'][0]['scores']['authorized_effects']=False
    report=aggregate([first,second])
    assert report['scenario_count']==1 and report['attempts']==2 and report['turns']==2
    assert report['intent_outcome']=={'pass':0,'fail':0,'not_applicable':2}
    assert report['scenarios_all_observed_checks_passed']==0


def test_corpus_binding_covers_current_source_runner_and_candidate_labels():
    assert validate()['scenarios']=={'development':25,'candidate-holdout':8}
    assert len(verify_manifest())==64
    assert expected_rows(FIXTURES,CASES[0]['turns'][0]['gold'])==[['100']]


def test_failed_later_turn_retains_earlier_observations_without_becoming_a_pass():
    case=next(c for c in CASES if c['id']=='followup-frequency-money');reference=ReferenceModel(PLANS[case['id']]);calls=[]
    def model(body):
        calls.append(1)
        if len(calls)>2:raise TimeoutError('synthetic later failure')
        return reference(body)
    with pytest.raises(AttemptFailure) as failure:run_case(case,FIXTURES,model)
    partial={**failure.value.partial,'status':'failed'}
    assert len(partial['turns'])==1 and len(partial['request_hashes'])==3 and len(calls)==3
    assert aggregate([partial])['failed_attempts']==1
    assert aggregate([partial])['scenarios_all_observed_checks_passed']==0
    assert received_usage({'usage':{'input_tokens':100,'private':'ignored','input_tokens_details':{'cached_tokens':50,'text':'ignored'}}})=={'input_tokens':100,'input_tokens_details':{'cached_tokens':50}}


def test_expected_unavailability_requires_the_correct_failure_and_actual_fault():
    turn=run('provider-unavailable')['turns'][0];observation=turn['observation']
    gold=next(c for c in CASES if c['id']=='provider-unavailable')['turns'][0]['gold']
    observation['provider_faults']=0
    assert score_turn(FIXTURES,gold,observation)['intent_outcome'] is False
    observation['provider_faults']=1;observation['result']['data']['tool_result']['error_code']='date_required'
    assert score_turn(FIXTURES,gold,observation)['intent_outcome'] is False


def test_cli_rejects_public_output_and_stale_tapes_without_printing_source(tmp_path):
    import subprocess
    script=CORPUS.parents[1]/'scripts/evaluate_intents.py'
    invalid=subprocess.run([sys.executable,str(script),'--reference','--output',str(CORPUS/'must-not-exist.json')],capture_output=True,text=True)
    assert invalid.returncode==2 and not (CORPUS/'must-not-exist.json').exists()
    tape=tmp_path/'private.json';tape.write_text(json.dumps({'manifest_sha256':'stale-private-source'}))
    invalid=subprocess.run([sys.executable,str(script),'--replay',str(tape),'--output',str(tmp_path/'report.json')],capture_output=True,text=True)
    assert invalid.returncode==2 and 'stale-private-source' not in invalid.stderr


def test_native_calendar_dates_are_tool_independent_and_preserve_duplicate_occurrences():
    result=run('calendar-list')['turns'][0]['observation'];gold=next(c for c in CASES if c['id']=='calendar-list')['turns'][0]['gold']
    events=[{'title':r['title'],'start':r['date']+'T09:00:00+07:00'} for r in FIXTURES['calendar'] if '2026-10-01'<=r['date']<='2026-10-31']
    # Midnight-boundary UTC and date-only provider forms have the same Bangkok date.
    events[0]['start']='2026-09-30T18:00:00Z';events[1]['start']='2026-10-01'
    result['reported_results']=[{'domain':'calendar','ok':True,'complete':True,'events':events,'count':len(events)}]
    assert score_turn(FIXTURES,gold,result)['intent_outcome'] is True
    result['reported_results'][0]['events']=events[1:]
    assert score_turn(FIXTURES,gold,result)['intent_outcome'] is False


def test_native_calendar_count_uses_complete_event_count_without_sql_column_requirements():
    result=run('calendar-duplicates')['turns'][0]['observation'];gold=next(c for c in CASES if c['id']=='calendar-duplicates')['turns'][0]['gold']
    result['reported_results']=[{'domain':'calendar','ok':True,'complete':True,'events':[],'count':5}]
    assert score_turn(FIXTURES,gold,result)['intent_outcome'] is True
    result['reported_results'][0]['count']=4
    assert score_turn(FIXTURES,gold,result)['intent_outcome'] is False


def test_native_fallback_grounding_reproduces_rejection_and_actual_delivered_values():
    from types import SimpleNamespace
    from dataclasses import replace
    from line_assistant.state import Result
    from semantic_harness import grounded_document
    case=next(c for c in CASES if c['id']=='bts-total');reference=ReferenceModel(PLANS[case['id']])
    tape=SimpleNamespace(model_bodies=[],model_outputs=[])
    def model(body):
        value=reference(body)
        for call in value.get('output',[]):
            if call.get('name')!='act':continue
            args=json.loads(call['arguments'])
            if args['request']['intent']=='respond':
                args['request']['document']['blocks'].insert(0,{'kind':'text','template':'999 บาท'})
                call['arguments']=json.dumps(args,ensure_ascii=False)
        tape.model_bodies.append(deepcopy(body));tape.model_outputs.append(deepcopy(value));return value
    turn=run_case(case,FIXTURES,model)['turns'][0]
    assert all(turn['scores'][key] is True for key in ['intent_outcome','execution_contract','structural_grounding','authorized_effects'])
    result=Result(**turn['observation']['result'])
    assert result.data['response_realization']['status']=='fallback'
    assert grounded_document(tape,result,0) is True
    assert grounded_document(tape,replace(result,message='999 บาท'),0) is False
    changed=deepcopy(result);changed.data['response_realization']['validation_code']='invented'
    assert grounded_document(tape,changed,0) is False


def test_fallback_status_cannot_self_award_grounding_to_an_accepted_model_document():
    from types import SimpleNamespace
    from line_assistant.state import Result
    from semantic_harness import grounded_document
    case=next(c for c in CASES if c['id']=='bts-total');reference=ReferenceModel(PLANS[case['id']])
    tape=SimpleNamespace(model_bodies=[],model_outputs=[])
    def model(body):
        value=reference(body);tape.model_bodies.append(deepcopy(body));tape.model_outputs.append(deepcopy(value));return value
    result=Result(**run_case(case,FIXTURES,model)['turns'][0]['observation']['result'])
    result.data['response_realization'].update(status='fallback',reason='invalid_answer_document',validation_code='unsupported_answer_literal')
    assert grounded_document(tape,result,0) is False
