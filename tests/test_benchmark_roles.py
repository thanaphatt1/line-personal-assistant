import sys,json
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from benchmark_driver import RoleRunner
from benchmark_roles import rate,percentile,validate_config
CONFIG=json.loads((Path(__file__).resolve().parents[1]/'benchmarks/v1/config.json').read_text())
BODY={'model':'gpt-6-luna','store':False,'max_output_tokens':256}
def fake(role,body):return {'model':body['model'],'usage':{'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':20}}}

def test_role_models_are_explicit_and_default_budget_remains_two():
 runner=RoleRunner(CONFIG,fake);runner.begin_turn('test')
 runner.request('query_planner',BODY);runner.request('response_style',BODY)
 with pytest.raises(ValueError,match='turn_budget'):runner.request('critic',BODY)
 assert len(runner.records)==2

def test_three_calls_are_explicit_development_only():
 runner=RoleRunner(CONFIG,fake);runner.begin_turn('test',development_critic=True)
 for role in ['query_planner','response_style','critic']:runner.request(role,BODY)
 assert len(runner.records)==3 and all(r['status']=='received' for r in runner.records)
 with pytest.raises(ValueError,match='turn_budget'):runner.request('critic',BODY)


def test_unknown_model_never_reaches_transport():
 config=json.loads(json.dumps(CONFIG));config['roles']['critic']='UNAPPROVED_PLACEHOLDER'
 with pytest.raises(ValueError,match='unapproved_model'):RoleRunner(config,lambda *a:pytest.fail('paid'))


def test_intervals_and_latency_are_explicit_for_small_samples():
 assert rate(0,0)['rate'] is None and rate(10,10)['wilson95'][0]<1
 assert percentile([1,2,3,4,5],.95)==5 and percentile([], .5) is None


def test_unknown_usage_retains_bound_without_fake_zero():
 runner=RoleRunner(CONFIG,lambda *a: {'model':'gpt-6-luna'});runner.begin_turn('test')
 with pytest.raises(ValueError,match='usage_unknown'):runner.request('critic',BODY)
 assert runner.records[0]['status']=='failed_or_usage_unknown' and 'estimated_usage_usd' not in runner.records[0]
