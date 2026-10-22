"""Tool-declared recovery; no global tool/error-name allowlist or attempt cap."""
from types import SimpleNamespace
import json
import pytest
from line_assistant.tool_repair import RepairLoop
from line_assistant.tool_errors import recovery


def host(monkeypatch):
    monkeypatch.setattr('line_assistant.pending_records.views',lambda h:[])
    return SimpleNamespace(_query_analysis_active=False,state=SimpleNamespace(raw_tool_definitions=[
        {'name':'future_metadata_tool','parameters':{'type':'object','properties':{'value':{'type':'string'}}}}]))


def call(value):return {'name':'future_metadata_tool','arguments':json.dumps({'value':value})}


def test_new_tool_and_new_error_repair_without_editing_a_central_list(monkeypatch):
    loop=RepairLoop(host(monkeypatch))
    output={'ok':False,'error':'new_validator_diagnostic','recovery':recovery(kind='assistant_arguments',fields=['value'])}
    feedback=loop.feedback(call('bad'),output)
    assert feedback['repair_feedback']['allowed_tools']==['future_metadata_tool']
    assert 'future_metadata_tool' in feedback['repair_feedback']['schemas']


def test_more_than_two_distinct_validation_steps_can_progress(monkeypatch):
    loop=RepairLoop(host(monkeypatch))
    for n in range(5):
        output={'ok':False,'error':f'validation_stage_{n}','recovery':recovery(kind='assistant_arguments',fields=[f'field_{n}'])}
        assert loop.feedback(call(n),output) is not None
    assert loop.attempts==5


@pytest.mark.parametrize('kind',['owner_input','unsupported','unavailable',None])
def test_missing_owner_input_unsupported_and_unknown_failures_do_not_retry(monkeypatch,kind):
    loop=RepairLoop(host(monkeypatch));output={'ok':False,'error':'arbitrary'}
    if kind:output['recovery']=recovery(kind=kind,required=['one specific owner fact'])
    assert loop.feedback(call('bad'),output) is None and loop.attempts==0


def test_different_arguments_with_identical_diagnostics_are_not_progress(monkeypatch):
    loop=RepairLoop(host(monkeypatch))
    def output():return {'ok':False,'error':'same_problem','recovery':recovery(kind='assistant_arguments',fields=['value'])}
    assert loop.feedback(call('first'),output())
    bad=output();assert loop.feedback(call('second'),bad) is None
    assert bad['repair_stopped']=='repeated_failure'


def test_inventing_different_unknown_fields_or_targets_is_not_progress(monkeypatch):
    loop=RepairLoop(host(monkeypatch))
    for n in range(2):
        request={'name':'future_metadata_tool','arguments':json.dumps({'draft_ref':f'invented_{n}','field':f'fake_{n}'})}
        bad={'ok':False,'error':f'unknown_field:fake_{n}','recovery':recovery(kind='assistant_arguments',fields=[f'fake_{n}'])}
        feedback=loop.feedback(request,bad)
        assert (feedback is not None)==(n==0)
    assert bad['repair_stopped']=='repeated_failure'


def test_tool_suggested_repair_scope_is_intersected_with_actual_offered_tools(monkeypatch):
    loop=RepairLoop(host(monkeypatch))
    output={'ok':False,'error':'bad','recovery':recovery(kind='assistant_arguments',tools=['invented_write_tool'])}
    assert loop.feedback(call('bad'),output)['repair_feedback']['allowed_tools']==['future_metadata_tool']


def test_real_pending_patch_can_fix_three_distinct_errors_before_succeeding():
    from test_pending_records import pending,patch
    from test_weekday_enrollment_v11 import active
    h=pending();count=len(h.port.requests)
    def damage(field,value):
        def mutate(args):args['records'][0]['changes'][0][field]=value
        return mutate
    plans=[patch({0:[('category','Food/Grocery','อาหาร')]},mutate=damage(field,value)) for field,value in [
        ('field','catagory'),('value','Not offered'),('source_text','not an owner span')]]
    r=h.send('อาหาร',*plans,patch({0:[('category','Food/Grocery','อาหาร')]}))
    assert len(h.port.requests)==count+4 and active(h)['entries'][0]['mapping']['category']=='Food/Grocery'
    assert not h.sheets.entries
