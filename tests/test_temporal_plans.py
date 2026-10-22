"""Semantic temporal plans with actual workflow arithmetic; synthetic only."""
from copy import deepcopy
from datetime import timedelta
import json,socket
import pytest

from line_assistant.temporal import Resolve,compute
from line_assistant.history_context import digest,HistoryState
from line_assistant.history_persistence import history_snapshot,restore_history
from test_calendar_mvp import Harness,complete,response
from test_pending_records import patch


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic temporal providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def resolve(text,*,basis=None,source=None,**plan):
    def choose(body):
        c=json.loads(body['input'][0]['content'])
        values={'source_ref':source or c['current_source_ref'],'source_text':text,**plan}
        if basis:
            values['basis_ref']=next(v['basis_ref'] for v in c['temporal_context']['bases'] if basis(v))
        result=response('resolve_temporal',values)
        result['output'][0]['call_id']='resolve_'+digest(values)[:20]
        return result
    return choose


def resolved(body):
    return [json.loads(v['output']) for v in body['input'] if v.get('type')=='function_call_output' and json.loads(v['output']).get('temporal_ref')][-1]


def apply(fields,*,domain='calendar',ordinal=0):
    def choose(body):
        value=resolved(body)
        from line_assistant.temporal import projection
        def cite(args):
            for change in args['records'][0]['changes']:
                change['temporal_ref']=value['temporal_ref'];change['source_ref']=value['source_ref']
        return patch({ordinal:[(field,value[projection(field)],value['source_text']) for field in fields]},domain=domain,mutate=cite)(body)
    return choose


def test_earlier_than_pending_appointment_computes_times_and_keeps_duration():
    h=Harness();complete(h);before=deepcopy(h.draft())
    text='ขอเริ่มก่อนนัดเดิมสักครึ่งชั่วโมง'
    r=h.send(text,resolve(text,kind='datetime',mode='offset',minutes=-30,basis=lambda b:b['origin'].get('edge')=='start'),apply(['date_text','time_text']))
    assert r.status=='preview',r
    d=h.draft();assert d['start']=='2026-10-17T13:30:00+07:00' and d['end']=='2026-10-17T14:15:00+07:00'
    assert d['facts']['title']==before['facts']['title'] and not h.provider.posts
    assert d['anchors']['time_text']['temporal']['plan']['minutes']==-30
    snap=history_snapshot(h.state);state=HistoryState();state.clock=lambda:h.now;state.raw_journal=h.state.raw_journal
    restore_history(state,snap,'mock-owner');other=Harness(state=state,provider=h.provider);other.now=h.now;other.counter=20
    assert other.draft()['start']==d['start']
    event=other.event(r.data['confirm_command']);saved=other.app.handle(event)
    assert saved.status=='created' and h.provider.posts==1
    assert other.app.handle(deepcopy(event))==saved and h.provider.posts==1


def test_redundant_original_duration_keeps_its_proof_while_time_changes():
    h=Harness();complete(h);before=deepcopy(h.draft())
    text='ขอเริ่มก่อนนัดเดิมครึ่งชั่วโมง โดยใช้ระยะเวลาเดิม'
    def change(body):
        result=apply(['time_text'])(body)
        args=json.loads(result['output'][0]['arguments'])
        args['records'][0]['changes'].append({'field':'duration_text','value':before['facts']['duration_text'],
            'source_text':'ระยะเวลาเดิม','source_ref':json.loads(body['input'][0]['content'])['current_source_ref']})
        result['output'][0]['arguments']=json.dumps(args,ensure_ascii=False)
        return result
    r=h.send(text,resolve('ก่อนนัดเดิมครึ่งชั่วโมง',kind='datetime',mode='offset',minutes=-30,
        basis=lambda b:b['origin'].get('edge')=='start'),change)
    assert r.status=='preview',r
    d=h.draft();assert d['start']=='2026-10-17T13:30:00+07:00' and d['end']=='2026-10-17T14:15:00+07:00'
    assert d['anchors']['duration_text']==before['anchors']['duration_text']
    assert d['review']['nonce']!=before['review']['nonce'] and not h.provider.posts


@pytest.mark.parametrize('span,accepted',[
    ('ก่อนนัดเดิมครึ่งชั่วโมง',True),
    ('ขอเริ่มก่อนนัดเดิมครึ่งชั่วโมง โดยใช้ระยะเวลาเดิม',True),
    ('โดยใช้ระยะเวลาเดิม',False),
])
def test_citation_can_shrink_or_expand_the_same_resolved_owner_span(span,accepted):
    h=Harness();complete(h);text='ขอเริ่มก่อนนัดเดิมครึ่งชั่วโมง โดยใช้ระยะเวลาเดิม'
    expression='ขอเริ่มก่อนนัดเดิมครึ่งชั่วโมง'
    def change(body):
        v=resolved(body)
        def cite(args):args['records'][0]['changes'][0]['temporal_ref']=v['temporal_ref']
        return patch({0:[('time_text',v['time'],span)]},domain='calendar',mutate=cite)(body)
    r=h.send(text,resolve(expression,kind='datetime',mode='offset',minutes=-30,
        basis=lambda b:b['origin'].get('edge')=='start'),change,response(text='ต้องอ้างช่วงที่ใช้คำนวณครับ'))
    assert (r.status=='preview')==accepted,r
    assert h.draft()['facts']['time_text']==('13:30' if accepted else 'บ่ายสอง') and not h.provider.posts


@pytest.mark.parametrize('pair',[False,True])
def test_offset_across_midnight_requires_the_computed_date_projection(pair):
    h=Harness();h.prepare('นัดดาว วันที่ 20 ตุลา 00:15 นาน 45 นาที',title='ดาว',date_text='วันที่ 20 ตุลา',time_text='00:15',duration_text='45 นาที')
    text='เริ่มก่อนเดิมครึ่งชั่วโมง'
    r=h.send(text,resolve(text,kind='datetime',mode='offset',minutes=-30,basis=lambda b:b['origin'].get('edge')=='start'),
        apply(['date_text','time_text'] if pair else ['time_text']),response(text='ต้องใช้วันที่ก่อนหน้าคู่กับเวลาครับ'))
    if pair:
        assert r.status=='preview' and h.draft()['start']=='2026-10-19T23:45:00+07:00'
        assert h.draft()['end']=='2026-10-20T00:30:00+07:00'
    else:assert r.status!='preview' and h.draft()['facts']['time_text']=='00:15'
    assert not h.provider.posts


def test_new_calendar_event_can_use_a_resolved_datetime_and_duration():
    h=Harness();text='เพิ่มนัดดาว พรุ่งนี้ช่วงสามโมงเย็น ใช้เวลาชั่วโมงครึ่ง'
    def prepare(body):
        c=json.loads(body['input'][0]['content']);refs=[json.loads(v['output']) for v in body['input'] if v.get('type')=='function_call_output']
        dt,span=[v for v in refs if v.get('temporal_ref')]
        return response('prepare_calendar_event',{'draft_ref':None,'draft_version':None,'title':'ดาว','source_refs':[c['current_source_ref']],
            'temporal_refs':[{'field':field,'temporal_ref':value['temporal_ref']} for field,value in [('date_text',dt),('time_text',dt),('duration_text',span)]]})
    r=h.send(text,resolve('พรุ่งนี้ช่วงสามโมงเย็น',kind='datetime',mode='components',date='2026-10-17',time='15:00'),
        resolve('ชั่วโมงครึ่ง',kind='duration',mode='duration',hours='1.5'),prepare)
    assert r.status=='preview',r
    assert h.draft()['start']=='2026-10-17T15:00:00+07:00' and h.draft()['end']=='2026-10-17T16:30:00+07:00' and not h.provider.posts


def test_expense_patch_uses_resolved_original_owner_date_without_future_bypass():
    from test_weekday_enrollment_v11 import harness,active
    from test_catalog_names_v09 import prepare,entry
    h=harness();prepare(h,'ร้านทดสอบโคเมต 47 บาท',[entry(item='ร้านทดสอบโคเมต',amount=47)])
    text='แก้เป็นวันที่ก่อนวันนี้สองวัน'
    r=h.send(text,resolve(text,kind='date',mode='offset',days=-2),apply(['date_text'],domain='expense'))
    assert r.status=='preview',r
    assert active(h)['entries'][0]['date']=='2026-10-14' and not h.sheets.entries
    r=h.send('แก้เป็นวันพรุ่งนี้',resolve('วันพรุ่งนี้',kind='date',mode='offset',days=1),apply(['date_text'],domain='expense'),response(text='รายจ่ายยังใช้วันอนาคตไม่ได้ครับ'))
    assert r.status!='preview' and active(h)['entries'][0]['date']=='2026-10-14' and not h.sheets.entries
    snap=history_snapshot(h.state);state=HistoryState();state.clock=lambda:h.now;state.raw_journal=h.state.raw_journal
    restore_history(state,snap,'mock-owner')
    assert next(iter(state.drafts.values()))['entries'][0]['date']=='2026-10-14'


def test_new_expense_temporal_date_keeps_historical_provenance_after_cold_restore():
    from test_weekday_enrollment_v11 import harness,active
    from test_catalog_names_v09 import entry
    h=harness();h.send('วันที่ที่หมายถึงคือวันศุกร์ก่อน',response(text='รับทราบ'))
    ref=h.service._current['source_ref'];at=h.service._current['at']
    def proposal(body):
        c=json.loads(body['input'][0]['content']);v=resolved(body)
        return response('prepare_expenses',{'draft_ref':None,'draft_version':None,'entries':[
            entry(item='ร้านทดสอบโคเมต',amount=47,date_ref=v['temporal_ref'],source_refs=[c['current_source_ref']])]})
    r=h.send('ร้านทดสอบโคเมต 47 บาท ใช้วันที่ที่บอกไว้',resolve('วันศุกร์ก่อน',source=ref,kind='date',mode='weekday',weekday=4,direction='previous'),proposal)
    assert r.status=='preview',r
    e=active(h)['entries'][0]
    assert e['date']=='2026-10-09' and e['fact_sources']['date']==[ref] and e['fact_times']['date']==at
    snap=history_snapshot(h.state);state=HistoryState();state.clock=lambda:h.now;state.raw_journal=h.state.raw_journal
    restore_history(state,snap,'mock-owner');assert not h.sheets.entries


def test_generic_date_column_uses_temporal_reference_without_a_date_language_parser():
    from test_table_workflow import harness as tables,prepare as table_prepare
    h=tables();text='รายรับของวันที่สิบห้าเดือนตุลาปีสองพันยี่สิบหก 1000'
    def proposal(body):
        c=json.loads(body['input'][0]['content']);v=resolved(body)
        return response('prepare_table_change',{'table_id':'family_income','operation':'insert','record_id':None,'draft_ref':None,
            'source_refs':[c['current_source_ref']],'values':[{'field':'date','value':v['date'],'source_text':v['source_text'],'temporal_ref':v['temporal_ref']},
                {'field':'amount','value':'1000','source_text':'1000'}]})
    r=h.send(text,resolve('วันที่สิบห้าเดือนตุลาปีสองพันยี่สิบหก',kind='date',mode='components',date='2026-10-15'),proposal)
    assert r.status=='preview' and r.data['table_draft']['values']['date']=='2026-10-15',r
    assert h.app.table_flow.registry.stores['family_income'].writes==0


def test_saved_calendar_reschedule_uses_the_same_basis_and_keeps_etag_confirmation():
    from test_saved_record_workflow import SavedHosted,turn,find
    api=SavedHosted();text='เลื่อนนัดวันที่ 5 พ.ย. ให้เริ่มก่อนเดิมครึ่งชั่วโมง'
    def proposal(body):
        c=json.loads(body['input'][0]['content']);v=resolved(body)
        return response('prepare_calendar_reschedule',{'candidate_ref':c['saved_record_edits']['candidates'][0]['candidate_ref'],
            'date':v['date'],'time':v['time'],'end_date':None,'end_time':None,'date_text':v['source_text'],'time_text':v['source_text'],
            'end_date_text':None,'end_time_text':None,'source_refs':[c['current_source_ref']],
            'temporal_refs':[{'field':f,'temporal_ref':v['temporal_ref']} for f in ['date','time']]})
    r,_=turn(api,text,find('calendar'),resolve('ก่อนเดิมครึ่งชั่วโมง',kind='datetime',mode='offset',minutes=-30,
        basis=lambda b:b['origin']['kind']=='saved_candidate' and b['origin']['edge']=='start'),proposal)
    assert r.status=='preview' and '08:30' in r.message,r
    assert api.saved_calendar.patches==0
    saved,_=turn(api,'ยืนยัน');assert saved.status=='updated' and api.saved_calendar.patches==1


def test_calendar_read_can_supply_a_basis_for_a_verified_temporal_answer():
    from test_calendar_semantic_queries import SemanticHosted
    from test_calendar_text_confirmation_v13 import turn
    from test_query_analysis_loop import read
    api=SemanticHosted();api.calendar.events={'star':{'id':'star','summary':'นัดดาว',
        'start':{'dateTime':'2026-11-03T09:00:00+07:00'},'end':{'dateTime':'2026-11-03T10:00:00+07:00'}}}
    r,_=turn(api,'วันที่ 3 พ.ย. ถ้าก่อนนัดดาวครึ่งชั่วโมงจะกี่โมง',read('calendar','2026-11-03'),
        resolve('ก่อนนัดดาวครึ่งชั่วโมง',kind='datetime',mode='offset',minutes=-30,
            basis=lambda b:b['origin']['kind']=='calendar_read' and b['origin']['edge']=='start'),response(text='เวลาที่คำนวณได้ครับ'))
    assert r.status=='answer' and r.data['tool_result']['time']=='08:30' and '08:30' in r.message,r
    assert not api.calendar.posts and not api.google.sheet_writes


@pytest.mark.parametrize('mode,values,expected',[
    ('weekday',{'weekday':4,'direction':'next'},'2026-10-23'),
    ('weekday',{'weekday':4,'direction':'inclusive'},'2026-10-16'),
    ('weekday',{'weekday':4,'direction':'previous'},'2026-10-09'),
    ('offset',{'days':2},'2026-10-18'),
    ('offset',{'months':1},'2026-11-16'),
    ('offset',{'years':1},'2027-10-16'),
])
def test_calendar_arithmetic_is_server_computed(mode,values,expected):
    p=Resolve(source_ref='owner',source_text='synthetic owner wording',kind='date',mode=mode,**values)
    assert compute(p,'2026-10-16T10:00:00+07:00')['date']==expected


@pytest.mark.parametrize('plan',[
    {'kind':'date','mode':'components','date':'2026-02-30'},
    {'kind':'time','mode':'components','time':'25:00'},
    {'kind':'time','mode':'components','time':'09:00:30'},
    {'kind':'duration','mode':'duration','hours':'0.001'},
    {'kind':'duration','mode':'duration','hours':'NaN'},
    {'kind':'duration','mode':'duration','minutes':0},
    {'kind':'date','mode':'offset','months':1},
])
def test_invalid_components_and_undefined_month_rollover_are_not_guessed(plan):
    p=Resolve(source_ref='owner',source_text='synthetic',**plan)
    with pytest.raises((ValueError,ArithmeticError)):compute(p,'2026-01-31T10:00:00+07:00')


def test_foreign_zone_is_declared_unsupported_and_not_retried():
    h=Harness();r=h.send('เวลา 09:00 JST',resolve('09:00 JST',kind='time',mode='components',time='09:00'),response(text='must not run'))
    assert len(h.model.requests)==1 and r.data['tool_result']['recovery']['kind']=='unsupported' and not h.provider.posts


def test_resolved_references_are_not_available_in_a_later_owner_turn():
    h=Harness();complete(h)
    first=h.send('ขอวันอีกสองวัน',resolve('อีกสองวัน',kind='date',mode='offset',days=2),response(text='รับทราบ'))
    ref=first.data['tool_result']['temporal_ref'];before=deepcopy(h.draft()['facts'])
    def corrupt(args):
        value=args['records'][0]['changes'][0];value['temporal_ref']=ref
    r=h.send('ใช้วันนั้น',patch({0:[('date_text','2026-10-18','ใช้วันนั้น')]},domain='calendar',mutate=corrupt),response(text='ต้องคำนวณอ้างอิงรอบนี้ใหม่ครับ'))
    assert r.status!='preview' and h.draft()['facts']==before and not h.provider.posts


@pytest.mark.parametrize('field',['values','basis','at','owner'])
def test_persisted_temporal_proof_tampering_is_rejected_even_with_a_new_draft_digest(field):
    from line_assistant.calendar_workflow import fingerprint
    h=Harness();complete(h);text='ก่อนเดิมครึ่งชั่วโมง'
    h.send(text,resolve(text,kind='datetime',mode='offset',minutes=-30,basis=lambda b:b['origin'].get('edge')=='start'),apply(['date_text','time_text']))
    snap=history_snapshot(h.state);calendar=snap['calendar_v1'];ref=next(iter(calendar['drafts']));d=calendar['drafts'][ref]
    proof=d['anchors']['time_text']['temporal']
    if field=='values':proof['values']['time']='02:00'
    elif field=='basis':proof['basis']['value']='2026-10-18T14:00:00+07:00'
    elif field=='at':proof['at']='no timestamp'
    else:proof['owner']='another-owner'
    calendar['proofs'][ref]={'owner_id':'mock-owner','version':d['version'],'fingerprint':fingerprint(d)}
    state=HistoryState();state.clock=lambda:h.now;state.raw_journal=h.state.raw_journal
    with pytest.raises(ValueError):restore_history(state,snap,'mock-owner')
