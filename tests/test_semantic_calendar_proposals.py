"""Semantic plans keep lexical parsers out of current Calendar preparation."""
from copy import deepcopy
import json,socket
import pytest
from line_assistant import calendar_sources,calendar_workflow
from line_assistant.calendar_workflow import FIELDS
from line_assistant.history_persistence import history_snapshot,restore_history
from line_assistant.history_context import HistoryState
from test_calendar_mvp import Harness,response,ProviderFailure
from test_calendar_multiline_metadata import proposed,TITLE,LAYOUTS
from test_pending_records import patch


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def plan(body,change=None,batch=False):
    reply=proposed(body,batch=batch)
    args=json.loads(reply['output'][0]['arguments'])
    if change:change(args,body)
    return response('prepare_calendar_events' if batch else 'prepare_calendar_event',args)


def restore(h):
    snap=history_snapshot(h.state);state=HistoryState();state.clock=lambda:h.now;state.raw_journal=h.state.raw_journal
    restore_history(state,snap,'mock-owner')
    other=Harness(state=state,provider=h.provider);other.now=h.now;other.counter=h.counter+50
    return other


@pytest.mark.parametrize('batch',[False,True])
def test_current_preparation_does_not_use_legacy_language_interpretation(monkeypatch,batch):
    def obsolete(*a,**k):raise AssertionError('Legacy parser must not run')
    for name in ['units','substantive','sharing','shared_title','shared_clock','shared_changes','repeated_names','temporal_occurrences']:
        monkeypatch.setattr(calendar_sources,name,obsolete)
    for name in ['event_date','event_time','duration']:
        monkeypatch.setattr(calendar_workflow,name,obsolete)
    h=Harness();result=h.send(LAYOUTS[0].format(title=TITLE),lambda body:plan(body,batch=batch))
    assert result.status=='preview' and len(h.model.requests)==1 and h.provider.posts==0
    assert restore(h).send(result.data['confirm_command']).status=='created'
    assert h.provider.posts==1


@pytest.mark.parametrize('change',[
    lambda p:p['fields']['date_text'].update(value='2026-02-30'),
    lambda p:p['fields']['time_text'].update(value='25:00'),
    lambda p:p['fields']['date_text']['source'].update(source_ref='unoffered-source'),
    lambda p:p['fields']['title'].update(value='invented-title'),
    lambda p:p['fields']['time_text']['source'].update(text='never written'),
    lambda p:p.update(source_refs=[]),
])
def test_bad_evidence_or_types_never_publish_a_confirmable_proposal(change):
    h=Harness()
    result=h.send(LAYOUTS[0].format(title=TITLE),lambda body:plan(body,lambda p,b:change(p)))
    assert result.status=='clarification' and h.provider.posts==0 and 'confirm_command' not in result.data
    assert not h.state.calendar_drafts


def test_explicit_uncertainty_survives_restore_and_blocks_confirmation():
    h=Harness()
    result=h.send(LAYOUTS[0].format(title=TITLE),lambda body:plan(body,
        lambda p,b:p.update(uncertain_fields=['date_text'])))
    assert result.status=='clarification' and 'calendar_date_unclear' in h.draft()['issues']
    other=restore(h)
    assert 'calendar_date_unclear' in other.draft()['issues']
    assert other.send('ยืนยัน').status!='created' and h.provider.posts==0


def test_approximation_stays_visible_and_unsupported_requests_cannot_be_confirmed():
    h=Harness()
    result=h.send(LAYOUTS[0].format(title=TITLE),lambda body:plan(body,
        lambda p,b:p.update(approximate_fields=['time_text'])))
    assert result.status=='preview' and 'เวลาเริ่มเป็นค่าประมาณ' in result.message
    h=Harness()
    result=h.send(LAYOUTS[0].format(title=TITLE),lambda body:plan(body,
        lambda p,b:p.update(unsupported_features=['recurrence'])))
    assert result.status=='clarification' and 'confirm_command' not in result.data
    assert h.provider.posts==0


def test_unsupported_preparation_can_be_restated_without_a_stuck_pending_flag():
    h=Harness();text=LAYOUTS[0].format(title=TITLE)
    result=h.send(text,lambda body:plan(body,lambda p,b:p.update(unsupported_features=['recurrence'])))
    assert result.status=='clarification' and not h.state.calendar_drafts
    original=h.app._current['source_ref']
    def one_time(body):
        args=json.loads(proposed(body)['output'][0]['arguments'])
        args['source_refs'].append(original)
        for fact in args['fields'].values():
            if fact:fact['source']['source_ref']=original
        return response('prepare_calendar_event',args)
    result=h.send('เอาแค่นัดครั้งเดียว',one_time)
    assert result.status=='preview' and h.draft()['facts']['title']==TITLE and h.provider.posts==0


def test_resolving_uncertainty_to_the_same_value_keeps_the_new_owner_evidence():
    h=Harness()
    result=h.send(LAYOUTS[0].format(title=TITLE),lambda body:plan(body,lambda p,b:p.update(uncertain_fields=['date_text'])))
    assert result.status=='clarification'
    result=h.send('ใช้วันที่นั้นแน่นอน',patch({0:[('date_text','2026-11-07','วันที่นั้นแน่นอน')]},domain='calendar'))
    assert result.status=='preview' and h.draft()['semantic_uncertain_fields']==[]
    assert h.draft()['anchors']['date_text']['temporal']['source_text']=='วันที่นั้นแน่นอน'
    assert h.provider.posts==0


@pytest.mark.parametrize('certainty,expected',[('approximate','preview'),('uncertain','clarification')])
def test_pending_temporal_qualifiers_are_model_metadata_not_keyword_rules(certainty,expected):
    h=Harness();h.send(LAYOUTS[0].format(title=TITLE),proposed)
    def qualify(args):args['records'][0]['changes'][0]['certainty']=certainty
    result=h.send('ใช้ช่วงก่อนบ่ายโมงตามที่เลือก',patch({0:[('time_text','12:15','ช่วงก่อนบ่ายโมง')]},domain='calendar',mutate=qualify))
    assert result.status==expected and h.provider.posts==0
    if certainty=='approximate':assert 'เวลาเริ่มเป็นค่าประมาณ' in result.message
    else:assert 'confirm_command' not in result.data


def test_changed_duration_replaces_the_old_endpoint_and_uses_server_arithmetic():
    h=Harness();h.send(LAYOUTS[0].format(title=TITLE),proposed)
    result=h.send('เปลี่ยนเป็นชั่วโมงครึ่ง',patch({0:[('duration_text',90,'ชั่วโมงครึ่ง')]},domain='calendar'))
    assert result.status=='preview' and h.draft()['facts']['end_time_text'] is None
    assert h.draft()['end']=='2026-11-07T13:30:00+07:00' and h.provider.posts==0


def test_a_bad_interval_returns_to_the_model_for_a_same_turn_patch_repair():
    h=Harness();h.send(LAYOUTS[0].format(title=TITLE),lambda body:plan(body,batch=True))
    result=h.send('ขอเริ่มเที่ยงครึ่ง',
        patch({0:[('time_text','14:30','เที่ยงครึ่ง')]},domain='calendar_batch'),
        patch({0:[('time_text','12:30','เที่ยงครึ่ง')]},domain='calendar_batch'))
    assert result.status=='preview',result
    assert h.draft()['start']=='2026-11-07T12:30:00+07:00' and h.provider.posts==0


def test_mode_is_interpreted_from_quoted_owner_words_without_an_all_day_phrase_list():
    h=Harness();text=f'เพิ่ม {TITLE} Nov 7, 2026 ตั้งแต่ต้นวันจนหมดวัน'
    def all_day(p,body):
        p['fields'].update(time_text=None,end_time_text=None,all_day={'value':True,
            'source':{'source_ref':p['source_refs'][0],'text':'ตั้งแต่ต้นวันจนหมดวัน','occurrence':0}})
    result=h.send(text,lambda body:plan(body,all_day))
    assert result.status=='preview' and h.draft()['all_day'] is True
    assert h.draft()['start']=='2026-11-07' and h.draft()['end']=='2026-11-08'
    assert restore(h).send(result.data['confirm_command']).status=='created'


def test_shared_fields_are_applied_to_each_model_selected_member_without_propagation_rules():
    h=Harness();second='กิจกรรมกลุ่มทดลองอีกเรื่อง'
    text=f'{TITLE} / {second}\nNov 7, 2026\n12:00 to 13:00'
    def grouped(p,body):
        other=deepcopy(p['events'][0]);other['fields']['title']['value']=second
        other['fields']['title']['source']['text']=second;p['events'].append(other)
    result=h.send(text,lambda body:plan(body,grouped,batch=True))
    assert result.status=='preview' and len(h.state.calendar_drafts)==2
    assert {d['facts']['title'] for d in h.state.calendar_drafts.values()}=={TITLE,second}
    assert {d['start'] for d in h.state.calendar_drafts.values()}=={'2026-11-07T12:00:00+07:00'}
    assert h.provider.posts==0
    assert restore(h).send(result.data['confirm_command']).status=='created' and h.provider.posts==2


@pytest.mark.parametrize('batch',[False,True])
def test_pending_date_patch_uses_typed_fields_without_retargeting_or_clauses(monkeypatch,batch):
    h=Harness();result=h.send(LAYOUTS[0].format(title=TITLE),lambda body:plan(body,batch=batch))
    old=result.data['confirm_command']
    def obsolete(*a,**k):raise AssertionError('Do not reinterpret a verified patch')
    for name in ['units','sharing','shared_changes','selected_ordinals']:
        if hasattr(calendar_sources,name):monkeypatch.setattr(calendar_sources,name,obsolete)
    result=h.send('เอาเป็นวันถัดจากนั้นหนึ่งสัปดาห์แทน',
        patch({0:[('date_text','2026-11-14','วันถัดจากนั้นหนึ่งสัปดาห์')]},domain='calendar_batch' if batch else 'calendar'))
    assert result.status=='preview',result
    assert h.draft()['start']=='2026-11-14T12:00:00+07:00' and h.provider.posts==0
    assert h.send(old).status=='stale'
    assert restore(h).send(result.data['confirm_command']).status=='created' and h.provider.posts==1


def test_invalid_pending_patch_cannot_publish_a_replacement_review():
    h=Harness()
    result=h.send(LAYOUTS[0].format(title=TITLE),lambda body:plan(body,batch=True))
    result=h.send('เลื่อนไปเวลาที่ไม่ถูกต้อง',patch({0:[('time_text','26:00','เวลาที่ไม่ถูกต้อง')]},domain='calendar_batch'))
    assert result.status=='clarification' and h.provider.posts==0
    # Validation happens before the patch mutation; a failed preflight does
    # not itself authorize any write or fabricate a replacement review.
    assert 'confirm_command' not in result.data


@pytest.mark.parametrize('committed',[False,True])
def test_unknown_creation_is_reconciled_without_a_second_post(committed):
    h=Harness();result=h.send(LAYOUTS[0].format(title=TITLE),proposed)
    if committed:h.provider.fail_after_create=True
    else:h.provider.fail_before_create=True
    event=h.event(result.data['confirm_command'])
    with pytest.raises(ProviderFailure):h.app.handle(event)
    assert h.provider.posts==1
    h.provider.fail_after_create=h.provider.fail_before_create=False
    other=restore(h);again=other.app.handle(deepcopy(event))
    assert again.status==('created' if committed else 'uncertain') and h.provider.posts==1


def test_current_tool_schemas_require_explicit_interpretation_and_quoted_sources():
    from line_assistant.calendar_proposals import definitions
    for tool in definitions():
        assert tool['strict'] is True
        params=tool['parameters']
        event=params['properties']['events']['items'] if tool['name'].endswith('events') else params
        if '$ref' in event:event=params['$defs'][event['$ref'].rsplit('/',1)[-1]]
        assert {'fields','source_refs'}<=set(event['required'])
        assert 'source' not in event['properties']
        assert event['additionalProperties'] is False
        facts=event['properties']['fields']
        if '$ref' in facts:facts=params['$defs'][facts['$ref'].rsplit('/',1)[-1]]
        assert {*FIELDS,'all_day'}==set(facts['required'])
        assert 'title' in facts['properties']


def test_schema_annotation_cleanup_preserves_business_properties_named_title_and_default():
    from line_assistant.tables import Strict
    from line_assistant.table_workflow import schema
    from line_assistant.calendar_proposals import compact_schema
    class BusinessFields(Strict):
        title:str
        default:str|None=None
    for document in [schema(BusinessFields),compact_schema(BusinessFields)]:
        assert set(document['properties'])=={'title','default'}
        assert set(document['required'])=={'title','default'}
