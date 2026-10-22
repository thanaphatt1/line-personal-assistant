"""Schema-driven patches of owned pending records, never provider writes.

Language interpretation belongs to the planner. Field types, source spans,
versions and the final delivered review remain application responsibilities.
Existing domain adapters retain their date, amount and mutation invariants.
"""
from copy import copy,deepcopy
from datetime import datetime

from pydantic import Field
from .tables import Strict,normalize,Column,TableError
from .table_workflow import schema
from .history_context import digest
from .tool_errors import PlanningError,recovery

NAME='patch_pending_records'
INSTRUCTIONS=(
    ' pending_records describes current editable drafts, record IDs, field types and choices. '
    'For a follow-up correction/completion prefer patch_pending_records: interpret the owner semantically, '
    'send only changed fields and literal owner evidence, retain omitted fields/records. '
    'Interpret the CURRENT owner request to decide whether to change a draft, which record and which facts it selects. '
    'First distinguish an actual requested change from a hypothetical or negated change. If no actual field change is requested, answer without a patch. '
    'Do not repeat the previous turn\'s operation or complete other pending fields merely because old instructions or choices appear in history. '
    'Choose create versus update from the CURRENT request: a follow-up referring to a pending record keeps that record ID, even when it is incomplete. '
    'Resolving a historical amount/date/name reference does not create a new record. Creation tools require a request for a NEW record; existing-record follow-ups use this patch tool. '
    'Questions, quoted labels and conditional add-if-absent requests can request preparation; hypothetical examples, negations and unrelated chat do not. '
    'Each changed field cites its original OWNER source_ref and a literal span, from the current message or verified visible owner history when the current request refers to it. '
    'Keep the original source timestamp for relative dates. Do not resend retained facts or treat old messages as new instructions or consent. '
    'For interpreted dates/times first resolve_temporal, then send its normalized projection as value and its temporal_ref with the original source_ref/source_text. '
    'Keep temporal_ref when correcting a citation: a computed clock/date need not appear literally in owner text. A literal subspan or containing span from the same original owner message is valid evidence; the full resolution proof remains authoritative. '
    'A field with interpretation=choice permits a semantic proposal of a declared choice, including synonyms or ordinals; '
    'do not require the owner to repeat a canonical label or a prescribed sentence. '
    'category already prepares enrollment of an unknown name: omit redundant catalog_action. catalog_action=new alone starts its category question; existing_item selects a displayed existing name. '
    'Combine a requested rename and category in one patch. Never guess a missing choice from an item name. '
    'Use the focused record for a short answer to its question; ask when the intended record/choice is ambiguous. '
    'Patches only prepare a reviewed proposal. They cannot save, confirm, grant access or change schema. '
    'When repair_feedback is returned, correct the plan using its current schema and available OWNER facts. '
    'Ask only for genuinely missing or ambiguous facts; never invent evidence to satisfy validation. '
)


class Change(Strict):
    field:str=Field(min_length=1,max_length=64)
    value:str|int|bool|None
    source_text:str=Field(min_length=1,max_length=2000)
    source_ref:str=Field(min_length=1,max_length=64)
    temporal_ref:str|None=None


class Record(Strict):
    record_ref:str=Field(min_length=1,max_length=64)
    changes:list[Change]=Field(min_length=1,max_length=40)


class Patch(Strict):
    draft_ref:str=Field(min_length=1,max_length=64)
    draft_version:int|str
    records:list[Record]=Field(min_length=1,max_length=8)


def field(name,label,kind='text',**extra):
    return {'name':name,'label':label,'type':kind,**extra}


def views(h):
    result=[]
    _,shown=h._presented_result()
    focus=shown.data.get('tool_result',{}).get('pending_focus',{}) if shown else {}
    for view in h._workflow():
        d=h._draft(view['draft_ref']);tax=h._taxonomy()
        records=[]
        for ordinal,e in enumerate(d['entries'],1):
            fields=[field('item','ชื่อรายการ'),field('amount','จำนวนเงิน','decimal'),
                    field('date_text','ข้อความวันที่'),field('note','หมายเหตุ')]
            resolution=next((r for r in view['item_resolutions'] if r['entry_ref']==e['entry_ref']),None)
            categories=resolution['category_choices'] if resolution else tax.choices(e['item'] or '')['categories']
            if categories and h.item_enrollment_enabled:
                fields += [field('catalog_action','เตรียมชื่อใหม่','enum',choices=['new'],interpretation='choice',implied_by={'category':'new'}),
                    field('category','หมวด','enum',choices=[c['label'] for c in categories],interpretation='choice')]
                names=[c['item'] for c in resolution['existing_item_choices']] if resolution else []
                if names:fields.append(field('existing_item','เลือกชื่อเดิม','enum',choices=names,interpretation='choice'))
                subs=list(dict.fromkeys(s['label'] for c in categories for s in c['subcategories'] if s['label']))
                if subs:fields.append(field('subcategory','หมวดย่อย','enum',choices=subs,interpretation='choice'))
            records.append({'record_ref':e['entry_ref'],'ordinal':ordinal,'values':{k:deepcopy(e.get(k)) for k in ['item','amount','date','note']},
                'fields':fields,'question_focus':bool(resolution and resolution['question_focus'] or
                    focus.get('draft_ref')==d['draft_ref'] and focus.get('version')==d['version'] and focus.get('record_ref')==e['entry_ref'])})
        result.append({'draft_ref':d['draft_ref'],'version':d['version'],'domain':'expense','records':records,
            'missing':deepcopy(d['missing_fields'])})
    draft=h.table_flow.draft()
    if draft and draft['operation']!='delete':
        table,_=h.table_flow.registry.get(draft['table_id'])
        result.append({'draft_ref':draft['ref'],'version':digest(draft),'domain':'table','records':[
            {'record_ref':draft['ref'],'ordinal':1,'values':deepcopy(draft['values']),
             'fields':[c.model_dump(exclude_none=True) for c in table.columns],'question_focus':True}],
            'missing':[c.name for c in table.columns if c.required and draft['values'].get(c.name) is None]})
    if h.calendar_flow.enabled:
        fields=[field(k,label) for k,label in [('title','ชื่อเรื่อง'),('date_text','วันนัด'),('time_text','เวลาเริ่ม'),
            ('end_date_text','วันสิ้นสุด'),('end_time_text','เวลาสิ้นสุด'),('duration_text','ระยะเวลา')]]+[field('all_day','ทั้งวัน','boolean')]
        batched=set()
        for batch in h.calendar_flow.batches.views():
            records=[]
            for event in batch['events']:
                if event['status'] in {'cancelled','recorded'}:continue
                batched.add(event['draft_ref'])
                records.append({'record_ref':event['draft_ref'],'ordinal':event['ordinal'],'version':event['version'],'values':event['facts'],
                    'fields':fields,'question_focus':False})
            if records:result.append({'draft_ref':batch['batch_ref'],'version':batch['version'],'domain':'calendar_batch','records':records})
        for event in h.calendar_flow.views():
            if event['draft_ref'] not in batched:
                result.append({'draft_ref':event['draft_ref'],'version':event['version'],'domain':'calendar','records':[
                    {'record_ref':event['draft_ref'],'ordinal':1,'values':event['facts'],'fields':fields,'question_focus':False}]})
    return result


def contextualize(h,call,output):
    if output.get('ok') is not False:return output
    import json
    try:args=json.loads(call['arguments'])
    except (ValueError,TypeError,KeyError):return output
    if not isinstance(args,dict):return output
    view=getattr(h,'_pending_record_views',{}).get(args.get('draft_ref'))
    if not view or view['version']!=args.get('draft_version'):return output
    result={**output,'pending_patch_error':True,'domain':view['domain'],
        'message':output.get('message','ขั้นตอนตรวจการแก้ร่างไม่สำเร็จ จึงยังระบุสาเหตุที่เจาะจงกว่านี้ไม่ได้')}
    records=args.get('records',[])
    if isinstance(records,list) and len(records)==1 and isinstance(records[0],dict):
        target=records[0].get('record_ref')
        if target in {r['record_ref'] for r in view['records']}:
            result['pending_focus']={'draft_ref':view['draft_ref'],'version':view['version'],'record_ref':target}
    return result


def definitions(available):
    if not available:return []
    return [{'type':'function','name':NAME,'strict':True,
        'description':'Prepare field changes to owned pending records using pending_records schema. Literal fields copy owner facts; choice fields permit semantic selection of a declared value with literal owner evidence. Omitted fields/records retain facts. Never saves or confirms.',
        'parameters':schema(Patch)}]


def planner_tools(base,available):
    """Keep creation and pending updates distinct in the actual tool schema.

    Domain handlers remain compatible with old calls/receipts. New planners
    cannot select an old update path whose language gate contradicts the
    general field-patch contract.
    """
    if not available:return base
    result=deepcopy(base)
    for tool in result:
        name=tool['name'];properties=tool['parameters'].get('properties',{})
        if name=='prepare_item_enrollment':
            tool['description']='Legacy compatibility tool. For current pending name/category changes use patch_pending_records with all requested changed fields and semantic choices from pending_records.'
        if name in {'prepare_expenses','prepare_calendar_event','prepare_table_change'}:
            for key in ['draft_ref','draft_version']:
                if key in properties:properties[key]['enum']=[None]
            tool['description']='Create a NEW draft/proposal. Correct or complete existing pending records with patch_pending_records. '+tool['description']
        if name=='prepare_calendar_events':
            for key in ['batch_ref','batch_version']:properties[key]['enum']=[None]
            event=properties['events']['items']['properties']
            for key in ['draft_ref','draft_version']:event[key]['enum']=[None]
            tool['description']='Create a NEW set. Existing pending member changes use patch_pending_records and its record ordinals. '+tool['description']
    return result


class PatchError(PlanningError):
    def __init__(self,code,field=None,*,kind='assistant_arguments'):
        super().__init__(code,kind=kind,fields=[field] if field else [])
        self.field=field


def validate_targets(p,view):
    if p.draft_version!=view['version']:raise PatchError('patch_stale_version')
    if len({r.record_ref for r in p.records})!=len(p.records):raise PatchError('patch_duplicate_record')
    records={r['record_ref']:r for r in view['records']}
    for record in p.records:
        if record.record_ref not in records:raise PatchError('patch_unknown_record')
        if len({v.field for v in record.changes})!=len(record.changes):raise PatchError('patch_duplicate_field')
        fields={f['name']:f for f in records[record.record_ref]['fields']}
        for change in record.changes:
            if change.field not in fields:raise PatchError('patch_unknown_field',change.field)


def validate(h,p,view):
    validate_targets(p,view)
    records={r['record_ref']:r for r in view['records']}
    for record in p.records:
        fields={f['name']:f for f in records[record.record_ref]['fields']}
        for change in record.changes:
            f=fields[change.field]
            rows=h._owner_sources([change.source_ref])
            if not any(change.source_text in r['text'] for r in rows):raise PatchError('patch_owner_evidence',change.field)
            try:normalize(Column.model_validate({k:v for k,v in f.items() if k not in {'interpretation','implied_by'}}),change.value)
            except (ValueError,TableError):raise PatchError('patch_invalid_value',change.field) from None
            if change.temporal_ref:
                from .temporal import get,projection
                part='date' if f['type']=='date' else projection(change.field)
                if part is None:raise PatchError('temporal_unknown_field',change.field)
                value,_=get(h,change.temporal_ref,part,source_ref=change.source_ref,source_text=change.source_text)
                if change.value!=value:raise PatchError('temporal_value_mismatch',change.field)
            if not change.temporal_ref and f['type']=='text' and change.value is not None and change.value not in change.source_text:
                raise PatchError('patch_literal_value',change.field)
            # A literal substring cannot turn part of a date/count/number
            # into a money fact by cropping its surrounding owner text.
            if view['domain']=='expense' and change.field=='amount' and change.value is not None:
                from decimal import Decimal
                from .expense_sources import roles
                labels=[*(r['values'].get('item') for r in view['records']),
                    *(v.value for r in p.records for v in r.changes if v.field=='item')]
                def proved(row):
                    spans=[m.span() for label in labels if label for m in h._item_spans(row['text'],label)]
                    values=roles(row['text'],datetime.fromisoformat(row['at']),spans)['money']
                    return any(v.value is not None and Decimal(v.value)==Decimal(str(change.value))
                        and any(start<=v.start and v.end<=start+len(change.source_text)
                            for start in range(len(row['text'])) if row['text'].startswith(change.source_text,start)) for v in values)
                if not any(proved(row) for row in rows):raise PatchError('patch_owner_evidence',change.field)
            if not change.temporal_ref and view['domain'] in {'calendar','calendar_batch'} and change.value is not None and change.field not in {'all_day','title'}:
                from .calendar_workflow import literal_fact
                if not any(literal_fact(change.value,row['text'],change.field) is not None for row in rows):
                    raise PatchError('patch_owner_evidence',change.field)


def canonical_patch(p,view):
    """Omit retained facts and schema-implied redundant actions.

    This never adds a field, fact or permission. A conflicting action remains
    visible to validation. Exact retained values keep their original proof;
    only effective changes need new evidence and the final proposal review.
    """
    records={r['record_ref']:r for r in view['records']};result=[]
    for record in p.records:
        fields={f['name']:f for f in records.get(record.record_ref,{}).get('fields',[])}
        present={v.field for v in record.changes}
        retained=records.get(record.record_ref,{}).get('values',{})
        values=[v for v in record.changes if not (v.field in retained and type(v.value) is type(retained[v.field]) and v.value==retained[v.field])
            and not any(k in present and v.value==value
            for k,value in fields.get(v.field,{}).get('implied_by',{}).items())]
        if values:result.append(record.model_copy(update={'changes':values}))
    return p.model_copy(update={'records':result})


def evidence(h,record):
    """Only validated original owner spans enter the private adapter path."""
    result={}
    for v in record.changes:
        row={**h._owner_sources([v.source_ref])[0],'text':v.source_text}
        if v.temporal_ref:
            from .temporal import get,projection
            value,proof=get(h,v.temporal_ref,projection(v.field) or 'date',source_ref=v.source_ref,source_text=v.source_text)
            row.update(temporal=proof,value=value)
        result[v.field]=row
    return result


def staged_expense(h,p):
    """Validate all changes in memory; publish one draft only after success."""
    stage=copy(h);stage.state=copy(h.state)
    stage.state.drafts=deepcopy(h.state.drafts);stage.state.draft_proofs=deepcopy(h.state.draft_proofs)
    stage.state.checkpoint=lambda:None
    stage._request_views=deepcopy(h._request_views);stage._read_views={};stage.trace=[]
    stage.previous_prices=copy(h.previous_prices);stage.previous_prices.h=stage
    stage._patch_field_sources={r.record_ref:evidence(h,r) for r in p.records}
    d=stage._draft(p.draft_ref)
    if h._request_views.get(p.draft_ref)!=h._view(d):raise PatchError('patch_unoffered_draft')
    facts=[]
    for record in p.records:
        changes=[v for v in record.changes if v.field in {'item','amount','date_text','note'}]
        if not changes:continue
        args={v.field:v.value for v in changes}
        for v in changes:
            if v.temporal_ref:
                if v.field!='date_text':raise PatchError('temporal_unknown_field',v.field)
                args.update(date_text=None,date_ref=v.temporal_ref)
        if any(v.value is None for v in changes):raise PatchError('patch_expense_clear_unsupported',kind='unsupported')
        args.update(entry_ref=record.record_ref,source_refs=list(dict.fromkeys(v.source_ref for v in changes)),
            field_sources={k:[v.source_ref for v in changes if v.field==('date_text' if k=='date' else k)] for k in ['item','amount','date','note']})
        if 'item' in args:args['item_text']=args['item']
        facts.append(args)
    if facts:
        args={'draft_ref':d['draft_ref'],'draft_version':d['version'],'entries':facts}
        result=stage._prepare(args,interpreted_patch=True)
        if not result.get('ok'):return result
        stage.trace.append({'call':{'name':'prepare_expenses'},'result':deepcopy(result)})
    for record in p.records:
        values={v.field:v.value for v in record.changes}
        choices={k:values[k] for k in ['category','subcategory','existing_item'] if k in values}
        if not choices and 'catalog_action' not in values:continue
        d=stage._draft(p.draft_ref);stage._request_views[d['draft_ref']]=stage._view(d)
        e=next(e for e in d['entries'] if e['entry_ref']==record.record_ref)
        if 'existing_item' in choices and (set(choices)!={'existing_item'} or 'catalog_action' in values):raise PatchError('patch_conflicting_choices')
        if 'existing_item' in choices:step='use_existing_item'
        elif 'subcategory' in choices and 'category' not in choices:step='choose_subcategory'
        else:step='begin_new_item' if e.get('enrollment') is None else 'choose_category' if 'category' in choices else 'show_choices'
        result=stage._enroll_item({'draft_ref':d['draft_ref'],'draft_version':d['version'],'entry_ref':record.record_ref,
            'resolution_step':step,'category':None,'subcategory':None,'existing_item':None,**choices},interpreted_patch=True)
        if not result.get('ok'):return result
    d=stage._draft(p.draft_ref)
    if d==h._draft(p.draft_ref):raise PatchError('patch_no_changes')
    h.state.drafts[p.draft_ref]=deepcopy(d);h._seal(h.state.drafts[p.draft_ref])
    if d['missing_fields']:h.state.checkpoint()
    target=next((r.record_ref for r in p.records if any(v.field in {'category','subcategory','catalog_action'} for v in r.changes)),p.records[0].record_ref)
    return h._enrollment_result(d,target)


def execute(h,args):
    p=Patch.model_validate(args)
    available=getattr(h,'_pending_record_views',{})
    view=available.get(p.draft_ref)
    if not view:raise PatchError('patch_unoffered_draft')
    # Rebuild against current authority; old model context never supplies a
    # version, provider binding or field permission after state has changed.
    fresh=next((v for v in views(h) if v['draft_ref']==p.draft_ref),None)
    if not fresh or fresh!=view:raise PatchError('patch_stale_version')
    validate_targets(p,view)
    p=canonical_patch(p,view)
    if not p.records:return {'ok':True,'status':'pending_unchanged','pending_records':[deepcopy(view)]}
    validate(h,p,view)
    if view['domain']=='expense':return staged_expense(h,p)
    if view['domain']=='table':
        d=h.table_flow.draft()
        return h.table_flow.tool('prepare_table_change',{'table_id':d['table_id'],'operation':d['operation'],'record_id':d['record_id'],
            'draft_ref':d['ref'],'source_refs':list(dict.fromkeys([h._current['source_ref'],*(v.source_ref for r in p.records for v in r.changes)])),
            'values':[{'field':v.field,'value':v.value,'source_text':v.source_text,'temporal_ref':v.temporal_ref} for r in p.records for v in r.changes]})
    if view['domain'] in {'calendar','calendar_batch'}:
        records={r['record_ref']:r for r in view['records']};events=[]
        for r in p.records:
            values={v.field:v.value for v in r.changes}
            values.update(draft_ref=r.record_ref,draft_version=records[r.record_ref].get('version',p.draft_version),
                source_refs=list(dict.fromkeys(v.source_ref for v in r.changes)))
            events.append(values)
        sources={r.record_ref:evidence(h,r) for r in p.records}
        if view['domain']=='calendar_batch':return h.calendar_flow.tool('prepare_calendar_events',{'batch_ref':p.draft_ref,'batch_version':p.draft_version,'events':events},interpreted_patch=True,field_evidence=sources)
        return h.calendar_flow.tool('prepare_calendar_event',events[0],interpreted_patch=True,field_evidence=sources)
    raise PatchError('patch_unsupported_domain',kind='unsupported')


def failure(error):
    reasons={'patch_stale_version':'ร่างเปลี่ยนแล้ว ต้องใช้เวอร์ชันปัจจุบัน',
        'patch_unoffered_draft':'ยังไม่มีร่างที่ตรวจสอบได้สำหรับคำขอนี้',
        'patch_unknown_field':'ฟิลด์ที่เสนอไม่มีใน schema ของรายการนี้',
        'patch_invalid_value':'ค่าที่เสนอไม่ตรงชนิดข้อมูลหรือตัวเลือกของฟิลด์',
        'patch_owner_evidence':'ข้อความอ้างอิงไม่ตรงกับข้อความของคุณ',
        'patch_literal_value':'ชื่อหรือข้อความที่เสนอไม่ตรงกับข้อความอ้างอิง',
        'patch_unknown_record':'รายการที่เลือกไม่มีในร่างนี้',
        'patch_conflicting_choices':'การเลือกชื่อเดิมกับการเตรียมชื่อใหม่ขัดกัน',
        'patch_expense_clear_unsupported':'ยังล้างค่ารายจ่ายด้วยการแก้แบบนี้ไม่ได้',
        'owner_no_write':'ข้อความนี้ยังไม่ใช่คำขอให้แก้ร่างที่ชัดเจน'}
    return {'ok':False,'error':str(error),'recovery':recovery(error,tools=[NAME,'resolve_temporal']),'pending_patch_error':True,'invalid_fields':[error.field] if error.field else [],
        'message':reasons.get(str(error),'การเปลี่ยนข้อมูลที่เสนอไม่ผ่านการตรวจ จึงยังไม่ได้ใช้การแก้ไขนี้')}
