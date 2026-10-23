"""Owner-bound, read-only continuation and arithmetic across query results.

Working results live only in the current request. Legacy operations and the
relational executor share observed references, never provider SQL or writes.
"""
from copy import deepcopy
from datetime import date,datetime,timedelta
from decimal import Decimal
from typing import Literal
import re
from pydantic import BaseModel,ConfigDict,Field
from .history_context import digest
from .model_api import object_schema
from .state import Claim,Result

READ_TOOLS={'query_sources','lookup_catalog','query_expenses','query_calendar','inspect_data_sources','finish_query_analysis','resolve_activity_dates','resolve_temporal','query_relations','list_tables','describe_table','query_table','check_table_change'}

def unverified_claim(text):
    # Output-claim check only, never an owner intent/date/keyword router.
    if re.search(r'ไม่ได้|ไม่รองรับ|ยังไม่สามารถ',text):return False
    verbs=r'(?:เรียง|จัดอันดับ|จัดกลุ่ม|รวมยอด|คำนวณ|จับคู่)'
    return bool(re.search(r'ผม(?:จะ|สามารถ|ช่วย)?'+verbs+r'.{0,80}(?:ให้|ได้|แล้ว)',text)
                or re.search(verbs+r'.{0,60}(?:เรียบร้อยแล้ว|เสร็จแล้ว)',text))


class Finish(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    query_refs:list[str]=Field(min_length=1)
    operation:Literal['report','expense_sum','calendar_count','compare']
    source_refs:list[str]=Field(min_length=1,max_length=16)

def definitions():
    return [{'type':'function','name':'finish_query_analysis','strict':True,
        'description':'Finish read-only analysis using current-request query_refs that answer the requested output. For spending on Calendar-selected dates select the resulting expense query_ref, not only the supporting Calendar ref. report shows grounded results; expense_sum requires disjoint complete date ranges; calendar_count deduplicates event refs; compare computes same-domain differences. No model totals or writes.',
        'parameters':object_schema({'query_refs':{'type':'array','items':{'type':'string'},'minItems':1},
            'operation':{'type':'string','enum':['report','expense_sum','calendar_count','compare']},
            'source_refs':{'type':'array','items':{'type':'string'},'minItems':1}})}]

def remember(host,output,at,step):
    from .read_queries import result as render_query
    from . import calendar_inference,response_realization
    if output.get('read_failure'):result=render(output)
    elif output.get('table_query'):
        from .table_workflow import render as render_table
        result=render_table(output)
    else:result=render_query(output,at)
    if calendar_inference.pending(result):
        key='analysis01_'+digest([host._active_event_id,step])[:40]
        parent=host.state.claims[host._active_event_id]
        host.state.claims.setdefault(key,Claim(parent.fingerprint,parent.expires_at))
        result=response_realization.realize(host,key,result)
        output=result.data['tool_result']
    ref='read_'+digest([host._active_event_id,len(host._query_reads),output['scope']])[:20]
    host._query_reads[ref]=deepcopy(output)
    value={k:deepcopy(v) for k,v in output.items() if k not in {'scope','analysis_records','calendar_candidates'}}
    value.update(query_ref=ref,scope={k:deepcopy(v) for k,v in output['scope'].items() if k not in {'calendar','workbook','calendar_read_ids_hash'}})
    records=output.get('analysis_records')
    if records is not None:
        if output['domain']=='table':
            value['matched_record_count']=len(records)
            value['join_source_fields']=output['scope']['query'].get('select') or [c.name for c in host.table_flow.registry.get(output['table_id'])[0].columns]
        else:value['events' if output['domain']=='calendar' else 'entries']=deepcopy(records)
    return value

def fail(code,message):
    return {'query_analysis':True,'ok':False,'complete':False,'status':'clarification',
            'read_only':True,'financial_write_authorized':False,'calendar_write_authorized':False,
            'error_code':code,'message':message}

def calendar_dates(host,refs,start,end):
    """Union Bangkok dates of complete source events, never model-supplied dates."""
    from .language import BANGKOK
    try:
        if not refs or len(set(refs))!=len(refs):raise KeyError()
        reads=[host._query_reads[ref] for ref in refs]
        if any(row['domain']!='calendar' for row in reads):raise KeyError()
    except (KeyError,TypeError):raise ValueError('invalid_calendar_refs') from None
    if not all(row['complete'] for row in reads):raise ValueError('incomplete_calendar_basis')
    if len({row['scope'].get('filter_text') for row in reads})!=1:raise ValueError('calendar_condition_mismatch')
    intervals=sorted((date.fromisoformat(row['scope']['start']),date.fromisoformat(row['scope']['end'])) for row in reads)
    covered=start
    for first,last in intervals:
        if first>covered:break
        covered=max(covered,last+timedelta(days=1))
    if covered<=end:raise ValueError('calendar_scope_incomplete')
    dates=set();occurrences=set()
    for row in reads:
        events=row.get('analysis_records',[])
        if len(events)!=row['count'] or any(not e.get('read_ref') for e in events):raise ValueError('missing_calendar_event_refs')
        for e in events:
            if e['all_day']:
                first=date.fromisoformat(e['start']);last=date.fromisoformat(e['end'])-timedelta(days=1)
            else:
                first=datetime.fromisoformat(e['start']).astimezone(BANGKOK).date()
                last=(datetime.fromisoformat(e['end']).astimezone(BANGKOK)-timedelta(microseconds=1)).date()
            # An event appearing in several windows only proves dates inside
            # windows that were actually read with the selected criteria.
            first=max(first,start,date.fromisoformat(row['scope']['start']))
            last=min(last,end,date.fromisoformat(row['scope']['end']))
            if first<=last:occurrences.add(e['read_ref'])
            while first<=last:
                dates.add(first.isoformat());first+=timedelta(days=1)
    return {'dates':sorted(dates),'event_count':len(occurrences),
            'criteria':list(dict.fromkeys(row['scope'].get('filter_text') or 'นัดในปฏิทิน' for row in reads)),
            'meaning':'all_expenses_on_matching_Bangkok_dates_not_proven_event_costs'}

def finish(host,args):
    from .read_queries import result as render_query
    try:
        p=Finish.model_validate(args);host._owner_sources(p.source_refs)
        if host._current['source_ref'] not in p.source_refs or len(set(p.query_refs))!=len(p.query_refs):raise ValueError()
        selected=[host._query_reads[ref] for ref in p.query_refs]
    except (ValueError,TypeError,KeyError):return fail('invalid_query_refs','ยังอ้างอิงผลค้นหาที่อ่านจริงไม่ครบครับ กรุณาระบุสิ่งที่ต้องการค้นอีกครั้ง')
    at=datetime.fromisoformat(host._current['at'])
    from .table_workflow import render as render_table
    reports=[render(row) if row.get('relational_query') or row.get('read_failure') else render_table(row) if row.get('table_query') else render_query(row,at) for row in selected]
    scopes=[{k:deepcopy(v) for k,v in row['scope'].items() if k not in {'workbook','calendar','calendar_read_ids_hash','continue_analysis'}} for row in selected]
    common={'query_analysis':True,'ok':True,'read_only':True,'financial_write_authorized':False,'calendar_write_authorized':False,
            'query_refs':p.query_refs,'scopes':scopes,'operation':p.operation,'complete':all(row['complete'] for row in selected)}
    expenses=[row for row in selected if row['domain']=='expense']
    reusable=[row for row in expenses if row['scope'].get('start') and row['scope'].get('end')]
    failed_member=any(row.get('read_failure') for row in selected)
    if not failed_member and len(reusable)==1:common['followup_scope']=deepcopy(reusable[0]['scope'])
    elif not failed_member and len(selected)==1:
        scope=selected[0].get('followup_scope') or selected[0]['scope']
        if scope.get('start') and scope.get('end'):common['followup_scope']=deepcopy(scope)
    if p.operation=='report':
        # Counts, units and incomplete-read qualifiers remain original facts.
        message='\n\n'.join(report.message for report in reports)
        return {**common,'status':'answer' if common['complete'] else 'clarification','message':message}
    if not common['complete']:return fail('incomplete_basis','ผลค้นหาบางส่วนยังไม่ครบหรือมีนัดที่จัดประเภทไม่ชัดเจนครับ จึงยังรวมเป็นยอดทั้งหมดไม่ได้')
    if any(row['domain']=='relation' for row in selected):
        return fail('relational_operation_required','ผลลัพธ์นี้วิเคราะห์ต่อได้ผ่าน query_relations ครับ')
    headers=[report.message.split('\n',1)[0] for report in reports]
    if p.operation=='expense_sum':
        if not expenses:return fail('domain','การรวมรายจ่ายต้องเลือกผลค้นหารายจ่ายครับ')
        intervals=sorted((row['scope']['start'],row['scope']['end']) for row in expenses)
        if any(a[1]>=b[0] for a,b in zip(intervals,intervals[1:])):
            return fail('overlap','ช่วงวันที่รายจ่ายที่เลือกซ้อนกันครับ จึงยังบวกรวมเพราะอาจนับรายการซ้ำ')
        total=sum((Decimal(row['expense']) for row in expenses),Decimal(0))
        common.update(expense=f'{total:.2f}',count=sum(row['count'] for row in expenses))
        message=f"รายจ่ายรวมจากช่วงวันที่เลือก {total:.2f} บาท จาก {common['count']} รายการครับ"
    elif p.operation=='calendar_count':
        if any(row['domain']!='calendar' for row in selected):return fail('domain','การนับนัดต้องเลือกผลค้นหาปฏิทินครับ')
        occurrences={}
        for row in selected:
            records=row.get('analysis_records',[])
            if len(records)!=row['count'] or any(not e.get('read_ref') for e in records):
                return fail('missing_event_refs','ยังไม่มีหลักฐานอ้างอิงนัดครบทุกนัดครับ จึงยังรวมจำนวนไม่ได้')
            for event in records:occurrences[event['read_ref']]=event
        common['count']=len(occurrences)
        message=f"นัดที่ไม่ซ้ำกันจากผลค้นหาที่เลือกมี {len(occurrences)} ครั้งครับ"
    else:
        if any(row['domain']=='table' for row in selected):
            return fail('comparison_basis','กรุณาใช้การจัดกลุ่มและคำนวณผ่าน query_table เพื่อเปรียบเทียบคอลัมน์ที่ต้องการครับ')
        if len(selected)<2 or len({row['domain'] for row in selected})!=1:
            return fail('comparison_basis','ต้องเลือกผลค้นหาอย่างน้อยสองชุดจากแหล่งข้อมูลชนิดเดียวกันครับ')
        expenses=selected[0]['domain']=='expense';unit='บาท' if expenses else 'ครั้ง'
        values=[Decimal(row['expense']) if expenses else Decimal(row['count']) for row in selected]
        differences=[value-values[0] for value in values[1:]]
        common['differences']=[f'{v:.2f}' if expenses else str(int(v)) for v in differences]
        message='เทียบกับผลค้นหาแรก:\n'+'\n'.join(
            headers[i+1]+f" — ต่างจากชุดแรก {value:+.2f} {unit}" if expenses
            else headers[i+1]+f" — ต่างจากชุดแรก {int(value):+d} {unit}" for i,value in enumerate(differences))
    message+='\n\nผลค้นหาที่ใช้:\n'+'\n'.join(headers)
    return {**common,'status':'answer','message':message}

def render(output):
    message=output['message']
    if len(message.encode('utf-16-le'))>9000:
        return Result('clarification','ผลวิเคราะห์ยาวเกินกว่าจะส่งในข้อความเดียวครับ ช่วยระบุส่วนที่ต้องการดูให้แคบลง',
                      {'domain':'query_analysis','query_analysis':True,'read_only':True,'complete':False})
    return Result(output['status'],message,{'domain':'query_analysis','query_analysis':True,'read_only':True,
        **({'read_query':deepcopy(output['followup_scope'])} if output.get('followup_scope') else {}),
        'financial_write_authorized':False,'calendar_write_authorized':False,'tool_result':deepcopy(output)})
