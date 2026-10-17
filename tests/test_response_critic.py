"""Development critic is bounded and never changes the production call budget."""
import importlib.util,json
from pathlib import Path
from threading import Event,enumerate as threads
import pytest
SPEC=importlib.util.spec_from_file_location('development_critic',Path(__file__).resolve().parents[1]/'scripts/response_critic.py')
critic=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(critic)

def response(passed=True,reasons=None):
 return {'model':'gpt-6-luna','status':'completed','output':[{'type':'message','status':'completed','content':[{'type':'output_text','text':json.dumps({'pass':passed,'reasons':reasons or []})}]}]}

@pytest.mark.parametrize('passed,reasons',[(True,[]),(False,['wrong_amount']),(False,['wrong_date','wrong_entity'])])
def test_exact_gate_then_single_critic_decision(passed,reasons):
 calls=[]
 def request(body):calls.append(body);return response(passed,reasons)
 r=critic.review({'expense':'20.00'},'draft','trusted',request,deterministic_validated=True)
 assert len(calls)==1 and r['message']==('draft' if passed else 'trusted') and r['admitted']==passed
 assert calls[0]['tools']==[] and calls[0]['store'] is False and calls[0]['max_output_tokens']==256


def test_deterministic_failure_or_deadline_never_calls_model():
 def forbidden(*a):pytest.fail('critic cannot run before exact gate')
 assert critic.review({},'x','trusted',forbidden,deterministic_validated=False)['model_calls']==0
 assert critic.review({},'x','trusted',forbidden,deterministic_validated=True,remaining_seconds=1)['model_calls']==0


@pytest.mark.parametrize('value',[{'pass':True,'reasons':['wrong_amount']},{'pass':False,'reasons':[]},{'pass':'true','reasons':[]},{'pass':False,'reasons':['other']},{'pass':False,'reasons':['wrong_date','wrong_date']},{'pass':True,'reasons':[],'extra':True}])
def test_invalid_verdict_uses_trusted_fallback(value):
 v=response();v['output'][0]['content'][0]['text']=json.dumps(value)
 r=critic.review({},'x','trusted',lambda body:v,deterministic_validated=True)
 assert not r['admitted'] and r['reason']=='invalid_or_failed'


def test_timeout_returns_fallback_without_regeneration():
 release=Event();calls=[]
 def slow(body):calls.append(1);release.wait(1);return response()
 r=critic.review({},'x','trusted',slow,deterministic_validated=True,timeout=.02)
 assert r['reason']=='timeout' and r['message']=='trusted' and len(calls)==1
 release.set()
 for t in threads():
  if t.name=='development-response-critic':t.join(1)


def test_critic_does_not_change_application_factory_or_production_limits():
 from line_assistant.history_cloud_run import HISTORY_KEYS
 assert not any('CRITIC' in key for key in HISTORY_KEYS)
 assert 'maximum3' not in critic.INSTRUCTIONS
