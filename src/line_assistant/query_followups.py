"""Delivered read clarifications, resolved by typed model selection, never writes.

The existing receipt owns the context. No second pending-workflow store or Thai
keyword/date router is introduced. A selection is revalidated against Calendar
before server arithmetic joins expenses to the confirmed Bangkok dates.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .expense_storage import storage_scope
from .history_context import digest
from .language import BANGKOK
from .model_api import object_schema
from .state import Result
from .transport import ApiError, NotReady, OutcomeUnknown

NAME = 'resolve_activity_dates'


class Resolve(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    clarification_ref: str = Field(min_length=1, max_length=64)
    candidate_refs: list[str] = Field(min_length=1, max_length=5)
    decision: Literal['confirm', 'exclude'] = 'confirm'
    selection_text: str = Field(min_length=1, max_length=200)
    source_refs: list[str] = Field(min_length=1, max_length=16)


def definitions(host):
    pending = current(host)
    if pending is None or not pending['candidates']:
        return []
    return [{'type': 'function', 'name': NAME, 'strict': True,
             'description': 'Resolve the current owner affirmative or negative selection among pending_query appointments in its original period. confirm affirms only selected candidate_refs; exclude rejects only those refs. Other candidates remain unconfirmed. Copy literal CURRENT selection_text identifying the selection. Re-reads Calendar and computes expenses on confirmed dates. No business writes. Questions and unrelated new queries do not select candidates.',
             'parameters': object_schema({
                 'clarification_ref': {'type': 'string'},
                 'candidate_refs': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 5},
                 'decision': {'type': 'string', 'enum': ['confirm', 'exclude']},
                 'selection_text': {'type': 'string'},
                 'source_refs': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 16}})}]


def current(host):
    """Return only retained, delivered, same-owner/source context; never write focus."""
    from .raw_history import speakers, message_hash, rendered_message
    from .read_queries import calendar_read_ids_hash
    now = datetime.fromisoformat(host._current['at'])
    for row in reversed(speakers(host.state, now, production=getattr(host, 'production', False))):
        if row['role'] != 'assistant' or row['event'] == host._active_event_id:
            continue
        receipt = host.state.receipts.get(row['event'])
        if not receipt or receipt.expires_at <= host.clock():
            continue
        if getattr(host, 'production', False):
            reply = host.state.replies.get(row['event'])
            if (not row.get('delivery_verified') or not reply or reply.status != 'sent'
                    or reply.content_hash != message_hash(rendered_message(receipt.result))
                    or host.state.event_workbooks.get(row['event']) != storage_scope(host.sheets)):
                continue
        data = receipt.result.data
        value = data.get('read_followup')
        if value:
            try:
                scope = value['scope']
                if (value['format'] != 'activity-read-followup-v1' or value['owner'] != host.settings.owner_id
                        or datetime.fromisoformat(value['expires_at']) <= now
                        or scope['workbook'] != storage_scope(host.sheets)
                        or scope['calendar'] != host.calendar_flow.calendar_id
                        or scope.get('calendar_read_all', False) != getattr(host.calendar_flow.adapter, 'read_all_enabled', False)
                        or scope.get('calendar_read_ids_hash') != calendar_read_ids_hash(host)):
                    return None
                return deepcopy(value)
            except (KeyError, TypeError, ValueError):
                return None
        output = data.get('tool_result', {})
        # A completed new read/selection or a business workflow replaces read
        # focus. Plain chat and failed reads can preserve the pending question.
        if (output.get('complete') or output.get('query_followup_result') and output.get('ok')
                or receipt.result.status in {'preview', 'recorded', 'created'}):
            return None
    return None


def context(host):
    value = current(host)
    if value is None:
        return {'pending_query': None}
    return {'pending_query': {
        'clarification_ref': value['ref'], 'original_question': value['question'],
        'goal': value['goal'], 'start': value['scope']['start'], 'end': value['scope']['end'],
        'activity': value['scope']['filter_text'], 'confirmed_count': len(value['confirmed']),
        'excluded_count': len(value.get('excluded', [])),
        'unconfirmed_count': value['unconfirmed_count'],
        'candidates': [{k: deepcopy(v) for k, v in row.items() if k != 'read_ref'} for row in value['candidates']]}}


def annotate(host, result):
    """Persist only candidates the grounded response actually presents."""
    if result.data.get('tool_result', {}).get('query_followup_result'):
        return result
    output = result.data.get('tool_result', {})
    reads = getattr(host, '_query_reads', {})
    rows = [reads[ref] for ref in output.get('query_refs', []) if ref in reads] if output.get('query_analysis') else [output]
    calendars = [r for r in rows if r.get('domain') == 'calendar' and r.get('source_complete')
                 and r.get('scope', {}).get('filter_text') and r.get('calendar_selection', {}).get('uncertain_count')]
    if len(calendars) != 1:
        return result
    row = calendars[0]
    candidates = row.get('uncertain_events', [])
    confirmed = row.get('analysis_records', row.get('events', []))
    if (not candidates or any(not e.get('read_ref') for e in candidates + confirmed)
            or len(confirmed) != row['count']):
        return result
    scope = row['scope']
    candidates = [{**deepcopy(e), 'candidate_ref': 'candidate_' + digest(e)[:20]} for e in candidates]
    now = datetime.fromisoformat(host._current['at'])
    pending = {'format': 'activity-read-followup-v1', 'owner': host.settings.owner_id,
               'ref': 'follow_' + digest([host.settings.owner_id, host._active_event_id, scope, candidates])[:20],
               'question': host._current['text'], 'goal': 'expense' if any(r.get('domain') == 'expense' for r in rows) else 'activity',
               'scope': deepcopy(scope), 'candidates': candidates, 'confirmed': deepcopy(confirmed),
               'unconfirmed_count': row['calendar_selection']['uncertain_count'],
               'expires_at': (now + timedelta(hours=24)).isoformat()}
    message = result.message
    if output.get('query_analysis'):
        from .read_queries import result as render_query
        # An uncertain activity needs a candidate choice, not the generic date
        # error from an attempted downstream expense read.
        kept = [r for r in rows if not (r.get('domain') == 'expense' and r.get('error_code') in
                {'invalid_arguments', 'date_required', 'incomplete_calendar_basis'})]
        message = '\n\n'.join(render_query(r, now).message for r in kept)
    from .read_queries import label_text, result as render_query
    prompt = '\n\nนัดไหนเป็น' + label_text(scope['filter_text']) + 'ครับ? บอกวันที่หรือชื่อนัดได้เลย'
    if pending['goal'] == 'expense':
        prompt += ' แล้วผมจะรวมรายจ่ายของวันที่ยืนยันให้ครับ'
    message += prompt
    if len(message.encode('utf-16-le')) > 9000:
        message = render_query(row, now).message + prompt
    return Result(result.status, message, {**result.data, 'read_followup': pending})


def repair(host, output):
    """Offer one normal read-loop correction; duplicate-call/read guards still apply."""
    if (output.get('status') == 'stored_query' and output.get('error_code') in
            {'date_required', 'invalid_arguments', 'invalid_calendar_refs', 'incomplete_calendar_basis', 'activity_selection_required'}
            and current(host) is not None):
        scope = {k: deepcopy(v) for k, v in output.get('scope', {}).items()
                 if k not in {'workbook', 'calendar', 'calendar_read_ids_hash'}}
        return {**output, 'scope': scope, **context(host), 'continue_model': True}
    return None


def clarification(host):
    return render({'query_followup_result': True, 'ok': False, 'read_only': True,
                   'error_code': 'invalid_selection', 'pending': current(host)})


def execute(host, args):
    from .read_queries import execute as expenses, calendar_read_ids_hash
    pending = current(host)
    def fail(code):
        return {'query_followup_result': True, 'ok': False, 'read_only': True,
                'error_code': code, 'pending': pending}
    try:
        p = Resolve.model_validate(args)
        host._owner_sources(p.source_refs)
        if (not pending or p.clarification_ref != pending['ref']
                or host._current['source_ref'] not in p.source_refs
                or p.selection_text.strip() != p.selection_text
                or p.selection_text not in host._current['text']
                or len(set(p.candidate_refs)) != len(p.candidate_refs)):
            raise ValueError()
        by_ref = {e['candidate_ref']: e for e in pending['candidates']}
        chosen = [by_ref[ref] for ref in p.candidate_refs]
        # Ground the selected identities against explicit date evidence. This
        # validates a model-selected reference; it does not route owner intent.
        from .history_dates import cues, answer_text
        stamp = datetime.fromisoformat(host._current['at'])
        parts, _, issue = cues(answer_text(p.selection_text) or p.selection_text, stamp)
        if parts.get('day') and not issue:
            for item in chosen:
                first = date.fromisoformat(item['start']) if item['all_day'] else datetime.fromisoformat(item['start']).astimezone(BANGKOK).date()
                last = date.fromisoformat(item['end']) - timedelta(days=1) if item['all_day'] else (datetime.fromisoformat(item['end']).astimezone(BANGKOK) - timedelta(microseconds=1)).date()
                day = max(first, date.fromisoformat(pending['scope']['start']))
                last = min(last, date.fromisoformat(pending['scope']['end']))
                supported = False
                while day <= last:
                    supported |= all(getattr(day, key) == value for key, value in parts.items())
                    day += timedelta(days=1)
                if not supported: raise ValueError('selection_date_mismatch')
    except (ValueError, TypeError, KeyError):
        return fail('invalid_selection')
    if not host.calendar_flow.enabled:
        return fail('calendar_disabled')
    scope = pending['scope']
    start, end = date.fromisoformat(scope['start']), date.fromisoformat(scope['end'])
    try:
        fresh = host.calendar_flow.adapter.saved_events(datetime.combine(start, time.min, BANGKOK),
                    datetime.combine(end + timedelta(days=1), time.min, BANGKOK), include_refs=True)
    except (ApiError, NotReady, OutcomeUnknown):
        return fail('calendar_unavailable')
    except Exception:
        return fail('calendar_unavailable')
    checked = {e['read_ref']: e for e in pending['confirmed'] + chosen}
    fresh_by_ref = {e['read_ref']: e for e in fresh}
    fields = ('title', 'start', 'end', 'all_day', 'read_ref')
    if any(ref not in fresh_by_ref or any(e[k] != fresh_by_ref[ref][k] for k in fields) for ref, e in checked.items()):
        return fail('calendar_changed')
    if p.decision == 'exclude':
        pending['candidates'] = [e for e in pending['candidates'] if e['candidate_ref'] not in p.candidate_refs]
        pending['unconfirmed_count'] -= len(chosen)
        pending['excluded'] = pending.get('excluded', []) + [{k: e[k] for k in fields} for e in chosen]
        pending['ref'] = 'follow_' + digest([host._active_event_id, pending['ref'], p.candidate_refs, p.decision])[:20]
        return {'query_followup_result': True, 'ok': True, 'complete': False, 'status': 'selection_updated',
                'read_only': True, 'pending': pending, 'excluded': chosen}
    confirmed = checked
    # Owner-confirmed membership is complete only for this explicit subset.
    # Its extent is derived from stored events, never a model-provided ISO date.
    def first(e):
        return e['start'] if e['all_day'] else datetime.fromisoformat(e['start']).astimezone(BANGKOK).date().isoformat()
    def last(e):
        return (date.fromisoformat(e['end']) - timedelta(days=1)).isoformat() if e['all_day'] else (datetime.fromisoformat(e['end']).astimezone(BANGKOK) - timedelta(microseconds=1)).date().isoformat()
    a = max(scope['start'], min(first(e) for e in confirmed.values()))
    b = min(scope['end'], max(last(e) for e in confirmed.values()))
    ref = 'read_' + digest([host._active_event_id, pending['ref'], p.candidate_refs])[:20]
    host._query_reads[ref] = {'domain': 'calendar', 'complete': True, 'count': len(confirmed),
        'scope': {**scope, 'start': a, 'end': b}, 'analysis_records': list(confirmed.values())}
    output = expenses(host, 'query_expenses', {'start_date': None, 'end_date': None, 'date_mode': 'previous',
        'relative_offset': None, 'period_text': None, 'source_refs': p.source_refs,
        'filter': None, 'measure': 'expense', 'view': 'total', 'calendar_query_refs': [ref]})
    if not output['complete']:
        return {**fail('expense_unavailable'), 'expense_error': output.get('error_code')}
    remaining = [e for e in pending['candidates'] if e['candidate_ref'] not in p.candidate_refs]
    pending.update(candidates=remaining, confirmed=[{k: e[k] for k in fields} for e in confirmed.values()],
                   unconfirmed_count=pending['unconfirmed_count'] - len(chosen))
    pending['ref'] = 'follow_' + digest([host._active_event_id, pending['ref'], p.candidate_refs])[:20]
    output['scope']['calendar_basis'].update(coverage='confirmed_dates_only',
            original_start=scope['start'], original_end=scope['end'], unconfirmed_count=pending['unconfirmed_count'])
    return {**output, 'query_followup_result': True, 'pending': pending, 'at': host._current['at']}


def render(output):
    from .read_queries import result as render_query, calendar_rows
    messages = {
        'invalid_selection': 'ยังจับคู่คำตอบกับนัดที่รอยืนยันไม่ได้ครับ โปรดเลือกวันที่หรือชื่อนัดจากรายการที่แสดง',
        'calendar_disabled': 'ตอนนี้ยังอ่านปฏิทินไม่ได้ครับ จึงยังตรวจนัดที่ยืนยันไม่ได้',
        'calendar_unavailable': 'อ่านปฏิทินเพื่อตรวจนัดที่ยืนยันไม่สำเร็จครับ จึงยังรวมรายจ่ายไม่ได้',
        'calendar_changed': 'นัดที่เลือกถูกเปลี่ยนหรือลบไปแล้วครับ โปรดค้นนัดในช่วงเดิมใหม่ก่อนรวมรายจ่าย',
        'expense_unavailable': 'ตรวจนัดที่ยืนยันแล้ว แต่อ่านรายจ่ายไม่ครบครับ จึงยังสรุปยอดไม่ได้'}
    pending = output.get('pending')
    if output.get('status') == 'selection_updated':
        message = 'ไม่นับนัดที่คุณระบุว่าไม่เข้าข่ายครับ:\n' + '\n'.join(calendar_rows(output['excluded'], pending['scope']))
        if pending['unconfirmed_count']:
            message += f"\nอีก {pending['unconfirmed_count']} นัดยังไม่ได้รับการยืนยัน จึงยังไม่นำมารวมรายจ่ายครับ"
            if pending['candidates']:
                message += '\nนัดต่อไปนี้เข้าข่ายหรือไม่ครับ?\n' + '\n'.join(calendar_rows(pending['candidates'], pending['scope']))
        else:
            message += '\nไม่มีนัดรอยืนยันเหลือแล้วครับ'
        data = {'tool_result': {k: v for k, v in output.items() if k != 'pending'}}
        status = 'clarification' if pending['unconfirmed_count'] else 'answer'
    elif output['ok']:
        safe = {k: v for k, v in output.items() if k != 'pending'}
        result = render_query(safe, datetime.fromisoformat(output['at']))
        message = 'ยอดเฉพาะวันที่ยืนยัน:\n' + result.message
        data = result.data
        if pending['unconfirmed_count']:
            message += f"\nยังไม่รวมอีก {pending['unconfirmed_count']} นัดที่ยังไม่ยืนยัน จึงยังไม่ใช่ยอดครบทุกวันของกิจกรรมในช่วงเดิมครับ"
            if pending['candidates']:
                message += '\nนัดที่ยังรอยืนยัน:\n' + '\n'.join(calendar_rows(pending['candidates'], pending['scope']))
            else:
                message += '\nต้องค้นปฏิทินช่วงเดิมเพิ่มเพื่อแสดงนัดที่เหลือก่อนยืนยันครับ'
        status = result.status
    else:
        message = messages[output['error_code']]
        data = {'tool_result': {k: v for k, v in output.items() if k != 'pending'}}
        status = 'clarification'
        if pending and pending['candidates']:
            message += '\nนัดที่รอยืนยัน:\n' + '\n'.join(calendar_rows(pending['candidates'], pending['scope']))
    return Result(status, message, {**data, 'query_analysis': True, 'read_only': True,
        'financial_write_authorized': False, 'calendar_write_authorized': False,
        **({'read_followup': pending} if pending and pending['unconfirmed_count'] else {})})
