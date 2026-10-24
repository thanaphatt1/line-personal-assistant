"""Read-only capability/source observations. Unknown coverage stays unknown.

Provider coordinates and business rows never enter the model-facing inventory.
Observations are cached only for the current owner request, with timestamps.
"""
from collections import Counter
from copy import deepcopy
from datetime import date
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from .model_api import object_schema
from .state import Result

NAME='inspect_data_sources'

class Inspect(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    source:Literal['capabilities','expense','calendar','tables','all']
    continue_analysis:bool=False
    source_refs:list[str]=Field(min_length=1,max_length=16)

def definitions():
    return [{'type':'function','name':NAME,'strict':True,
        'description':'Inspect actual available tools and configured-source scope. expense reads complete bounded data to return observed years/counts/first-last record dates, never rows or amounts; calendar discovers selected source count and known query windows, with global date extent explicitly UNKNOWN. No new access, providers, writes, SQL or model-invented metadata.',
        'parameters':object_schema({'source':{'type':'string','enum':['capabilities','expense','calendar','tables','all']},
            'continue_analysis':{'type':'boolean','description':'True to plan further read-only queries after seeing metadata; false to answer this inventory question directly.'},
            'source_refs':{'type':'array','items':{'type':'string'},'minItems':1,'maxItems':16}})}]

def context(host):
    from .read_queries import MAX_EXPENSE_DAYS,MAX_QUERY_DAYS
    observations=deepcopy(getattr(host,'_data_inventory',{}))
    value={'data_sources':{
        'expense':{'scope':'configured expense dataset only','record_date_extent':'current_request_observation' if observations.get('expense',{}).get('status')=='verified' else 'unknown_until_inspected',
            'query_window_days':MAX_EXPENSE_DAYS,'income':False,'balance':False},
        'calendar':{'enabled':host.calendar_flow.enabled,'scope':'server-selected configured/shared calendars only',
            'event_read_permission':'verified_per_actual_read','global_date_extent':'unknown','query_window_days':MAX_QUERY_DAYS},
        'external_search':False,'write_policy':'explicit new owner confirmation of a reviewed proposal; tool calls do not grant consent'},
        'data_source_observations':observations}
    flow=getattr(host,'table_flow',None)
    if flow and flow.enabled:value['data_sources']['tables']={'scope':'configured logical tables only','tables':flow.registry.catalog(),'schemas':'describe_table','unknown_coverage':'query_table complete snapshot required'}
    return value

def windows(host):
    rows=[]
    for value in getattr(host,'_query_reads',{}).values():
        if value.get('domain')=='calendar':
            scope=value['scope'];rows.append({'start':scope['start'],'end':scope['end'],
                'criteria':scope.get('filter_text'),'complete':value['complete']})
    return rows

def execute(host,args):
    result={'data_inventory':True,'ok':True,'read_only':True,'status':'answer',
        'checked_at':host.clock().isoformat(),'financial_write_authorized':False,'calendar_write_authorized':False}
    try:
        p=Inspect.model_validate(args);host._owner_sources(p.source_refs)
        if host._current['source_ref'] not in p.source_refs:raise ValueError()
    except (ValueError,KeyError,TypeError):return {**result,'ok':False,'status':'clarification','error':'invalid_arguments','continue_analysis':False}
    result.update(source=p.source,continue_analysis=p.continue_analysis,
        available_tools=[t['name'] for t in host.state.raw_tool_definitions],data_sources=context(host)['data_sources'])
    cache=host._data_inventory
    for source in (['expense','calendar','tables'] if p.source=='all' else [p.source] if p.source!='capabilities' else []):
        if source not in cache:
            try:
                if source=='expense':
                    records=host.sheets.classification_entries()
                    days=[date.fromisoformat(row['date']).isoformat() for row in records]
                    years=Counter(int(day[:4]) for day in days)
                    value={'status':'verified','checked_at':host.clock().isoformat(),'complete_input_read':True,
                        'coverage_kind':'observed_records_in_configured_dataset_not_import_completeness',
                        'record_count':len(days),'first_record_date':min(days) if days else None,
                        'last_record_date':max(days) if days else None,
                        'years':[{'year':year,'record_count':years[year]} for year in sorted(years)]}
                elif source=='tables':value={'status':'configured','checked_at':host.clock().isoformat(),'tables':host.table_flow.registry.catalog()}
                elif not host.calendar_flow.enabled:value={'status':'disabled','checked_at':host.clock().isoformat(),'global_date_extent':'unknown'}
                else:
                    selected=host.calendar_flow.adapter.readable_calendars()
                    value={'status':'selected_sources_discovered','checked_at':host.clock().isoformat(),
                        'selected_calendar_count':len(selected),'per_calendar_event_access':'not_verified_by_discovery',
                        'global_date_extent':'unknown','reason':'bounded reads and potentially unending recurring events'}
            except Exception:
                value={'status':'unavailable','checked_at':host.clock().isoformat(),'coverage_kind':'unknown','reason':'complete_read_or_schema_or_access_unavailable'}
            cache[source]=value
        result[source]=deepcopy(cache[source])
    if 'calendar' in result:result['calendar']['queried_windows']=windows(host)
    return result

def render(output):
    from .read_queries import day_label,label_text
    from datetime import datetime
    from .language import BANGKOK
    if not output['ok']:return Result('clarification','ยังตรวจขอบเขตข้อมูลจากคำขอนี้ไม่ได้ครับ',{'data_inventory':True,'tool_result':output,'read_only':True})
    tools=set(output['available_tools']);lines=[]
    if output['source']=='capabilities':
        if 'query_expenses' in tools:lines.append('รายจ่าย: อ่านยอดและรายการ กรอง จัดกลุ่ม เรียงยอด และจับคู่กับวันที่จากปฏิทินได้')
        if 'query_calendar' in tools and output['data_sources']['calendar']['enabled']:lines.append('ปฏิทิน: อ่านนัดในช่วงวันที่และตีความเงื่อนไขกิจกรรมได้')
        lines.append('อ่านรายจ่ายได้ครั้งละไม่เกิน 10 ปี และปฏิทินไม่เกิน 3 ปี')
        lines.append('เข้าถึงเฉพาะฐานข้อมูลและปฏิทินที่เชื่อมให้ระบบ สิทธิ์อ่านนัดต้องตรวจเมื่ออ่านจริง ยังไม่ใช่ทุกปฏิทินในอีเมล')
        lines.append('รายรับเพิ่มเติมขึ้นอยู่กับ table ที่เชื่อมไว้; รายจ่ายเดิมไม่มีข้อมูลยอดคงเหลือ' if output['data_sources'].get('tables') else 'ยังไม่มีข้อมูลรายรับหรือยอดคงเหลือ')
        lines.append('การสร้างหรือแก้ข้อมูลต้องผ่านการตรวจและยืนยันจากเจ้าของก่อน' if any(t.startswith('prepare_') for t in tools)
                     else 'รอบวิเคราะห์นี้ใช้ได้เฉพาะการอ่านข้อมูล ไม่มีเครื่องมือสร้างหรือแก้ข้อมูลให้ใช้')
    expense=output.get('expense')
    tables=output.get('tables',output['data_sources'].get('tables',{})).get('tables',[])
    if tables:
        lines.append('ตารางที่เชื่อมไว้:')
        lines.extend(row['label']+' — '+row['description'] for row in tables)
    if expense:
        if expense['status']!='verified':lines.append('รายจ่าย: ยังตรวจช่วงข้อมูลและปีที่มีรายการไม่ได้ครับ จึงยังไม่ยืนยันว่ามีหรือไม่มีข้อมูลในปีใด')
        elif not expense['record_count']:lines.append('รายจ่าย: ไม่พบรายการในฐานข้อมูลที่เชื่อมต่อจากการอ่านที่ตรวจครบแล้วครับ')
        else:
            years=expense['years'];lines.append(f"รายจ่ายที่บันทึกไว้: {expense['record_count']} รายการ")
            lines.append('ปีที่พบ: '+', '.join(f"{r['year']} ({r['record_count']} รายการ)" for r in years[:20]))
            if len(years)>20:lines.append(f'แสดง 20 ปีแรกจาก {len(years)} ปีที่พบ')
            lines.append('วันที่แรกที่พบ '+day_label(expense['first_record_date'])+' ถึงวันที่ล่าสุด '+day_label(expense['last_record_date']))
            lines.append('ช่วงนี้บอกขอบเขตรายการที่มีในฐานข้อมูล ยังไม่ยืนยันว่าบันทึกรายจ่ายจริงครบทุกวันครับ')
    calendar=output.get('calendar')
    if calendar:
        if calendar['status']=='disabled':lines.append('ปฏิทินยังไม่ได้เปิดใช้งานครับ')
        elif calendar['status']=='unavailable':lines.append('ปฏิทิน: ยังตรวจรายชื่อแหล่งข้อมูลที่เลือกไว้ไม่ได้ครับ')
        else:lines.append(f"ปฏิทิน: เลือกไว้ {calendar['selected_calendar_count']} แหล่ง สิทธิ์อ่านนัดจริงต้องตรวจในแต่ละช่วงที่อ่าน")
        lines.append('ช่วงวันแรก–วันสุดท้ายของข้อมูลทั้งปฏิทินยังไม่ทราบครับ นัดที่เกิดซ้ำอาจไม่มีวันสิ้นสุด')
        queried=calendar.get('queried_windows',[])
        for row in queried[:10]:
            lines.append('ตรวจช่วง '+day_label(row['start'])+' ถึง '+day_label(row['end'])+
                (' เฉพาะ '+label_text(row['criteria']) if row['criteria'] else '')+(' ครบแล้ว' if row['complete'] else ' ยังไม่ครบ/มีนัดที่ยังไม่ชัดเจน'))
        if len(queried)>10:lines.append(f'แสดง 10 ช่วงแรกจาก {len(queried)} ช่วงที่ตรวจในคำถามรอบนี้')
    at=datetime.fromisoformat(output['checked_at']).astimezone(BANGKOK)
    lines.append('ข้อมูลข้างต้นตรวจ ณ '+day_label(at.date().isoformat())+' '+at.strftime('%H:%M')+' เวลาไทย')
    return Result('answer','\n'.join(lines),{'data_inventory':True,'read_only':True,
        'financial_write_authorized':False,'calendar_write_authorized':False,'tool_result':deepcopy(output)})
