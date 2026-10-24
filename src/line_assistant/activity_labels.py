"""Owner-stated title → activity decisions reused by later Calendar activity reads.

A decision is remembered only from an owner answer covering every shown
occurrence of a title, or from an explicit remember/correct/forget request that
names both the title and the activity. Labels apply by exact normalized title
and activity, are disclosed in every answer that uses them and never touch
Calendar. Runtimes before this key ignore it: a rollback forgets the labels and
the next read asks again instead of rejecting the owner state.
"""
from copy import deepcopy
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .catalog import normalized
from .history_context import digest
from .model_api import object_schema
from .state import Result
from .tool_errors import recovery

FORMAT = 'activity-labels-v1'
MAX_LABELS = 200
MAX_HINTS = 20
NAME = 'update_activity_label'
DECISIONS = ('match', 'exclude')


class Update(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=200)
    activity: str = Field(min_length=1, max_length=80)
    decision: Literal['match', 'exclude', 'forget']
    source_refs: list[str] = Field(min_length=1, max_length=16)


def key(activity, title):
    return digest([normalized(activity), normalized(title)])[:24]


def enabled(host):
    return bool(getattr(host, 'activity_memory_enabled', False))


def lookup(host, activity, title):
    if not enabled(host):
        return None
    row = getattr(host.state, 'activity_labels', {}).get(key(activity, title))
    return row['decision'] if row else None


def storable(activity, title):
    """The restore check's bounds: a label it would reject is never written."""
    return 1 <= len(title) <= 200 and 1 <= len(activity) <= 80 and bool(normalized(title)) and bool(normalized(activity))


def hints(host, activity, titles):
    """Labels for these titles stated under other activity wording, for the classifier to weigh."""
    if not enabled(host):
        return []
    wanted = {normalized(t) for t in titles}
    rows = [r for r in getattr(host.state, 'activity_labels', {}).values()
            if normalized(r['title']) in wanted and normalized(r['activity']) != normalized(activity)]
    return [{k: r[k] for k in ('title', 'activity', 'decision')} for r in sorted(rows, key=lambda r: r['at'])[-MAX_HINTS:]]


def remember(host, activity, title, decision, source_ref):
    if not storable(activity, title) or not 1 <= len(source_ref) <= 64:
        return False
    labels = host.state.activity_labels
    labels[key(activity, title)] = {'activity': activity, 'title': title, 'decision': decision,
                                    'source_ref': source_ref, 'at': host._current['at']}
    # A bounded memory keeps the newest owner statements.
    while len(labels) > MAX_LABELS:
        labels.pop(min(labels, key=lambda k: labels[k]['at']))
    return True


def validate(value, owner):
    if (not isinstance(value, dict) or set(value) != {'format', 'owner', 'labels'} or value['format'] != FORMAT
            or value['owner'] != owner or not isinstance(value['labels'], dict) or len(value['labels']) > MAX_LABELS):
        raise ValueError('Invalid activity label state')
    for ref, row in value['labels'].items():
        if (not isinstance(row, dict) or set(row) != {'activity', 'title', 'decision', 'source_ref', 'at'}
                or any(not isinstance(row[k], str) for k in row) or not 1 <= len(row['title']) <= 200
                or not 1 <= len(row['activity']) <= 80 or row['decision'] not in DECISIONS
                or not 1 <= len(row['source_ref']) <= 64 or ref != key(row['activity'], row['title'])
                or datetime.fromisoformat(row['at']).utcoffset() is None):
            raise ValueError('Invalid activity label binding')


def snapshot(state):
    return {'format': FORMAT, 'owner': state.owner_id, 'labels': deepcopy(state.activity_labels)}


def definitions(host):
    if not enabled(host) or not host.calendar_flow.enabled:
        return []
    return [{'type': 'function', 'name': NAME, 'strict': True,
             'description': ('Remember, correct or forget the owner\'s own statement that an appointment title is or is not an activity. '
                             'Only when the CURRENT owner message explicitly asks to remember/forget or corrects a remembered label; '
                             'answering a pending_query uses resolve_activity_dates, which remembers whole-title answers itself. '
                             'Copy title and activity from the current owner message. Changes only this assistant memory, never Calendar.'),
             'parameters': object_schema({
                 'title': {'type': 'string', 'maxLength': 200}, 'activity': {'type': 'string', 'maxLength': 80},
                 'decision': {'type': 'string', 'enum': ['match', 'exclude', 'forget'],
                              'description': ('match: the owner says the title IS the activity. exclude: the owner says the title is NOT the activity. '
                                              'forget: the owner asks to stop remembering or undo a label (for example the ไม่ต้องจำ button) '
                                              'without saying it is not the activity; later reads ask again.')},
                 'source_refs': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 16}})}]


def context(host):
    if not enabled(host) or not getattr(host.state, 'activity_labels', None):
        return {}
    rows = sorted(host.state.activity_labels.values(), key=lambda r: r['at'])[-20:]
    return {'activity_labels': [{k: r[k] for k in ('title', 'activity', 'decision')} for r in rows]}


def execute(host, args):
    base = {'activity_label': True, 'read_only': True, 'financial_write_authorized': False, 'calendar_write_authorized': False}
    if not enabled(host):
        return {**base, 'ok': False, 'error_code': 'activity_memory_disabled'}
    try:
        p = Update.model_validate(args)
        host._owner_sources(p.source_refs)
        text = normalized(host._current['text'])
        if host._current['source_ref'] not in p.source_refs:
            raise ValueError('activity_label_source')
        # Both names come from the owner's own words; a model cannot invent a label.
        if any(not normalized(v) or normalized(v) not in text for v in (p.title, p.activity)):
            raise ValueError('activity_label_not_quoted')
    except (ValueError, TypeError, KeyError) as error:
        code = str(error) if str(error) in {'activity_label_source', 'activity_label_not_quoted'} else 'activity_label_invalid'
        return {**base, 'ok': False, 'error_code': code,
                'recovery': recovery(kind='assistant_arguments', fields=['title', 'activity'], tools=[NAME])}
    ref = key(p.activity, p.title)
    previous = host.state.activity_labels.get(ref)
    if p.decision == 'forget':
        if previous is None:
            return {**base, 'ok': False, 'error_code': 'activity_label_missing', 'title': p.title, 'activity': p.activity}
        host.state.activity_labels.pop(ref)
    else:
        remember(host, p.activity, previous['title'] if previous else p.title, p.decision, host._current['source_ref'])
    return {**base, 'ok': True, 'decision': p.decision, 'title': previous['title'] if previous else p.title,
            'activity': previous['activity'] if previous else p.activity}


def names(titles):
    """Bounded title list for one disclosure line."""
    from .read_queries import label_text
    return ', '.join(label_text(t) for t in titles[:3]) + (f' และอีก {len(titles) - 3} ชื่อ' if len(titles) > 3 else '')


def render(output):
    from .read_queries import label_text
    data = {'domain': 'activity_label', 'activity_label': deepcopy(output), 'read_only': True}
    if not output['ok']:
        messages = {'activity_memory_disabled': 'ยังไม่ได้เปิดการจำประเภทนัดครับ',
                    'activity_label_missing': 'ยังไม่ได้จำเรื่องนี้ไว้ครับ เลยไม่มีอะไรให้ลบ'}
        return Result('clarification', messages.get(output['error_code'], 'ยังจำไม่ได้ครับ ช่วยบอกชื่อนัดและประเภทนัดอีกครั้ง'), data)
    title, activity = label_text(output['title']), label_text(output['activity'])
    if output['decision'] == 'forget':
        message = f'ไม่จำแล้วครับ ครั้งหน้าถ้าเจอ {title} จะถามก่อนว่าเป็น{activity}ไหม'
    elif output['decision'] == 'match':
        message = f'จำไว้แล้วครับ ครั้งหน้าจะนับ {title} เป็น{activity}เลย'
    else:
        message = f'จำไว้แล้วครับ ครั้งหน้าจะไม่นับ {title} เป็น{activity}'
    return Result('answer', message, data)
