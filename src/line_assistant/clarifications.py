"""Ask one genuinely missing fact; retain the full draft in state and review."""
from copy import deepcopy
import re
from .state import Result


def compact(host,result):
    if not getattr(host,'raw_history_enabled',False) or result.status!='clarification' or result.data.get('clarification_question'):return result
    output=result.data.get('tool_result') or {}
    if not output.get('ok') or output.get('validation_feedback') or output.get('validation_issues') or output.get('invalid_fields'):return result
    question=None;focus=None
    entries=output.get('entries')
    if entries and not output.get('item_enrollment'):
        if (output.get('previous_price_lookup') is not None or any(e.get('previous_price') or e.get('date_issue') not in {None,'continuation_date_required'} for e in entries)):
            return result
        # Catalog decisions and validation explanations have their own exact
        # question/choice contract. Never shorten warnings or uncertainty.
        if output.get('catalog_question'):question=output['catalog_question']
        else:
            fields=output.get('missing_fields') or []
            found=re.fullmatch(r'entries\[(\d+)\]\.(item|amount|date|date\.day|date\.month)',fields[0]) if fields else None
            if found and int(found[1])<len(entries):
                entry=entries[int(found[1])];name=entry.get('item') or 'รายการที่ '+str(int(found[1])+1)
                ask={'item':'ชื่ออะไรครับ?','amount':'จ่ายเท่าไรครับ?','date':'เป็นรายจ่ายวันที่เท่าไรครับ?',
                     'date.day':'เป็นรายจ่ายวันที่เท่าไรครับ?','date.month':'หมายถึงเดือนไหนครับ?'}[found[2]]
                question=name+' '+ask;focus={'draft_ref':output.get('draft_ref'),'version':output.get('draft_version'),
                                          'record_ref':entry.get('entry_ref'),'field':found[2]}
    if output.get('table_draft') and output.get('missing'):
        draft=output['table_draft'];table,_=host.table_flow.registry.get(draft['table_id'])
        field=output['missing'][0];column=table.column(field)
        name=draft['values'].get(table.display_field) if table.display_field else None
        question=(str(name)+' — ' if name else '')+column.label+'เป็นอะไรครับ?'
        if column.choices:question+='\n'+ ' / '.join(column.choices)
        focus={'draft_ref':draft['ref'],'record_ref':draft['ref'],'field':field}
    view=result.data.get('calendar_batch_view')
    if not view and result.data.get('calendar_view'):view={'events':[result.data['calendar_view']]}
    if view:
        from .calendar_event_types import clarification_fields
        # Timing conflicts and invalid dates still display their full diagnosis.
        events=view['events']
        # An untitled member of a set keeps the full listing: its own source
        # wording, not an ordinal alone, tells the owner which slot is meant.
        if not any(e.get('issues') or len(events)>1 and not e['facts'].get('title') for e in events):
            for event in events:
                fields=clarification_fields(event)
                if not fields:continue
                subject=event['facts'].get('title') or 'นัดนี้'
                field=fields[0]
                ask={'title':'ชื่ออะไรครับ?','date':'วันไหนครับ?','time':'เริ่มกี่โมงครับ?',
                     'end':'เวลาสิ้นสุดกี่โมง หรือใช้เวลานานเท่าไรครับ?','time_or_all_day':'เป็นนัดทั้งวัน หรือเริ่มกี่โมงครับ?'}.get(field)
                if ask:
                    question=subject+('' if field=='title' else ' ')+ask
                    focus={'draft_ref':view.get('batch_ref') or event.get('draft_ref'),
                           'version':view.get('version') or event.get('version'),'record_ref':event.get('draft_ref'),'field':field}
                break
    if not question or len(question.encode('utf-16-le'))>8000:return result
    data=deepcopy(result.data);data['clarification_question']=question
    if focus:data['clarification_focus']=focus
    if data.get('line_message'):data['line_message']={'type':'text','text':question}
    return Result(result.status,question,data)
