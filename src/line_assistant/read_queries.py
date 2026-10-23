"""Model-selected, typed reads. No natural-language intent/date/filter parser.

The model interprets the owner's language and history. This module validates
arguments, reads fixed providers completely and admits authoritative facts.
"""
from .expense_storage import storage_scope
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
from .history_context import digest
from .state import Result
from .transport import ApiError, OutcomeUnknown, NotReady
from .calendar_provider import MAX_QUERY_DAYS

NAMES = {'query_expenses', 'query_calendar'}
MONTHS = ('ม.ค.','ก.พ.','มี.ค.','เม.ย.','พ.ค.','มิ.ย.','ก.ค.','ส.ค.','ก.ย.','ต.ค.','พ.ย.','ธ.ค.')
WEEKDAYS = ('จันทร์','อังคาร','พุธ','พฤหัสบดี','ศุกร์','เสาร์','อาทิตย์')

class Filter(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['item', 'category', 'subcategory']
    value: str = Field(min_length=1, max_length=200)

class Query(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    start_date: str | None = Field(max_length=10)
    end_date: str | None = Field(max_length=10)
    source_refs: list[str] = Field(min_length=1, max_length=16)
    date_mode: Literal['range','calendar_month','calendar_week','calendar_year','previous','upcoming'] = 'range'
    period_text: str | None = Field(default=None,max_length=120)
    relative_offset: int | None = Field(default=None,ge=-120,le=120)
    continue_analysis: bool=False

class Expenses(Query):
    filter: Filter | None
    view: Literal['total', 'list']
    measure: Literal['expense', 'income', 'balance']
    group_by: Literal['none','item','date','category','subcategory']='none'
    order_by: Literal['date','amount','count','label']='date'
    descending: bool=False
    limit: int=Field(default=20,ge=1,le=20)
    calendar_query_refs: list[str]=Field(default_factory=list,max_length=16)

class Calendar(Query):
    upcoming: bool
    filter_text: str | None = Field(default=None,max_length=200)


def definitions():
    common = {'period_text':{'type':['string','null'],'description':'Exact contiguous date/period span from a cited original owner message. Null if no period was stated; the clock alone is not owner evidence. A filter/view-only follow-up uses date_mode=previous. Preserve coordinated wording rather than joining separated spans.'},'relative_offset':{'type':['integer','null'],'description':'THIS/NEXT/LAST calendar week/month/year uses0/+1/-1; the server computes from Bangkok now. Null for named/absolute frames or range.'},'date_mode':{'type':'string','enum':['calendar_month','calendar_week','calendar_year','previous','upcoming','range'],'description':'calendar_month/week/year expands to the WHOLE frame, including dates after today; week is Monday-Sunday. previous retains the verified prior scope for a follow-up. upcoming uses now through next6dates for next-appointment intent. range is one day, an explicit span, or explicitly to-date. Interpret stated Thai/Buddhist years against the owner time context.'},
              'start_date': {'type':['string','null'], 'description':'Bangkok ISO anchor date, required for a named/absolute frame with relative_offset=null. Null for a relative-offset frame, previous/upcoming, or genuinely missing period. period_text is evidence, not a parsed anchor.'},
              'end_date': {'type':['string','null'], 'description':'Inclusive Bangkok ISO end; weeks Monday-Sunday. Expense max367 dates, Calendar max31.'},
              'continue_analysis':{'type':'boolean','description':'True when this read is part of multi-step analysis, comparison or combining Expense/Calendar; inspect its query_ref and continue with read tools, then finish_query_analysis. False for a simple standalone answer.'},
              'source_refs': {'type':'array','items':{'type':'string'},'minItems':1,'maxItems':16}}
    return [
        {'type':'function','name':'query_expenses','strict':True,
         'description':'Read complete saved expenses. For filtering/analysis prefer FULL period filter=null, view=list, continue_analysis=true THEN query_relations SQL. Legacy filter means equality/INCLUSION ONLY; it CANNOT exclude, negate or combine conditions. Never use it for ไม่รวม/ยกเว้น. Simple unfiltered totals/listing may answer directly. Legacy grouping/sorting runs before display limits. For expenses on activity dates FIRST query_calendar with continue_analysis=true, THEN pass its query_ref in calendar_query_refs; never use an activity as an expense category. Follow-ups reuse verified dates with date_mode=previous. No writes or income/balance.',
         'parameters':object_schema({**common,'filter':{'description':'Legacy exact positive equality only. Prefer null with continue_analysis=true and apply all requested conditions in query_relations SQL. A non-null filter selects ONLY the label; it never means excluding it.','anyOf':[{'type':'null'},object_schema({'kind':{'type':'string','enum':['item','category','subcategory']},'value':{'type':'string'}})]},
             'view':{'type':'string','enum':['total','list']},'measure':{'type':'string','enum':['expense','income','balance']},
             'group_by':{'type':'string','enum':['none','item','date','category','subcategory'],'description':'none=individual payments; item/date/category/subcategory=SUM amounts and COUNT payments per group. Unknown category mappings fail honestly.'},
             'order_by':{'type':'string','enum':['date','amount','count','label'],'description':'Sort the FULL result before top limit. amount is numeric; count requires groups. date requires individual rows or date groups.'},
             'descending':{'type':'boolean','description':'True for largest/highest first; false for smallest/earliest first.'},
             'limit':{'type':'integer','minimum':1,'maximum':20,'description':'Displayed top rows/groups; totals use ALL matching payments.'},
             'calendar_query_refs':{'type':'array','items':{'type':'string'},'maxItems':16,'description':'Current-request complete Calendar query refs whose matching events define the Bangkok DATES to include. Empty for ordinary expenses. Server unions dates, clips spans and never double-counts same-day expenses. date_mode=previous can use these Calendar periods directly. Re-read Calendar on a new turn; refs cannot be reused across turns.'}})},
        {'type':'function','name':'query_calendar','strict':True,
         'description':'Read saved appointments from all server-selected readable calendars, max31 dates, including all-day/transparent. Reads the full requested period and semantically classifies events by the requested condition, preserving uncertainty. Next-appointment intent uses upcoming; an explicit calendar frame uses its own date_mode. Never creates or edits.',
         'parameters':object_schema({**common,'upcoming':{'type':'boolean','description':'True only for next/upcoming appointment intent; false for an explicit day, week, month or year scope.'},'filter_text':{'type':['string','null'],'description':'Literal appointment subject/type/condition from a cited owner message; server classifies fetched events semantically. Null only when no filter requested.'}})}]


def context(host):
    """Trusted scope and bounded catalog data for model interpretation, not rules."""
    scope=None
    # Read focus may survive a clarification/chat turn. This is intentionally
    # separate from _presented_result: write consent still uses the latest reply.
    for row in sorted(host.state.history,key=lambda r:datetime.fromisoformat(r['at']),reverse=True):
        if row['role']!='assistant' or row['event']==host._active_event_id or datetime.fromisoformat(row['at'])>=datetime.fromisoformat(host._current['at']):continue
        receipt=host.state.receipts.get(row['event'])
        if not receipt or receipt.expires_at<=host.clock():continue
        candidate=receipt.result.data.get('read_query')
        if not candidate or not candidate.get('start') or not candidate.get('end'):continue
        if candidate.get('workbook')!=storage_scope(host.sheets) or candidate.get('calendar')!=host.calendar_flow.calendar_id:continue
        if candidate.get('domain')=='calendar' or candidate.get('calendar_basis'):
            if candidate.get('calendar_read_all',False)!=getattr(host.calendar_flow.adapter,'read_all_enabled',False):continue
            if candidate.get('calendar_read_ids_hash')!=calendar_read_ids_hash(host):continue
        if getattr(host,'production',False):
            from . import semantic_tools
            reply=host.state.replies.get(row['event'])
            if (not row.get('delivery_verified') or not reply or reply.status!='sent'
                or reply.content_hash!=semantic_tools.message_hash(semantic_tools.rendered_message(receipt.result))
                or host.state.event_workbooks.get(row['event'])!=storage_scope(host.sheets)):continue
        scope=candidate;break
    try:
        tax = host._taxonomy()
        catalog = {'complete':True,'items':[r.item for r in tax.rows],
                   'categories':list(dict.fromkeys(r.category for r in tax.rows if r.category)),
                   'subcategories':list(dict.fromkeys(r.subcategory for r in tax.rows if r.subcategory))}
    except Exception:catalog = {'complete':False,'items':[],'categories':[],'subcategories':[]}
    return {'previous_query':{k:deepcopy(v) for k,v in scope.items() if k not in {'workbook','calendar','calendar_read_ids_hash'}} if scope else None,
            'query_capabilities':{'expense_group_by':['item','date','category','subcategory'],
                'filtering':{'primary':'query_relations SQL WHERE over full source reads; arbitrary supported boolean predicates',
                    'legacy_expense_filter':'positive equality ONLY, never exclusion; use null when reading SQL inputs'},
                'expense_order_by':['date','amount','count','label'],'top_rows':20,'sort_before_limit':True,
                'calendar_date_join':'complete current-request Calendar query refs; unions Bangkok dates; all spending on those dates, not causal event costs',
                'relational_analysis':'query_relations executes read-only SQL over complete observed snapshots; no provider SQL or writes',
                'unavailable':['unconfigured_income','unconfigured_balance','provider_SQL','causal_expense_attribution']},
            'query_catalog':catalog}

def calendar_read_ids_hash(host):
    adapter=host.calendar_flow.adapter
    identifiers=set(getattr(adapter,'calendar_read_ids',()))|set(getattr(adapter,'connected_ids',lambda:())())
    return digest(sorted(identifiers)) if identifiers else None


def label_text(value):
    value = re.sub(r'[\r\n\t\x00-\x1f]', ' ', value)
    return value[:80] + ('…' if len(value)>80 else '')


def error(output, code, outcome='unavailable'):
    logging.getLogger('line_assistant.query').info('query_failure domain=%s reason=%s', output['domain'], code)
    return {**output,'ok':False,'complete':False,'outcome':outcome,'error_code':code}


def filtered_entries(tax, entries, chosen):
    """Use catalog identity, never guessed historical categories or fuzzy names.

    Unresolved category membership can affect the requested total. Return a
    separately named partial subtotal and diagnostics, never a complete total.
    """
    by_name={normalized(r.item):r for r in tax.rows}
    matched=[];unresolved={};identity_counts={}
    for entry in entries:
        identity=tax.catalog.identity(entry['item'])
        identity_counts[identity.status]=identity_counts.get(identity.status,0)+1
        row=by_name.get(normalized(identity.canonical)) if identity.canonical else None
        reason=None
        if row is None:
            # An exact item filter has a literal identity boundary. A spacing
            # collision containing that item remains unresolved, not excluded.
            if chosen.kind=='item' and chosen.value not in identity.suggestions:continue
            reason='ambiguous_item_identity' if identity.status=='ambiguous_spacing' else 'missing_catalog_item'
        else:
            value=row.item if chosen.kind=='item' else getattr(row,chosen.kind)
            if chosen.kind=='category' and not value.strip():reason='unassigned_category'
            elif value==chosen.value:matched.append(entry)
        if reason:
            key=(entry['item'],reason)
            if key not in unresolved:
                unresolved[key]={'item':entry['item'],'reason':reason,'count':0,'expense':Decimal(0),
                    'candidate_items':list(identity.suggestions[:3]),'candidate_count':len(identity.suggestions),
                    'may_affect_filter':True}
            unresolved[key]['count']+=1;unresolved[key]['expense']+=Decimal(entry['amount'])
    issues=list(unresolved.values())
    for item in issues:item['expense']=f"{item['expense']:.2f}"
    return matched,issues,identity_counts


def mapping_failure(output, matched, issues, identities, listing):
    subtotal=sum((Decimal(e['amount']) for e in matched),Decimal(0))
    unresolved_total=sum((Decimal(e['expense']) for e in issues),Decimal(0))
    if not subtotal.is_finite() or not unresolved_total.is_finite():raise ValueError('invalid_subtotal')
    matched.sort(key=lambda e:(e['date'],e['item']))
    return {**error(output,'mapping_incomplete'),'input_read_complete':True,
        'mapping_diagnostics':{'unresolved_item_count':len(issues),'unresolved_record_count':sum(e['count'] for e in issues),
            'unresolved_expense':f'{unresolved_total:.2f}','items':issues[:20],'shown':min(20,len(issues)),
            'may_affect_filter':True,'identity_matches':identities,'resolution':'review_expense_name_and_Database_mapping_no_automatic_changes'},
        'partial':{'complete':False,'basis':'uniquely_identified_and_mapped_rows_only','expense':f'{subtotal:.2f}',
            'count':len(matched),'entries':[],'shown':0,'requested_view':'list' if listing else 'total',
            'excludes_unresolved_records':True}}


def execute(host, name, args):
    domain = 'expense' if name=='query_expenses' else 'calendar'
    output = {'status':'stored_query','domain':domain,'read_only':True,'ok':True,'complete':False,
              'financial_write_authorized':False,'calendar_write_authorized':False,
              'scope':{'domain':domain,'workbook':storage_scope(host.sheets),'calendar':host.calendar_flow.calendar_id}}
    try:
        p = (Expenses if domain=='expense' else Calendar).model_validate(args)
        sources=host._owner_sources(p.source_refs)
    except (ValueError,TypeError,KeyError):return error(output,'invalid_arguments','clarification')
    if domain=='expense' and p.measure!='expense':return error(output,'income_unconfigured','income_unconfigured')
    if p.date_mode=='previous':
        if domain=='expense' and not p.calendar_query_refs:
            from .query_followups import current
            if current(host) is not None:return error(output,'activity_selection_required','clarification')
        if domain=='expense' and p.calendar_query_refs:
            reads=getattr(host,'_query_reads',{})
            try:
                scopes=[reads[ref]['scope'] for ref in p.calendar_query_refs]
                if any(s['domain']!='calendar' for s in scopes):raise ValueError()
                previous={'start':min(s['start'] for s in scopes),'end':max(s['end'] for s in scopes)}
            except (KeyError,ValueError,TypeError):return error(output,'invalid_calendar_refs','clarification')
        else:previous=context(host)['previous_query']
        if not previous or not previous.get('start') or not previous.get('end') or p.relative_offset is not None:return error(output,'date_required','clarification')
        if domain=='expense' and previous.get('calendar_basis') and not p.calendar_query_refs:return error(output,'calendar_basis_required','clarification')
        p=p.model_copy(update={'start_date':previous['start'],'end_date':previous['end'],'date_mode':'range'})
    if domain=='calendar' and p.filter_text is not None:
        if not p.filter_text.strip() or not any(p.filter_text in row['text'] for row in sources):return error(output,'invalid_arguments','clarification')
    if args.get('date_mode')!='previous' and (not p.period_text or not any(p.period_text in row['text'] for row in sources)):return error(output,'date_required','clarification')
    if p.date_mode=='upcoming':
        if domain!='calendar':return error(output,'invalid_arguments','clarification')
        today=datetime.fromisoformat(host._current['at']).astimezone(BANGKOK).date()
        p=p.model_copy(update={'date_mode':'range','relative_offset':None,'start_date':today.isoformat(),'end_date':(today+timedelta(days=6)).isoformat(),'upcoming':True})
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
        if not 1900<=a.year<=b.year<=2100 or domain=='calendar' and (b-a).days>=MAX_QUERY_DAYS:raise ValueError()
    except ValueError:return error(output,'date_bounds','clarification')
    output['scope'].update(start=a.isoformat(),end=b.isoformat())
    if p.continue_analysis:output['scope']['continue_analysis']=True
    if domain=='calendar':
        output['scope']['upcoming']=p.upcoming
        output['scope']['filter_text']=p.filter_text
        output['scope']['calendar_read_all']=getattr(host.calendar_flow.adapter,'read_all_enabled',False)
        if calendar_read_ids_hash(host):output['scope']['calendar_read_ids_hash']=calendar_read_ids_hash(host)
        if not host.calendar_flow.enabled:return error(output,'calendar_disabled')
        if p.filter_text and not getattr(host,'response_realization_enabled',False):return error(output,'calendar_inference_disabled','clarification')
        start=datetime.combine(a,time.min,BANGKOK);end=datetime.combine(b+timedelta(days=1),time.min,BANGKOK)
        if p.upcoming:start=max(start,datetime.fromisoformat(host._current['at']).astimezone(BANGKOK))
        try:events=(host.calendar_flow.adapter.saved_events(start,end,include_details=bool(p.filter_text),include_refs=True) if p.continue_analysis
                    else host.calendar_flow.adapter.saved_events(start,end,include_details=True,include_refs=True) if p.filter_text
                    else host.calendar_flow.adapter.saved_events(start,end)) if start<end else []
        except (ApiError,OutcomeUnknown):return error(output,'provider_failure')
        except NotReady:return error(output,'read_incomplete')
        except Exception:return error(output,'schema_failure')
        if p.filter_text and events:
            from .calendar_inference import MAX_CANDIDATES
            if len(events)>MAX_CANDIDATES:return error(output,'calendar_inference_capacity','clarification')
            return {**output,'outcome':'clarification','error_code':'calendar_inference_pending','source_complete':True,'calendar_candidates':events}
        return {**output,'complete':True,'outcome':'answer','events':events[:20],'count':len(events),'shown':min(20,len(events)),
                **({'analysis_records':events} if p.continue_analysis else {})}
    chosen=p.filter
    output['scope'].update(filter={chosen.kind:chosen.value} if chosen else None,listing=p.view=='list')
    if (p.order_by=='count' and p.group_by=='none' or p.order_by=='date' and p.group_by not in {'none','date'}):
        return error(output,'invalid_arguments','clarification')
    if p.view!='list' and (p.group_by!='none' or p.order_by!='date' or p.descending):return error(output,'invalid_arguments','clarification')
    if p.group_by!='none' or p.order_by!='date' or p.descending or p.limit!=20:
        output['scope'].update(group_by=p.group_by,order_by=p.order_by,descending=p.descending,limit=p.limit)
    if p.calendar_query_refs:
        from .query_analysis import calendar_dates
        try:basis=calendar_dates(host,p.calendar_query_refs,a,b)
        except ValueError as problem:return error(output,str(problem),'clarification')
        output['scope'].update(calendar_basis=basis,calendar_read_all=getattr(host.calendar_flow.adapter,'read_all_enabled',False),calendar_read_ids_hash=calendar_read_ids_hash(host))
    try:
        tax=host._taxonomy() if chosen or p.group_by in {'category','subcategory'} else None
        if chosen:
            choices = {r.item if chosen.kind=='item' else getattr(r,chosen.kind) for r in tax.rows}
            matches={v for v in choices if normalized(v)==normalized(chosen.value)}
            if len(matches)!=1:return error(output,'filter_unknown','clarification')
            chosen=chosen.model_copy(update={'value':matches.pop()});output['scope']['filter']={chosen.kind:chosen.value}
        entries=host.sheets.saved_entries(a,b)
        if p.calendar_query_refs:entries=[e for e in entries if e['date'] in basis['dates']]
        if chosen:
            entries,issues,identities=filtered_entries(tax,entries,chosen)
            if issues:return mapping_failure(output,entries,issues,identities,p.view=='list')
        entries.sort(key=lambda e:(e['date'],e['item']))
        total=sum((Decimal(e['amount']) for e in entries),Decimal('0'))
        if not total.is_finite() or total<0:raise ValueError()
        groups=[]
        if p.group_by!='none':
            by_name={normalized(r.item):r for r in tax.rows} if tax else {}
            grouped={}
            for e in entries:
                if p.group_by in {'item','date'}:label=e[p.group_by]
                else:
                    identity=tax.catalog.identity(e['item'])
                    mapped=by_name.get(normalized(identity.canonical)) if identity.canonical else None
                    if not mapped or not getattr(mapped,p.group_by).strip():return error(output,'mapping_incomplete','clarification')
                    label=getattr(mapped,p.group_by)
                group=grouped.setdefault(label,{'label':label,'amount':Decimal(0),'count':0})
                group['amount']+=Decimal(e['amount']);group['count']+=1
            groups=sorted(grouped.values(),key=lambda g:g['label'])
            if p.order_by in {'amount','count'}:groups.sort(key=lambda g:g[p.order_by],reverse=p.descending)
            elif p.descending:groups.reverse()
            groups=[{**g,'amount':f"{g['amount']:.2f}"} for g in groups]
        elif p.order_by=='amount':entries.sort(key=lambda e:Decimal(e['amount']),reverse=p.descending)
        elif p.order_by=='label':entries.sort(key=lambda e:(e['item'],e['date']),reverse=p.descending)
        elif p.descending:entries.reverse()
    except (ApiError,OutcomeUnknown):return error(output,'provider_failure')
    except NotReady:return error(output,'schema_failure')
    except Exception:return error(output,'schema_failure')
    return {**output,'complete':True,'outcome':'summary','expense':f'{total:.2f}','count':len(entries),
            'entries':entries[:p.limit] if p.view=='list' and p.group_by=='none' else [],
            'shown':min(p.limit,len(groups) if p.group_by!='none' else len(entries)) if p.view=='list' else 0,'income_supported':False,
            **({'groups':groups[:p.limit] if p.view=='list' else [],'group_count':len(groups)} if p.group_by!='none' else {}),
            **({'analysis_records':entries if p.view=='list' else []} if p.continue_analysis else {})}


def day_label(value):
    day=date.fromisoformat(value)
    return f'{day.day} {MONTHS[day.month-1]} {day.year}'


def calendar_records(events,scope):
    """Display cells for each appointment's first visible Bangkok day.

    Spanning appointments retain their original date range instead of being
    counted again on every day. A carry-in belongs under the query's first day.
    """
    first=date.fromisoformat(scope['start']);rows=[]
    for event in events:
        if event['all_day']:
            start=date.fromisoformat(event['start']);last=date.fromisoformat(event['end'])-timedelta(days=1)
            when='ทั้งวัน'+(' ('+day_label(start.isoformat())+' ถึง '+day_label(last.isoformat())+')' if last!=start else '')
            day=start;order=event['start']
        else:
            start,end=(datetime.fromisoformat(event[key]).astimezone(BANGKOK) for key in ['start','end'])
            day=start.date();order=start.isoformat()
            if day<first:
                when='เริ่ม '+day_label(day.isoformat())+' '+start.strftime('%H:%M')+' ถึง '+day_label(end.date().isoformat())+' '+end.strftime('%H:%M')
            elif day!=end.date():when=start.strftime('%H:%M')+' ถึง '+day_label(end.date().isoformat())+' '+end.strftime('%H:%M')
            else:when=start.strftime('%H:%M')+'–'+end.strftime('%H:%M')
        rows.append((max(day,first),not event['all_day'],order,event['title'],when))
    return [{'date':day.isoformat(),'title':label_text(title),'when':when}
            for day,_,_,title,when in sorted(rows)]


def calendar_rows(events,scope):
    result=[];previous=None;ordinal=0
    for event in calendar_records(events,scope):
        day=date.fromisoformat(event['date'])
        heading=''
        if day!=previous:
            heading='\nวัน'+WEEKDAYS[day.weekday()]+'ที่ '+day_label(day.isoformat())+':\n'
            ordinal=0;previous=day
        ordinal+=1
        result.append(heading+f"{ordinal}. {event['when']} — "+event['title'])
    return result


def period_label(scope, at):
    a,b=map(date.fromisoformat,(scope['start'],scope['end']));today=at.astimezone(BANGKOK).date()
    if a==b:
        prefix={-1:'เมื่อวาน',0:'วันนี้',1:'พรุ่งนี้'}.get((a-today).days)
        return (prefix+' ('+day_label(a.isoformat())+')') if prefix else day_label(a.isoformat())
    return day_label(a.isoformat())+' ถึง '+day_label(b.isoformat())


ERRORS = {
 'activity_selection_required':'ยังมีนัดที่รอยืนยันสำหรับคำถามรายจ่ายเดิมครับ โปรดเลือกนัดก่อนรวมรายจ่ายของวันนั้น',
 'invalid_arguments':'ข้อมูลที่ใช้ค้นหายังไม่ชัดเจนครับ ช่วยระบุช่วงวันหรือสิ่งที่ต้องการดูอีกครั้ง',
 'date_required':'ต้องการดูช่วงวันไหนครับ?',
 'date_bounds':'ช่วงวันที่ไม่ถูกต้องหรือยาวเกินขอบเขตครับ รายจ่ายดูได้ไม่เกิน 367 วัน และปฏิทินไม่เกิน 31 วัน',
 'income_unconfigured':'ยังไม่มีข้อมูลรายรับที่รองรับครับ จึงสรุปรายรับหรือยอดคงเหลือไม่ได้',
 'filter_unknown':'ยังจับคู่ชื่อหรือหมวดที่ต้องการกับฐานข้อมูลไม่ได้ครับ ช่วยระบุให้ชัดอีกนิด',
 'invalid_calendar_refs':'ยังไม่มีผลค้นหาปฏิทินที่ใช้อ้างอิงได้ในคำถามรอบนี้ครับ ต้องค้นหานัดก่อนจับคู่รายจ่ายตามวัน',
 'incomplete_calendar_basis':'ผลค้นหาปฏิทินยังไม่ครบหรือมีนัดที่ยังจัดประเภทไม่ชัดเจนครับ จึงยังรวมรายจ่ายของทุกวันที่ถามไม่ได้',
 'calendar_condition_mismatch':'ผลค้นหาปฏิทินใช้เงื่อนไขกิจกรรมต่างกันครับ ต้องอ่านทุกช่วงด้วยเงื่อนไขเดียวกันก่อนรวมรายจ่ายตามวัน',
 'calendar_scope_incomplete':'ผลค้นหาปฏิทินยังไม่ครอบคลุมช่วงวันที่รายจ่ายที่ถามครบครับ ต้องอ่านปฏิทินให้ครบก่อน',
 'missing_calendar_event_refs':'ยังไม่มีหลักฐานนัดครบทุกนัดสำหรับจับคู่วันกับรายจ่ายครับ ต้องอ่านปฏิทินใหม่ก่อน',
 'calendar_basis_required':'คำถามก่อนเป็นรายจ่ายเฉพาะวันที่มีนัดครับ ต้องอ่านปฏิทินตามเงื่อนไขเดิมใหม่ก่อน จึงจะไม่เผลอรวมรายจ่ายทุกวัน',
 'mapping_incomplete':'มีรายการในช่วงนี้ที่จับคู่หมวดไม่ได้ครับ จึงยังรวมยอดเฉพาะหมวดนี้ให้ครบไม่ได้',
 'provider_failure':'ตอนนี้เชื่อมต่อแหล่งข้อมูลไม่สำเร็จครับ เลยยังสรุปผลให้ไม่ได้',
 'schema_failure':'รูปแบบข้อมูลที่อ่านมายังตรวจสอบไม่ผ่านครับ จึงยังสรุปยอดหรือยืนยันว่าไม่มีรายการไม่ได้',
 'read_incomplete':'อ่านข้อมูลได้ไม่ครบครับ จึงยังยืนยันรายการทั้งหมดหรือการไม่มีนัดไม่ได้',
 'calendar_filter_unsupported':'ตอนนี้อ่านนัดได้ตามช่วงวันครับ ยังกรองชื่อหรือประเภทนัดไม่ได้ ต้องการดูทุกนัดในช่วงนั้นไหมครับ?',
 'calendar_inference_pending':'อ่านนัดในช่วงที่ถามแล้ว แต่ยังแยกนัดที่เข้าข่ายให้ไม่ได้ครับ จึงยังสรุปจำนวนเฉพาะที่ถามไม่ได้',
 'calendar_inference_disabled':'ยังไม่ได้เปิดการวิเคราะห์นัดที่เข้าข่ายครับ จึงยังสรุปจำนวนเฉพาะที่ถามไม่ได้',
 'calendar_inference_capacity':'นัดในช่วงนี้มีมากเกินกว่าจะวิเคราะห์ได้ครบในครั้งเดียวครับ ช่วยระบุช่วงวันที่สั้นลงหน่อย',
 'calendar_disabled':'ยังไม่ได้เปิดการอ่านปฏิทินครับ จึงตรวจนัดที่บันทึกไว้ไม่ได้'}


def facts(output, at):
    if not output['complete'] and not output.get('calendar_selection'):
        if output['error_code']!='mapping_incomplete' or not output.get('mapping_diagnostics'):return {'reason':ERRORS[output['error_code']]}
        scope=output['scope'];diagnostics=output['mapping_diagnostics'];partial=output['partial']
        label=label_text(next(iter(scope['filter'].values())))
        kind=next(iter(scope['filter']))
        impact='อาจตรงกับชื่อรายการที่ถาม' if kind=='item' else 'อาจอยู่ใน'+('หมวดย่อย' if kind=='subcategory' else 'หมวด')+'ที่ถาม'
        values={'reason':f"{period_label(scope,at)} เฉพาะ {label} ยังยืนยันยอดครบไม่ได้ครับ",
            'mapping_partial':f"ยอดบางส่วนเฉพาะรายการที่จับคู่ Database ได้ {partial['expense']} บาท จาก {partial['count']} รายการ ยังไม่รวม {diagnostics['unresolved_record_count']} รายการ ({diagnostics['unresolved_item_count']} ชื่อ) รวม {diagnostics['unresolved_expense']} บาท ซึ่ง{impact}ครับ",
            'mapping_resolution':'โปรดตรวจชื่อรายการด้านล่างใน All Expenses และคู่ชื่อ–หมวดใน Database ให้ตรงกันก่อน แล้วถามใหม่ครับ ยังไม่ได้แก้ข้อมูลใดให้'}
        reasons={'missing_catalog_item':'ไม่พบชื่อใน Database','ambiguous_item_identity':'ตรงกับชื่อที่ต่างกันเฉพาะช่องว่างมากกว่าหนึ่งชื่อ',
            'unassigned_category':'ยังไม่มีหมวดใน Database','unassigned_subcategory':'ยังไม่มีหมวดย่อยใน Database'}
        shown=diagnostics['items'][:10]
        for i,item in enumerate(shown,1):
            values[f'row{i}']=f"{label_text(item['item'])} — {item['count']} รายการ รวม {item['expense']} บาท ({reasons[item['reason']]})"
            if item['candidate_items']:values[f'row{i}']+=' ตัวเลือกที่ต้องตรวจ: '+', '.join(label_text(x) for x in item['candidate_items'][:2])
        if diagnostics['unresolved_item_count']>len(shown):values['truncation']='แสดงชื่อที่ยังจับคู่ไม่ได้ 10 ชื่อแรก จำนวนและยอดรายการที่ยังไม่รวมใช้ทั้งหมดที่อ่านครบแล้ว'
        return values
    scope=output['scope'];values={'period':period_label(scope,at)}
    if scope['domain']=='expense':
        # A bare label lets prose turn inclusion into exclusion. Supply the
        # actual complete condition as an indivisible display fact instead.
        if scope.get('filter'):values['selection']='เฉพาะ '+label_text(next(iter(scope['filter'].values())))
        if scope.get('calendar_basis'):
            basis=scope['calendar_basis'];activity=' / '.join(label_text(c) for c in basis['criteria'])
            values['selection']='เฉพาะวันที่มีนัดที่เข้าข่าย '+activity+(' และ '+values['selection'] if 'selection' in values else '')
        values.update(total=output['expense']+' บาท',count=str(output['count'])+' รายการ')
        if scope.get('calendar_basis'):
            values['row_basis']=f"จับคู่ได้ {len(basis['dates'])} วัน จาก {basis['event_count']} นัด: "+(', '.join(day_label(d) for d in basis['dates']) or 'ไม่พบวันที่เข้าข่าย')
            values['row_method']='ยอดนี้รวมรายจ่ายทั้งหมดในวันที่ตรงกัน ไม่ได้ยืนยันว่าแต่ละรายการเป็นค่าใช้จ่ายของนัดนั้นโดยตรง'
        grouping=scope.get('group_by','none');order=scope.get('order_by','date')
        if grouping!='none' or order!='date' or scope.get('descending'):
            groups={'none':'รายการ','item':'ชื่อรายการ','date':'วัน','category':'หมวด','subcategory':'หมวดย่อย'}
            sort={'amount':'ยอดเงิน','count':'จำนวนรายการ','label':'ชื่อ','date':'วันที่'}[order]
            values['row_order']=('รวมยอดแยกตาม'+groups[grouping]+' ' if grouping!='none' else '')+'เรียงตาม'+sort+('จากมากไปน้อย' if scope.get('descending') else 'จากน้อยไปมาก')
        for i,g in enumerate(output.get('groups',[]),1):
            label=day_label(g['label']) if grouping=='date' else label_text(g['label'])
            values[f'row{i}']=f"{i}. {label} — {g['amount']} บาท ({g['count']} รายการ)"
        previous=None
        for i,e in enumerate(output['entries'],1):
            row=f"{label_text(e['item'])} {Decimal(e['amount']):.2f} บาท"
            if scope['start']!=scope['end']:
                if order=='date':
                    if e['date']!=previous:row='\n'+day_label(e['date'])+':\n'+row
                else:row=day_label(e['date'])+' — '+row
            values[f'row{i}']=row;previous=e['date']
    else:
        if scope.get('filter_text'):values['filter']=label_text(scope['filter_text'])
        values['count']=str(output['count'])+(' ครั้ง' if scope.get('filter_text') else ' นัด')
        for i,row in enumerate(calendar_rows(output['events'],scope),1):values[f'row{i}']=row
        if scope.get('upcoming'):values['upcoming']='เฉพาะนัดที่ยังไม่จบตั้งแต่เวลาปัจจุบัน'
        selection=output.get('calendar_selection',{})
        if selection.get('uncertain_count'):
            values['uncertainty']=f"อีก {selection['uncertain_count']} นัดยังไม่แน่ใจว่าเข้าข่าย {values['filter']} จึงยังยืนยันจำนวนทั้งหมดไม่ได้ครับ"
            for i,row in enumerate(calendar_rows(output['uncertain_events'],scope),1):
                values[f'row_uncertain{i}']=('นัดที่ยังไม่แน่ใจว่าเข้าข่าย:\n' if i==1 else '')+row
            if selection['uncertain_count']>len(output['uncertain_events']):values['uncertainty_limit']='แสดงนัดที่ยังไม่แน่ใจ 5 รายการแรก'
    if output.get('group_count',output['count'])>output['shown'] and output['shown']:
        if scope['domain']=='expense':
            unit='กลุ่ม' if scope.get('group_by','none')!='none' else 'รายการ'
            values['truncation']=f"แสดง {output['shown']} {unit}แรก ยอดและจำนวนรวมใช้ข้อมูลทั้งหมดที่อ่านครบแล้ว"
        else:values['truncation']='แสดง 20 รายการแรกจากนัดทั้งหมดที่อ่านครบแล้ว'
    return values


def result(output, at):
    values=facts(output,at)
    if not output['complete'] and not output.get('calendar_selection'):message='\n'.join(v for k,v in values.items() if not k.startswith('row') and k!='truncation')
    elif output['domain']=='expense':
        message=values['period']+(' '+values['selection'] if 'selection' in values else '')+f" ใช้ไป {values['total']} จาก {values['count']}ครับ"
    else:message=values['period']+(' มีนัดที่เข้าข่าย '+values['filter'] if 'filter' in values else ' มี')+f" {values['count']}ครับ"
    message+=''.join('\n'+v for k,v in values.items() if k.startswith('row') or k in {'truncation','upcoming','uncertainty','uncertainty_limit'})
    data={'domain':output['domain'],'read_only':True,'financial_write_authorized':False,'calendar_write_authorized':False,
          'read_query':deepcopy(output['scope']),'query_facts':values,'tool_result':output}
    if output.get('calendar_candidates'):data['calendar_selection_at']=at.isoformat()
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
