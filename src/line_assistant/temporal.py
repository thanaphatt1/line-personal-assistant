"""Model-interpreted temporal plans, server arithmetic and owned provenance.

This is a read/preparation helper. References are local to one owner turn;
sealed drafts retain a self-contained calculation proof, never live formulas.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import date,datetime,time,timedelta
from decimal import Decimal,InvalidOperation
import re
from typing import Literal
from pydantic import Field,ValidationError
from .tables import Strict
from .table_workflow import schema
from .history_context import digest
from .language import BANGKOK
from .tool_errors import PlanningError,recovery

NAME='resolve_temporal'


class Resolve(Strict):
    source_ref:str=Field(min_length=1,max_length=64)
    source_text:str=Field(min_length=1,max_length=200)
    kind:Literal['date','time','datetime','duration']
    mode:Literal['components','offset','weekday','duration']=Field(description='components: explicit ISO date/HH:MM interpreted from owner wording and original source timestamp; offset: server arithmetic from an offered basis; weekday: anchored weekday selection; duration: elapsed hours/minutes.')
    basis_ref:str|None=Field(default=None,description='An actual offered temporal_context basis. Null uses the original source timestamp for components/weekday; never invent a saved-event basis.')
    date:str|None=Field(default=None,description='Complete ISO date for components, otherwise null. Omitted year/month follow the original owner source timestamp.')
    time:str|None=Field(default=None,description='Bangkok HH:MM for components, otherwise null. An endpoint clock is not an elapsed duration.')
    days:int=Field(default=0,ge=-36600,le=36600)
    months:int=Field(default=0,ge=-1200,le=1200)
    years:int=Field(default=0,ge=-100,le=100)
    minutes:int=Field(default=0,ge=-10080,le=10080)
    hours:str|int|None=None
    weekday:int|None=Field(default=None,ge=0,le=6,description='Monday=0 through Sunday=6. Used only by weekday mode.')
    direction:Literal['next','previous','inclusive']|None=Field(default=None,description='weekday direction: next/previous are strict; inclusive permits the anchor date.')
    timezone:Literal['Asia/Bangkok']='Asia/Bangkok'


def definitions():
    return [{'type':'function','name':NAME,'strict':True,'description':
        'Resolve an owner temporal expression using a semantic plan and verified bases. Server computes arithmetic. Returns a current-turn temporal_ref for reviewed proposals. No writes or confirmation.',
        'parameters':schema(Resolve)}]


def context(h):
    bases={}
    def add(label,value,origin):
        ref='tb_'+digest([h.settings.owner_id,h._active_event_id,label,value,origin])[:24]
        bases[ref]={'basis_ref':ref,'label':label,'value':value,'origin':origin}
    for ref,d in h.state.calendar_drafts.items():
        try:h.calendar_flow.draft(ref)
        except ValueError:continue
        for edge in ['start','end']:
            if d.get(edge):add((d['facts']['title'] or 'นัด')+' '+edge,d[edge],{'kind':'pending_calendar','ref':ref,'version':d['version'],'edge':edge})
    for ref,read in getattr(h,'_query_reads',{}).items():
        if read.get('domain')!='calendar' or not read.get('complete'):continue
        for record in read.get('analysis_records',[]):
            for edge in ['start','end']:
                if record.get(edge):add(record.get('title','นัด')+' '+edge,record[edge],{'kind':'calendar_read','query_ref':ref,'record_ref':record.get('read_ref'),'edge':edge})
    if h.saved_edits.flags.get('calendar') and h.saved_edits.has_pending():
        try:selection=h.saved_edits.pending().payload
        except ValueError:selection={}
        if selection.get('domain')=='calendar':
            for candidate in selection.get('candidates',[]):
                for edge in ['start','end']:
                    record=candidate['record']
                    add(record['title']+' '+edge,record[edge],{'kind':'saved_candidate','ref':candidate['ref'],'version':record['version'],'edge':edge})
    h._temporal_bases=bases
    return {'temporal_context':{'timezone':'Asia/Bangkok','bases':list(bases.values()),
        'resolved':[{'temporal_ref':ref,**deepcopy(proof['values'])} for ref,proof in getattr(h,'_temporal_values',{}).items()]}}


def compute(p,basis):
    """No language grammar: typed components and calendar arithmetic only."""
    if p.mode!='components' and (p.date is not None or p.time is not None):raise PlanningError('temporal_conflicting_components')
    if p.mode!='offset' and any([p.days,p.months,p.years]) or p.mode not in {'offset','duration'} and p.minutes:
        raise PlanningError('temporal_conflicting_offset')
    if p.mode!='weekday' and (p.weekday is not None or p.direction is not None):raise PlanningError('temporal_conflicting_weekday')
    if p.mode!='duration' and p.hours is not None:raise PlanningError('temporal_conflicting_duration')
    if p.mode=='duration':
        if p.kind!='duration' or p.basis_ref is not None:raise PlanningError('temporal_kind_mismatch')
        try:
            hours=Decimal(str(p.hours or 0));total=hours*60+p.minutes
        except InvalidOperation:raise PlanningError('temporal_invalid_duration') from None
        if not total.is_finite() or hours<0 or p.minutes<0 or total!=total.to_integral_value() or not 0<total<=10080:raise PlanningError('temporal_invalid_duration')
        return {'date':None,'time':None,'datetime':None,'duration_text':f'{int(total)} นาที'}
    if p.kind=='duration':raise PlanningError('temporal_kind_mismatch')
    if p.mode=='components':
        if p.basis_ref is not None:raise PlanningError('temporal_conflicting_basis')
        day=date.fromisoformat(p.date) if p.date else None
        if p.time and not re.fullmatch(r'\d{2}:\d{2}',p.time):raise PlanningError('temporal_invalid_clock')
        clock=time.fromisoformat(p.time) if p.time else None
        if (p.kind in {'date','datetime'} and day is None) or (p.kind in {'time','datetime'} and clock is None):
            raise PlanningError('temporal_components_missing',required=[p.kind])
        if p.kind=='date' and clock is not None or p.kind=='time' and day is not None:raise PlanningError('temporal_kind_mismatch')
        instant=datetime.combine(day,clock,BANGKOK) if day and clock else None
    else:
        instant=datetime.fromisoformat(basis) if 'T' in basis else datetime.combine(date.fromisoformat(basis),time.min,BANGKOK)
        if instant.utcoffset() is None:raise PlanningError('temporal_naive_basis')
        instant=instant.astimezone(BANGKOK)
        if p.basis_ref and p.kind in {'time','datetime'} and (instant.second or instant.microsecond):raise PlanningError('temporal_precision_unsupported',kind='unsupported')
        instant=instant.replace(second=0,microsecond=0)
        if p.mode=='offset':
            month=instant.month-1+p.months+p.years*12;year=instant.year+month//12;month=month%12+1
            # Do not silently clamp Jan31 plus one month into a different day.
            if not 1900<=year<=2100 or instant.day>monthrange(year,month)[1]:raise PlanningError('temporal_invalid_date')
            instant=instant.replace(year=year,month=month)+timedelta(days=p.days,minutes=p.minutes)
        else:
            if p.weekday is None or p.direction is None:raise PlanningError('temporal_weekday_missing',required=['weekday','direction'])
            delta=(p.weekday-instant.weekday())%7
            if p.direction=='next':delta=delta or 7
            elif p.direction=='previous':delta=-((instant.weekday()-p.weekday)%7 or 7)
            instant+=timedelta(days=delta)
        day=instant.date();clock=instant.time().replace(tzinfo=None)
    if day and not 1900<=day.year<=2100:raise PlanningError('temporal_invalid_date')
    if p.kind=='date':clock=None;instant=None
    return {'date':day.isoformat() if day else None,'time':clock.strftime('%H:%M') if clock else None,
        'datetime':instant.isoformat() if instant else None,'duration_text':None}


def validate_proof(proof):
    if set(proof)!={'owner','event','source_ref','source_text','at','plan','basis','values'}:raise ValueError('Invalid temporal proof')
    if datetime.fromisoformat(proof['at']).utcoffset() is None:raise ValueError('Invalid temporal anchor')
    if not proof['source_ref'] or not isinstance(proof['source_text'],str) or not 1<=len(proof['source_text'])<=200:raise ValueError('Invalid temporal evidence')
    p=Resolve.model_validate(proof['plan'])
    if p.basis_ref is None and proof['basis']!={'value':proof['at'],'origin':{'kind':'owner_message','source_ref':proof['source_ref']}}:
        raise ValueError('Invalid temporal owner anchor')
    if p.source_ref!=proof['source_ref'] or p.source_text!=proof['source_text'] or compute(p,proof['basis']['value'])!=proof['values']:
        raise ValueError('Invalid temporal calculation')


def execute(h,args):
    try:
        p=Resolve.model_validate(args)
        rows=h._owner_sources([p.source_ref]);row=rows[0]
        if p.source_text not in row['text']:raise PlanningError('temporal_owner_evidence')
        from .calendar_dates import bangkok_zone
        bangkok_zone(p.source_text)
        context(h)
        basis=h._temporal_bases.get(p.basis_ref) if p.basis_ref else {'value':row['at'],'origin':{'kind':'owner_message','source_ref':row['source_ref']}}
        if basis is None:raise PlanningError('temporal_basis_unavailable',required=['basis_ref'])
        values=compute(p,basis['value'])
        proof={'owner':h.settings.owner_id,'event':h._active_event_id,'source_ref':row['source_ref'],'source_text':p.source_text,
            'at':row['at'],'plan':p.model_dump(),'basis':deepcopy(basis),'values':values}
        ref='temporal_'+digest(proof)[:24];h._temporal_values[ref]=proof
        return {'ok':True,'temporal_resolution':True,'continue_model':True,'temporal_ref':ref,'source_ref':row['source_ref'],'source_text':p.source_text,**values}
    except (PlanningError,ValidationError,ValueError,OverflowError) as error:
        typed=isinstance(error,PlanningError)
        message=('ตอนนี้รองรับเวลาไทย (Asia/Bangkok) กรุณาระบุเวลาไทยที่ต้องการครับ' if str(error)=='calendar_timezone_unsupported' else
            'วันเวลาที่เสนอไม่ผ่านการตรวจ จึงยังไม่ได้ใช้ค่านี้ครับ')
        return {'ok':False,'temporal_resolution':True,'error':str(error) if typed else 'temporal_invalid_arguments',
            'message':message,
            'recovery':recovery(error,kind=None if typed else 'assistant_arguments',tools=[NAME,'query_calendar','read_calendar_drafts','find_saved_records'])}


def get(h,ref,projection,*,source_ref=None,source_text=None):
    proof=getattr(h,'_temporal_values',{}).get(ref)
    if not proof or proof['owner']!=h.settings.owner_id or proof['event']!=h._active_event_id:raise PlanningError('temporal_reference_unavailable')
    if ref!='temporal_'+digest(proof)[:24]:raise PlanningError('temporal_proof_changed')
    validate_proof(proof)
    row=h._owner_sources([proof['source_ref']])[0]
    if row['at']!=proof['at'] or proof['source_text'] not in row['text']:raise PlanningError('temporal_owner_evidence')
    if source_ref is not None and source_ref!=proof['source_ref'] or source_text is not None and (
        source_text not in row['text'] or not (source_text in proof['source_text'] or proof['source_text'] in source_text)):
        raise PlanningError('temporal_evidence_mismatch')
    if proof['plan']['basis_ref']:
        context(h)
        if h._temporal_bases.get(proof['plan']['basis_ref'])!=proof['basis']:raise PlanningError('temporal_basis_changed')
    value=proof['values'].get(projection)
    if value is None:raise PlanningError('temporal_projection_unavailable')
    return value,deepcopy(proof)


def projection(field):
    return {'date_text':'date','end_date_text':'date','time_text':'time','end_time_text':'time','duration_text':'duration_text',
        'date':'date','end_date':'date','time':'time','end_time':'time'}.get(field)


def calendar_evidence(h,refs,source_refs):
    result={}
    for field,ref in refs.items():
        part=projection(field)
        if part is None:raise PlanningError('temporal_unknown_field',fields=[field])
        value,proof=get(h,ref,part)
        if proof['source_ref'] not in source_refs:raise PlanningError('temporal_source_required')
        result[field]={'role':'user','source_ref':proof['source_ref'],'text':proof['source_text'],'at':proof['at'],
            'temporal':proof,'value':value}
    return result


def interpreted_evidence(h,field,value,row):
    """Validate an inline model interpretation without parsing owner language.

    The original quotation remains evidence. ISO components/minutes are a
    reviewed semantic proposal, not proof that their meaning was understood.
    """
    part=projection(field) or field
    if part not in {'date','time','duration_text'}:
        raise PlanningError('temporal_unknown_field',fields=[field])
    args={'source_ref':row['source_ref'],'source_text':row['text'],
          'kind':'duration' if part=='duration_text' else part,
          'mode':'duration' if part=='duration_text' else 'components'}
    if part=='duration_text':
        if type(value) is not int:raise PlanningError('temporal_invalid_duration',fields=[field])
        args['minutes']=value
    else:args[part]=value
    output=execute(h,args)
    if not output.get('ok'):raise PlanningError(output['error'],fields=[field])
    actual,proof=get(h,output['temporal_ref'],part,source_ref=row['source_ref'],source_text=row['text'])
    return {**row,'value':actual,'temporal':proof}
