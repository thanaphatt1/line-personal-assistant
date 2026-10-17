"""Model-selected, typed reads. No natural-language intent/date/filter parser.

The model interprets the owner's language and history. This module validates
arguments, reads fixed providers completely and admits authoritative facts.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
import logging, re
from pydantic import BaseModel, ConfigDict, Field
from .contracts import SpendingSummary
from .language import BANGKOK
from .catalog import normalized
from .model_api import object_schema
from .state import Result
from .transport import ApiError, OutcomeUnknown, NotReady

NAMES = {'query_expenses', 'query_calendar'}
MONTHS = ('ม.ค.','ก.พ.','มี.ค.','เม.ย.','พ.ค.','มิ.ย.','ก.ค.','ส.ค.','ก.ย.','ต.ค.','พ.ย.','ธ.ค.')

class Filter(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['item', 'category', 'subcategory']
    value: str = Field(min_length=1, max_length=200)

class Query(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    start_date: str | None = Field(max_length=10)
    end_date: str | None = Field(max_length=10)
    source_refs: list[str] = Field(min_length=1, max_length=16)
    date_mode: Literal['range','calendar_month','calendar_week','calendar_year'] = 'range'
    relative_offset: int | None = Field(default=None,ge=-120,le=120)

class Expenses(Query):
    filter: Filter | None
    view: Literal['total', 'list']
    measure: Literal['expense', 'income', 'balance']

class Calendar(Query):
    upcoming: bool
    filter_text: str | None = Field(default=None,max_length=200)


def definitions():
    common = {'relative_offset':{'type':['integer','null'],'description':'For THIS/NEXT/LAST calendar week/month/year use0/+1/-1; server computes from current Bangkok clock. Use null for named/absolute periods or range. Do not calculate relative frame dates yourself.'},'date_mode':{'type':'string','enum':['calendar_month','calendar_week','calendar_year','range'],'description':'Interpret the intended interval: unqualified month/monthly uses calendar_month, week uses calendar_week, year uses calendar_year. Server expands the start date to the WHOLE calendar frame. Use range for one day, explicit date spans, or explicitly month-to-date.'},
              'start_date': {'type':['string','null'], 'description':'Inclusive Bangkok ISO date; interpret natural dates using bangkok_datetime. Null for relative calendar frames or when unclear.'},
              'end_date': {'type':['string','null'], 'description':'Inclusive Bangkok ISO end; weeks Monday-Sunday. Expense max367 dates, Calendar max7.'},
              'source_refs': {'type':'array','items':{'type':'string'},'minItems':1,'maxItems':16}}
    return [
        {'type':'function','name':'query_expenses','strict':True,
         'description':'Read saved expenses, totals or entries. Interpret aliases/typos with query_catalog; choose an exact existing label. Follow-ups carry only intended prior scope; new general questions clear filters. Never write, classify unknown stored rows, or claim income/balance supported.',
         'parameters':object_schema({**common,'filter':{'anyOf':[{'type':'null'},object_schema({'kind':{'type':'string','enum':['item','category','subcategory']},'value':{'type':'string'}})]},
             'view':{'type':'string','enum':['total','list']},'measure':{'type':'string','enum':['expense','income','balance']}})},
        {'type':'function','name':'query_calendar','strict':True,
         'description':'Read SAVED appointments from the one configured Calendar, max7 dates, including all-day/transparent. upcoming=true only for next/upcoming appointments, excluding already ended events. No title/type filters; clarify unsupported filters instead of ignoring them. Never create or edit.',
         'parameters':object_schema({**common,'upcoming':{'type':'boolean'},'filter_text':{'type':['string','null'],'description':'Copy any requested saved-event title/type filter here, even though unsupported. Null only when no filter requested. Server clarifies instead of silently returning all events.'}})}]


def context(host):
    """Trusted scope and bounded catalog data for model interpretation, not rules."""
    row, previous = host._presented_result()
    scope = previous.data.get('read_query') if previous else None
    if scope and (scope.get('workbook') != host.sheets.target.spreadsheet_id or scope.get('calendar') != host.calendar_flow.calendar_id):scope = None
    if scope and getattr(host,'production',False) and (not row.get('delivery_verified') or host.state.event_workbooks.get(row['event']) != host.sheets.target.spreadsheet_id):scope = None
    try:
        tax = host._taxonomy()
        catalog = {'complete':True,'items':[r.item for r in tax.rows],
                   'categories':list(dict.fromkeys(r.category for r in tax.rows if r.category)),
                   'subcategories':list(dict.fromkeys(r.subcategory for r in tax.rows if r.subcategory))}
    except Exception:catalog = {'complete':False,'items':[],'categories':[],'subcategories':[]}
    return {'previous_query':{k:deepcopy(v) for k,v in scope.items() if k not in {'workbook','calendar'}} if scope else None,
            'query_catalog':catalog}


def label_text(value):
    value = re.sub(r'[\r\n\t\x00-\x1f]', ' ', value)
    return value[:80] + ('…' if len(value)>80 else '')


def error(output, code, outcome='unavailable'):
    logging.getLogger('line_assistant.query').info('query_failure domain=%s reason=%s', output['domain'], code)
    return {**output,'ok':False,'complete':False,'outcome':outcome,'error_code':code}


def execute(host, name, args):
    domain = 'expense' if name=='query_expenses' else 'calendar'
    output = {'status':'stored_query','domain':domain,'read_only':True,'ok':True,'complete':False,
              'financial_write_authorized':False,'calendar_write_authorized':False,
              'scope':{'domain':domain,'workbook':host.sheets.target.spreadsheet_id,'calendar':host.calendar_flow.calendar_id}}
    try:
        p = (Expenses if domain=='expense' else Calendar).model_validate(args)
        host._owner_sources(p.source_refs)
    except (ValueError,TypeError,KeyError):return error(output,'invalid_arguments','clarification')
    if domain=='expense' and p.measure!='expense':return error(output,'income_unconfigured','income_unconfigured')
    if domain=='calendar' and p.filter_text:return error(output,'calendar_filter_unsupported','clarification')
    if p.relative_offset is not None and p.date_mode=='range':return error(output,'invalid_arguments','clarification')
    relative=p.relative_offset is not None and p.date_mode!='range'
    if not relative and (not p.start_date or p.date_mode=='range' and not p.end_date):return error(output,'date_required','clarification')
    try:
        if relative:
            a=datetime.fromisoformat(host._current['at']).astimezone(BANGKOK).date()
            if p.date_mode=='calendar_month':
                y,m=divmod(a.year*12+a.month-1+p.relative_offset,12);a=date(y,m+1,1)
            elif p.date_mode=='calendar_week':a+=timedelta(weeks=p.relative_offset)
            elif p.date_mode=='calendar_year':a=date(a.year+p.relative_offset,1,1)
            b=a
        elif p.date_mode!='range':
            a=date.fromisoformat(p.start_date);b=a
            if p.end_date:date.fromisoformat(p.end_date)
        else:
            period=SpendingSummary(start_date=p.start_date,end_date=p.end_date);a,b=period.start_date,period.end_date
        if p.date_mode=='calendar_month':a,b=date(a.year,a.month,1),date(a.year,a.month,monthrange(a.year,a.month)[1])
        elif p.date_mode=='calendar_week':a=a-timedelta(days=a.weekday());b=a+timedelta(days=6)
        elif p.date_mode=='calendar_year':a,b=date(a.year,1,1),date(a.year,12,31)
        SpendingSummary(start_date=a,end_date=b)
        if not 1900<=a.year<=b.year<=2100 or domain=='calendar' and (b-a).days>6:raise ValueError()
    except ValueError:return error(output,'date_bounds','clarification')
    output['scope'].update(start=a.isoformat(),end=b.isoformat())
    if domain=='calendar':
        output['scope']['upcoming']=p.upcoming
        if not host.calendar_flow.enabled:return error(output,'calendar_disabled')
        start=datetime.combine(a,time.min,BANGKOK);end=datetime.combine(b+timedelta(days=1),time.min,BANGKOK)
        if p.upcoming:start=max(start,datetime.fromisoformat(host._current['at']).astimezone(BANGKOK))
        try:events=host.calendar_flow.adapter.saved_events(start,end) if start<end else []
        except (ApiError,OutcomeUnknown):return error(output,'provider_failure')
        except NotReady:return error(output,'read_incomplete')
        except Exception:return error(output,'schema_failure')
        return {**output,'complete':True,'outcome':'answer','events':events[:20],'count':len(events),'shown':min(20,len(events))}
    chosen=p.filter
    output['scope'].update(filter={chosen.kind:chosen.value} if chosen else None,listing=p.view=='list')
    try:
        tax=host._taxonomy() if chosen else None
        if chosen:
            choices = {r.item if chosen.kind=='item' else getattr(r,chosen.kind) for r in tax.rows}
            matches={v for v in choices if normalized(v)==normalized(chosen.value)}
            if len(matches)!=1:return error(output,'filter_unknown','clarification')
            chosen=chosen.model_copy(update={'value':matches.pop()});output['scope']['filter']={chosen.kind:chosen.value}
        entries=host.sheets.saved_entries(a,b)
        if chosen:
            if chosen.kind=='item':entries=[e for e in entries if normalized(e['item'])==normalized(chosen.value)]
            else:
                mappings={}
                for r in tax.rows:mappings.setdefault(normalized(r.item),set()).add(getattr(r,chosen.kind))
                if any(len(mappings.get(normalized(e['item']),set()))!=1 for e in entries):return error(output,'mapping_incomplete')
                entries=[e for e in entries if chosen.value in mappings[normalized(e['item'])]]
        entries.sort(key=lambda e:(e['date'],e['item']))
        total=sum((Decimal(e['amount']) for e in entries),Decimal('0'))
        if not total.is_finite() or total<0:raise ValueError()
    except (ApiError,OutcomeUnknown):return error(output,'provider_failure')
    except NotReady:return error(output,'schema_failure')
    except Exception:return error(output,'schema_failure')
    return {**output,'complete':True,'outcome':'summary','expense':f'{total:.2f}','count':len(entries),
            'entries':entries[:20] if p.view=='list' else [],'shown':min(20,len(entries)) if p.view=='list' else 0,'income_supported':False}


def day_label(value):
    day=date.fromisoformat(value)
    return f'{day.day} {MONTHS[day.month-1]} {day.year}'


def period_label(scope, at):
    a,b=map(date.fromisoformat,(scope['start'],scope['end']));today=at.astimezone(BANGKOK).date()
    if a==b:
        prefix={-1:'เมื่อวาน',0:'วันนี้',1:'พรุ่งนี้'}.get((a-today).days)
        return (prefix+' ('+day_label(a.isoformat())+')') if prefix else day_label(a.isoformat())
    return day_label(a.isoformat())+' ถึง '+day_label(b.isoformat())


ERRORS = {
 'invalid_arguments':'ข้อมูลที่ใช้ค้นหายังไม่ชัดเจนครับ ช่วยระบุช่วงวันหรือสิ่งที่ต้องการดูอีกครั้ง',
 'date_required':'ต้องการดูช่วงวันไหนครับ?',
 'date_bounds':'ช่วงวันที่ไม่ถูกต้องหรือยาวเกินขอบเขตครับ รายจ่ายดูได้ไม่เกิน 367 วัน และปฏิทินไม่เกิน 7 วัน',
 'income_unconfigured':'ยังไม่มีข้อมูลรายรับที่รองรับครับ จึงสรุปรายรับหรือยอดคงเหลือไม่ได้',
 'filter_unknown':'ยังจับคู่ชื่อหรือหมวดที่ต้องการกับฐานข้อมูลไม่ได้ครับ ช่วยระบุให้ชัดอีกนิด',
 'mapping_incomplete':'มีรายการในช่วงนี้ที่จับคู่หมวดไม่ได้ครับ จึงยังรวมยอดเฉพาะหมวดนี้ให้ครบไม่ได้',
 'provider_failure':'ตอนนี้เชื่อมต่อแหล่งข้อมูลไม่สำเร็จครับ เลยยังสรุปผลให้ไม่ได้',
 'schema_failure':'รูปแบบข้อมูลที่อ่านมายังตรวจสอบไม่ผ่านครับ จึงยังสรุปยอดหรือยืนยันว่าไม่มีรายการไม่ได้',
 'read_incomplete':'อ่านข้อมูลได้ไม่ครบครับ จึงยังยืนยันรายการทั้งหมดหรือการไม่มีนัดไม่ได้',
 'calendar_filter_unsupported':'ตอนนี้อ่านนัดได้ตามช่วงวันครับ ยังกรองชื่อหรือประเภทนัดไม่ได้ ต้องการดูทุกนัดในช่วงนั้นไหมครับ?',
 'calendar_disabled':'ยังไม่ได้เปิดการอ่านปฏิทินครับ จึงตรวจนัดที่บันทึกไว้ไม่ได้'}


def facts(output, at):
    if not output['complete']:return {'reason':ERRORS[output['error_code']]}
    scope=output['scope'];values={'period':period_label(scope,at)}
    if scope['domain']=='expense':
        if scope.get('filter'):values['filter']=label_text(next(iter(scope['filter'].values())))
        values.update(total=output['expense']+' บาท',count=str(output['count'])+' รายการ')
        for i,e in enumerate(output['entries'],1):values[f'row{i}']=f"{day_label(e['date'])} — {label_text(e['item'])} {Decimal(e['amount']):.2f} บาท"
    else:
        values['count']=str(output['count'])+' นัด'
        for i,e in enumerate(output['events'],1):
            if e['all_day']:
                last=(date.fromisoformat(e['end'])-timedelta(days=1)).isoformat()
                when=day_label(e['start'])+(' ถึง '+day_label(last) if last!=e['start'] else '')+' ทั้งวัน'
            else:
                start,end=(datetime.fromisoformat(e[k]).astimezone(BANGKOK) for k in ['start','end'])
                when=day_label(start.date().isoformat())+' '+start.strftime('%H:%M')+' ถึง '+(day_label(end.date().isoformat())+' ' if start.date()!=end.date() else '')+end.strftime('%H:%M')
            values[f'row{i}']=when+' — '+label_text(e['title'])
        if scope.get('upcoming'):values['upcoming']='เฉพาะนัดที่ยังไม่จบตั้งแต่เวลาปัจจุบัน'
    if output['count']>20 and output['shown']:values['truncation']='แสดง 20 รายการแรก ยอดและจำนวนรวมใช้ข้อมูลทั้งหมดที่อ่านครบแล้ว' if scope['domain']=='expense' else 'แสดง 20 รายการแรกจากนัดทั้งหมดที่อ่านครบแล้ว'
    return values


def result(output, at):
    values=facts(output,at)
    if not output['complete']:message=values['reason']
    elif output['domain']=='expense':
        message=values['period']+(' เฉพาะ '+values['filter'] if 'filter' in values else '')+f" ใช้ไป {values['total']} จาก {values['count']}ครับ"
    else:message=values['period']+f" มี {values['count']}ครับ"
    message+=''.join('\n'+v for k,v in values.items() if k.startswith('row') or k in {'truncation','upcoming'})
    data={'domain':output['domain'],'read_only':True,'financial_write_authorized':False,'calendar_write_authorized':False,
          'read_query':deepcopy(output['scope']),'query_facts':values,'tool_result':output}
    return Result(output['outcome'],message,data)


# Compatibility for non-raw legacy applications; not exposed to the raw planner.
def summary_output(totals, dates):
    for key in ('expense','income'):
        if key=='income' and totals.get('income_supported') is False and totals.get(key) is None:continue
        value=Decimal(totals[key])
        if not value.is_finite() or value<0:raise ValueError('invalid_summary_result')
    return {'ok':True,'status':'summary','range':dates,'expense':str(Decimal(totals['expense'])),'income_supported':False,'read_only':True,'financial_write_authorized':False}


def summary_result(output):
    return Result('summary',f"ช่วง {output['range']['start_date']} ถึง {output['range']['end_date']} รายจ่าย {Decimal(output['expense']):.2f} บาท\nยังไม่มีแหล่งข้อมูลรายรับที่รองรับ จึงสรุปรายรับหรือยอดคงเหลือไม่ได้",{'tool_result':output})
