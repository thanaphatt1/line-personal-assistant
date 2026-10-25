"""Metadata-driven relational analysis of complete current-request snapshots."""
from copy import deepcopy
from datetime import date,datetime,timedelta
import re
import sqlite3
from pydantic import Field,ValidationError
from .tables import Strict
from .table_workflow import schema
from .history_context import digest
from .language import BANGKOK
from .tool_errors import PlanningError,recovery
from .relational_sql import execute as run_sql

NAME='query_relations'


class Plan(Strict):
    query_refs:list[str]=Field(min_length=1,max_length=32,description='Current-request complete relation refs from relations_catalog. Schema metadata and refs from prior turns are not executable data.')
    sql:str=Field(min_length=1,max_length=32_000,description=(
        'Read-only SQLite SELECT/CTEs over offered sql_table names and columns. Use WHERE for the requested predicates. '
        'Decimal columns use exact TEXT with COLLATE decimal: SUM/decimal_sum and their window forms return exact text; '
        'sort derived decimal aliases with COLLATE decimal. Use decimal_add/sub/mul, decimal_div(a,b,places) and decimal_cmp; '
        'quote decimal constants and use decimal_div(SUM(column),COUNT(column),places) for averages. Native AVG/TOTAL, '
        'float casts and floating point results are unavailable. Empty SUM is null; COALESCE can return a zero total. '
        'Unavailable mapping columns must not be guessed. Preserve duplicate payment occurrences. '
        'Output unique column labels with units for money, and only fields needed for the answer.'))
    source_refs:list[str]=Field(min_length=1,max_length=16)
    continue_analysis:bool=Field(default=False,description='False when these rows answer the question: finish directly. True only when another computation needs this derived relation.')


def definitions():
    parameters=schema(Plan)
    parameters['properties']['continue_analysis']={'type':'boolean','enum':[True],
        'description':'Always true for the planner: inspect the executed predicates, result and source coverage against the owner request before finish_query_analysis report/clarify. Execution success alone does not establish intent coverage.'}
    return [{'type':'function','name':NAME,'strict':True,'description':
        'Primary saved-data filtering and analysis, including one named label, exclusions, several labels, joins, computed or ranked results: read-only SQLite SELECT/WHERE/NOT/AND/OR/JOIN/CASE/subquery/window over complete observed relations_catalog snapshots. Resolve labels using source metadata and categorical choices. Exact decimal SUM; no writes/provider SQL. First read sources with continue_analysis=true, expense filter=null and view=list.',
        'parameters':parameters}]


def columns(*values):return [{'name':name,'type':kind} for name,kind in values]


def materialize(h,ref):
    try:output=h._query_reads[ref]
    except KeyError:raise PlanningError('relation_reference_unavailable') from None
    if not output.get('ok') or not output.get('complete'):raise PlanningError('relation_incomplete_source',kind='unavailable')
    records=deepcopy(output.get('analysis_records'))
    if records is None or len(records)!=output.get('count'):raise PlanningError('relation_rows_unavailable',required=['complete raw rows; read with continue_analysis=true and expense view=list'])
    domain=output['domain'];unavailable=[]
    if domain=='table':
        table,_=h.table_flow.registry.get(output['table_id'])
        if output['scope']['binding']!=h.table_flow.registry.binding(table.id):raise PlanningError('relation_binding_changed')
        selected=output['scope']['query'].get('select') or [c.name for c in table.columns]
        cols=[{'name':'id','type':'text'},*[c.model_dump(exclude_none=True) for c in table.columns if c.name in selected]]
        records=[dict(id=r['id'],**r['values']) for r in records]
    elif domain=='expense':
        cols=columns(('date','date'),('item','text'),('amount','decimal'),('note','text'))
        cols[2]['unit']='บาท'
        # Catalog identity is source metadata, not a model classification. Keep
        # raw labels available; block uses of incomplete mapping columns so
        # SQL NULL/NOT semantics cannot silently omit unknown memberships.
        cols+=columns(('canonical_item','text'),('category','text'),('subcategory','text'))
        from .catalog import normalized
        try:
            tax=h._taxonomy();by_name={normalized(r.item):r for r in tax.rows}
        except Exception:tax=None;by_name={}
        for r in records:
            identity=tax.catalog.identity(r['item']) if tax else None
            mapped=by_name.get(normalized(identity.canonical)) if identity and identity.canonical else None
            for field in ('canonical_item','category','subcategory'):
                value=(mapped.item if field=='canonical_item' else getattr(mapped,field)) if mapped else None
                r[field]=value if value and value.strip() else None
                if r[field] is None and field not in unavailable:unavailable.append(field)
        if tax:
            for c in cols:
                if c['name'] in {'category','subcategory'}:
                    c['choices']=sorted({getattr(r,c['name']) for r in tax.rows if getattr(r,c['name']) and getattr(r,c['name']).strip()})
    elif domain=='calendar':
        cols=columns(('read_ref','text'),('title','text'),('start','text'),('end','text'),('all_day','boolean'),('start_date','date'),('last_date','date'))
        for r in records:
            if r['all_day']:
                first=date.fromisoformat(r['start']);last=date.fromisoformat(r['end'])-timedelta(days=1)
            else:
                first=datetime.fromisoformat(r['start']).astimezone(BANGKOK).date()
                last=(datetime.fromisoformat(r['end']).astimezone(BANGKOK)-timedelta(microseconds=1)).date()
            # Reads prove only the part of an overlapping event inside scope.
            r['start_date']=max(first,date.fromisoformat(output['scope']['start'])).isoformat()
            r['last_date']=min(last,date.fromisoformat(output['scope']['end'])).isoformat()
        for name in ['description','location']:
            if all(name in r for r in records) and records:
                cols+=columns((name,'text'))
                if any(r.get('details_truncated') for r in records):unavailable.append(name)
    elif domain=='relation':cols=deepcopy(output['columns'])
    else:raise PlanningError('relation_unsupported_source',kind='unsupported')
    notes=list(output.get('provenance_notes',[]))
    if domain in {'expense','calendar'}:
        from .read_queries import facts
        notes.extend(value for key,value in facts(output,datetime.fromisoformat(h._current['at'])).items()
                     if key.startswith('remembered_'))
    return {'query_ref':ref,'sql_table':'q_'+digest(ref)[:16],'domain':domain,'columns':cols,
        'row_count':len(records),'records':records,'unavailable_fields':unavailable,
        'scope':{k:deepcopy(v) for k,v in output['scope'].items() if k not in {'binding','workbook','calendar','calendar_read_ids_hash'}},
        'identity':'snapshot_only' if domain=='expense' else 'observed_rows',
        'analysis_input_only':bool(output.get('analysis_input_only')),
        'provenance_notes':list(dict.fromkeys(notes))}


def context(h):
    catalog=[]
    for ref,output in getattr(h,'_query_reads',{}).items():
        try:relation=materialize(h,ref);catalog.append({k:v for k,v in relation.items() if k!='records'})
        except PlanningError as error:catalog.append({'query_ref':ref,'domain':output['domain'],'available':False,'error_code':str(error)})
    return {'relations_catalog':catalog,'relational_engine':{'dialect':'SQLite','version':sqlite3.sqlite_version,
        'read_only':True,'decimal_storage':'TEXT COLLATE decimal','arithmetic':'exact decimal helpers; decimal_div rounds to explicit places',
        'unavailable':['provider SQL','files/extensions','writes','AVG','TOTAL','binary64 output']}}


def message(rows,count,columns=()):
    from .reply_display import quantity_text
    units={c['name']:c.get('unit') for c in columns}
    def text(v):
        value=re.sub(r'[\x00-\x1f]',' ',str(v)) if v is not None else 'ไม่มีค่า'
        return value[:300]+('…' if len(value)>300 else '')
    def cell(k,v):
        unit=units.get(k)
        return text(quantity_text(v,unit))+(' '+text(unit) if unit else '') if v is not None else text(v)
    def label(k):
        return (re.sub(r'\s*\(บาท\)|_?baht$','',text(k),flags=re.I) or text(k)) if units.get(k) in {'บาท','THB'} else text(k)
    lines=['ผลลัพธ์ครับ']
    lines += [(str(i)+'. ' if len(rows)>1 else '')+' | '.join(label(k)+': '+cell(k,v) for k,v in row.items()) for i,row in enumerate(rows,1)]
    if count>len(rows):lines.append(f'แสดง {len(rows)} จาก {count} แถวของผลลัพธ์ที่คำนวณครบแล้ว')
    if not count:lines.append('ไม่พบแถวที่ตรงเงื่อนไข')
    return '\n'.join(lines)


def followup_scope(h,relations,used,sql,result_columns,row_count):
    """Retain one verified source scope and SQL intent, never prior rows/refs.

    Multiple distinct source scopes have no single unambiguous previous period.
    SQL references still expire at request end and must be re-read next turn.
    """
    scopes=[]
    for relation in relations:
        if relation['sql_table'] not in used:continue
        output=h._query_reads[relation['query_ref']]
        scope=output.get('followup_scope') if output['domain']=='relation' else output.get('scope')
        if not scope or not scope.get('start') or not scope.get('end'):return None
        scopes.append(scope)
    if not scopes:return None
    bases=[{k:v for k,v in s.items() if k not in {'analysis','continue_analysis'}} for s in scopes]
    if any(s!=bases[0] for s in bases[1:]):return None
    # A complete input is often read as an unfiltered list even when the final
    # answer is a filtered scalar or grouped result. Do not present these fetch
    # controls as the last delivered selection/view. Keep them with the inputs.
    scope={k:deepcopy(v) for k,v in bases[0].items()
           if k not in {'filter','listing','view','group_by','order_by','descending','limit'}}
    scope['analysis']={'sql':sql,'result_columns':deepcopy(result_columns),'row_count':row_count,
        'inputs':[{k:deepcopy(r[k]) for k in ('sql_table','domain','columns','scope')}
        for r in relations if r['sql_table'] in used]}
    return scope


def execute(h,args):
    base={'relational_query':True,'query_analysis':True,'domain':'relation','read_only':True,
        'financial_write_authorized':False,'calendar_write_authorized':False}
    try:
        p=Plan.model_validate(args);h._owner_sources(p.source_refs)
        if h._current['source_ref'] not in p.source_refs:raise PlanningError('relation_current_source_required')
        if len(set(p.query_refs))!=len(p.query_refs):raise PlanningError('relation_duplicate_reference')
        relations=[materialize(h,ref) for ref in p.query_refs]
        names,records,used=run_sql(relations,p.sql)
        from .relational_metadata import result_columns
        cols=result_columns(relations,p.sql,names,records)
        # Derived decimal strings keep an explicit numeric ordering option;
        # SQLite does not propagate collation through aggregate aliases.
        scope={'domain':'relation','query_refs':[r['query_ref'] for r in relations if r['sql_table'] in used],
            'sql':p.sql,'source_scopes':[r['scope'] for r in relations if r['sql_table'] in used]}
        ref='read_'+digest([h._active_event_id,p.sql,scope['query_refs'],records])[:20]
        output={**base,'ok':True,'complete':True,'status':'answer','query_ref':ref,'scope':scope,'columns':cols,
            'count':len(records),'rows':records[:20],'analysis_records':records,'shown':min(len(records),20),
            'continue_analysis':p.continue_analysis,'numeric_semantics':'Exact decimal SUM/add/sub/mul; decimal_div explicitly rounds half-even to requested places',
            'message':message(records[:20],len(records),cols)}
        previous=followup_scope(h,relations,used,p.sql,cols,len(records))
        notes=list(dict.fromkeys(note for r in relations if r['sql_table'] in used for note in r.get('provenance_notes',[])))
        if notes:
            output['provenance_notes']=notes
            output['message']+='\n'+'\n'.join(notes)
        if previous:output['followup_scope']=previous
        h._query_reads[ref]=deepcopy(output)
        return {k:v for k,v in output.items() if k!='analysis_records'}
    except (PlanningError,ValidationError,ValueError) as error:
        code=str(error) if isinstance(error,PlanningError) else 'relation_invalid_arguments'
        causes={'relation_unknown_column':'แผนค้นหาอ้างคอลัมน์ที่ไม่มีในข้อมูลชุดนี้',
            'relation_unknown_table':'แผนค้นหาอ้างตารางที่ยังไม่ได้อ่านในรอบนี้',
            'relation_rows_unavailable':'ผลค้นหาก่อนหน้ามีเพียงสรุป จึงต้องอ่านรายการให้ครบก่อน',
            'relation_incomplete_source':'ข้อมูลต้นทางยังอ่านไม่ครบ จึงยังสรุปเป็นยอดทั้งหมดไม่ได้',
            'relation_inexact_numeric':'การคำนวณนี้ใช้เลขทศนิยมแบบประมาณค่า ต้องเปลี่ยนเป็นการคำนวณทศนิยมที่แน่นอน',
            'relation_incomplete_field':'ข้อมูลคอลัมน์ที่ใช้กรองหรือคำนวณยังไม่ครบ จึงยังสรุปยอดทั้งหมดตามเงื่อนไขนี้ไม่ได้',
            'relation_resource_limit':'แผนค้นหานี้ใช้ทรัพยากรเกินขอบเขตของรอบประมวลผล'}
        return {**base,'ok':False,'complete':False,'status':'clarification','error_code':str(error) if isinstance(error,PlanningError) else 'relation_invalid_arguments',
            **({'sql_diagnostic':error.detail} if hasattr(error,'detail') else {}),
            'recovery':recovery(error,kind=None if isinstance(error,PlanningError) else 'assistant_arguments',tools=[NAME,'query_expenses','query_calendar','query_table','describe_table']),
            'message':causes.get(code,'แผนค้นหาหรือข้อมูลที่อ้างอิงยังใช้ไม่ได้')+'ครับ'}
