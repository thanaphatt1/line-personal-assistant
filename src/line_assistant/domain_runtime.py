"""Flagged local candidate. Reversible tools; only inherited runtime commits."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import re
import secrets
from pydantic import ValidationError
from .conversation import ConversationAssistant
from .conversation_evidence import command_text, category_choice, subcategory_choice, grounded_item, edited_item
from .conversation_mocks import ConversationSheets
from .conversation_ui import message, version
from .contracts import SpendingSummary
from .domain_tools import MODELS, Patch
from .domain_notes import apply_note, section, CLEAR, NoteError
from .intent_api import ContextDraft
from .language import BANGKOK, DIGITS, amounts, erase, intent_guard, read_request, expense_source
from .planner import PlannerNotice
from .state import Result
from .taxonomy import identifier
from .tool_loop import MAX_CALLS, step_key


class ToolError(ValueError):
    def __init__(self, code): self.code = code


def blank():
    return {'item': None, 'amount': None, 'date': None, 'note': '', 'kind': 'expense',
            'date_parts': {}, 'item_id': None, 'category_id': None, 'subcategory_id': None,
            'new_item': False, 'date_required': False}


def relative_parts(text, at):
    """Small explicit relative spans, independent of model month-name semantics."""
    current = at.astimezone(BANGKOK).date()
    cues = re.findall(r'เมื่อวานซืน|เมื่อวาน|วานนี้|วันนี้|เดือนก่อน|เดือนที่แล้ว|เดือนนี้|ปีที่แล้ว|ปีก่อน|ปีนี้', text)
    if len(cues) > 1: raise ToolError('ambiguous_date_source')
    if re.search(r'เมื่อวานซืน|เมื่อวาน|วานนี้|วันนี้', text):
        delta = -2 if 'เมื่อวานซืน' in text else -1 if re.search(r'เมื่อวาน|วานนี้', text) else 0
        value = current + timedelta(days=delta)
        return {'day': value.day, 'month': value.month, 'year': value.year}
    if re.search(r'เดือนก่อน|เดือนที่แล้ว|เดือนนี้', text):
        previous = 'เดือนนี้' not in text
        return {'month': (current.month - 2) % 12 + 1 if previous else current.month,
                'year': current.year - (previous and current.month == 1)}
    if re.search(r'ปีที่แล้ว|ปีก่อน|ปีนี้', text):
        return {'year': current.year - (0 if 'ปีนี้' in text else 1)}
    return None


def semantic_date(entry, patch, source, at, item_spans, *, relative=False):
    """LLM supplies semantic components; calendar/source presence are runtime checks.

    A span proves presence, not the semantics of a colloquial word. A wrong but
    valid month remains a proposal visible in the owner-confirmed final preview.
    """
    parts = dict(entry['date_parts'])
    if entry['date'] and not parts:
        old = date.fromisoformat(entry['date']); parts = {'day': old.day, 'month': old.month, 'year': old.year}
    values = {k: getattr(patch, k) for k in ('day', 'month', 'year') if getattr(patch, k) is not None}
    spans = []
    date_text = erase(source, item_spans) if relative else source
    numeric_day = re.search(r'วันที่\s*(\d{1,2})(?!\d)', date_text.translate(DIGITS))
    if numeric_day: spans.append(numeric_day.span())
    if values:
        witness = patch.date_source
        if not witness or witness not in source: raise ToolError('date_source_required')
        occurrences = list(re.finditer(re.escape(witness), source))
        if len(occurrences) != 1: raise ToolError('ambiguous_date_source')
        span = occurrences[0].span()
        if any(span[0] < end and span[1] > start for start, end in item_spans):
            raise ToolError('date_source_is_item')
        spans.append(span)
        evidence = witness.translate(DIGITS)
        numbers = [int(v) for v in re.findall(r'\d+', evidence)]
        numeric_date = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', evidence.strip())
        slash_date = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', evidence.strip())
        expected = None
        if numeric_date: expected = dict(zip(('year', 'month', 'day'), map(int, numeric_date.groups())))
        elif slash_date: expected = dict(zip(('day', 'month', 'year'), map(int, slash_date.groups())))
        elif relative: expected = relative_parts(evidence, at)
        for key, value in values.items():
            normalized = value - 543 if key == 'year' and value >= 2400 else value
            if expected and key in expected:
                actual = expected[key] - 543 if key == 'year' and expected[key] >= 2400 else expected[key]
                if normalized != actual: raise ToolError('numeric_date_mismatch')
            elif key in {'day', 'year'}:
                if not any((n - 543 if key == 'year' and n >= 2400 else n) == normalized for n in numbers):
                    raise ToolError('unbacked_date_number')
            elif key == 'month':
                explicit_month = re.search(r'เดือน\s*(\d{1,2})(?!\d)', evidence)
                if explicit_month and int(explicit_month[1]) != value: raise ToolError('numeric_date_mismatch')
                if relative and expected and set(expected) == {'year'} and not re.sub(r'ปี(?:ที่แล้ว|ก่อน|นี้)|วันที่|\d|[\s/.-]', '', evidence):
                    raise ToolError('month_not_supplied')
                lexical = re.sub(r'วันที่|เดือน|ปี|พ\.?ศ\.?|ค\.?ศ\.?|\d|[\s/.-]', '', evidence)
                if not lexical and not explicit_month and not (parts.get('day') and source.strip() == evidence.strip()
                                        and evidence.strip().isdigit() and numbers == [value]):
                    raise ToolError('month_not_supplied')
            parts[key] = normalized
        if relative and expected and 'day' in expected and parts.get('day') and parts.get('month'):
            proposed = {**parts, 'year': parts.get('year', at.astimezone(BANGKOK).year)}
            if any(proposed[k] != v for k, v in expected.items()): raise ToolError('relative_date_incomplete')
    elif numeric_day and not entry['date']:
        # A dropped semantic component cannot turn a source day-only date into today.
        parts.setdefault('day', int(numeric_day[1]))
    if not values and not parts and not entry['date']:
        if (re.search(r'วันที่|เดือน|\d{4}-\d{2}-\d{2}|\d+/\d+', date_text)
                or (relative and relative_parts(date_text, at))):
            entry.update(date=None, date_required=True); return spans, None
        entry.update(date=at.astimezone(BANGKOK).date().isoformat(), date_required=False); return spans, None
    if not values and entry['date']:
        return spans, None
    entry.update(date_parts=parts, date=None, date_required=True)
    if (any(k in parts and not low <= parts[k] <= high for k, low, high in
            [('day', 1, 31), ('month', 1, 12), ('year', 1900, 2100)])):
        return spans, 'invalid_or_future_date'
    if not (parts.get('day') and parts.get('month')): return spans, None
    try:
        year = parts.get('year', at.astimezone(BANGKOK).year)
        if not 1900 <= year <= 2100: raise ValueError()
        value = date(year, parts['month'], parts['day'])
        if value > at.astimezone(BANGKOK).date(): raise ValueError()
    except ValueError: return spans, 'invalid_or_future_date'
    entry.update(date=value.isoformat(), date_parts={}, date_required=False)
    return spans, None


class ToolLoopAssistant(ConversationAssistant):
    relative_dates = False
    def __init__(self, *args, candidate_enabled=False, **kwargs):
        if candidate_enabled is not True: raise ValueError('Candidate flag must be explicit')
        super().__init__(*args, **kwargs)
        if not (isinstance(self.sheets, ConversationSheets) or getattr(getattr(self.sheets, 'client', None), 'mocked', False)):
            raise ValueError('Candidate effects must be mocked')
        if hasattr(self.state, 'client') and not getattr(self.state.client, 'mocked', False):
            raise ValueError('Candidate durable state must be mocked')
        self.loop = None
        self._search_sets = {}  # Current owner turn only; never a durable grant.

    def _entry_ids(self, pending):
        ids = pending.payload.get('entry_ids')
        if ids is None:
            ids = [identifier('entry', pending.origin_event_id + ':' + str(i))
                   for i in range(len(pending.payload['entries']))]
            pending.payload['entry_ids'] = ids
        return ids

    def loop_fallback(self, status):
        pending = self.state.pending
        if pending and pending.kind == 'conversation':
            text = 'ยังแปลคำสั่งรอบนี้ไม่ได้ ร่างเดิมยังอยู่ กรุณาระบุข้อมูลที่ต้องการแก้'
        elif pending and pending.kind == 'tool_source':
            text = 'ยังสร้างร่างไม่ได้ ไม่มีข้อมูลถูกบันทึก เก็บข้อความต้นทางไว้ชั่วคราว กรุณาระบุข้อมูลเพิ่ม หรือพิมพ์ ยกเลิก'
        else:
            text = 'ยังสร้างร่างไม่ได้ ไม่มีข้อมูลถูกบันทึก กรุณาส่งรายการและจำนวนเงินใหม่'
        return Result(status, text)

    @staticmethod
    def _no_draft():
        return Result('no_pending', 'ไม่มีร่างที่รออยู่ หรือร่างหมดอายุแล้ว กรุณาส่งรายการและจำนวนเงินใหม่')

    def _receipt(self, event_id, fingerprint, result):
        self._search_sets = {}
        for step in range(MAX_CALLS):
            run = self.state.model_runs.get(step_key(event_id, step))
            if run and run.status == 'completed': run.decision = None
        return super()._receipt(event_id, fingerprint, result)

    def loop_context(self, taxonomy):
        pending = self.state.pending
        safe = super()._context(taxonomy, '')['pending']
        if safe: safe = ContextDraft.model_validate(safe).model_dump(mode='json')
        control = pending.payload.get('loop_context', {}) if pending else {}
        return {'pending': safe, 'awaiting': deepcopy(control.get('awaiting')),
                'latest_question': control.get('question', ''),
                'recent': [{'role': t['role'], 'text': t['text']} for t in self.state.recent_turns],
                'entry_ids': self._entry_ids(pending) if safe else [],
                'source_context': {'text': pending.payload['source'], 'bangkok_date': pending.payload['bangkok_date']}
                    if pending and pending.kind == 'tool_source' else None}

    def _route(self, text, event_id, stamp, occurred_at):
        self._search_sets = {}
        if self.loop is not None: self.loop.trace = []
        self._migrate(); taxonomy = self._taxonomy()
        if self._active_event_kind == 'postback': return self._button(text, event_id, stamp, taxonomy)
        pending = self.state.pending
        if pending and pending.kind == 'conversation' and pending.payload.get('loop_event') == event_id and stamp == pending.source_timestamp:
            # Interrupted reversible tools may have saved a draft but no receipt.
            # Show its validated current state; never retry a paid step or commit.
            return self._show(pending, taxonomy)
        if pending and stamp <= pending.source_timestamp:
            return Result('stale', 'คำตอบนี้เกิดก่อนคำถามล่าสุด กรุณาตอบคำถามล่าสุด')
        blocked = self._blocked_action()
        if blocked: return self._uncertain(blocked)
        # Explicit trailing note contents are data, including quotes/negation;
        # they do not cancel or authorize the surrounding expense command.
        guard = intent_guard(command_text(text))
        if guard:
            if guard == 'negated': self.state.pending = None
            return self._remember(text, Result('no_action', 'ไม่มีการบันทึกข้อความนี้'))
        if re.fullmatch(r'(?:ยกเลิก(?:รายการ(?:นี้|ที่รอ)?)?|cancel)(?:ครับ|ค่ะ)?', text, re.I):
            self.state.pending = None; return self._remember(text, Result('cancelled', 'ยกเลิกรายการที่รอแล้ว'))
        if re.fullmatch(self.confirmation, text, re.I):
            result = self._confirm(pending, taxonomy, stamp) if pending and pending.kind == 'conversation' else self._no_draft()
            return self._remember(text, result)
        commands = [command_text(line) for line in text.splitlines()]
        if re.search(r'(?:แก้|เปลี่ยน).*(?:ที่บันทึกแล้ว|บันทึกไปแล้ว|รายการล่าสุด|รายจ่ายล่าสุด)', command_text(text)):
            return Result('unsupported', 'ยังแก้รายจ่ายที่บันทึกแล้วไม่ได้ กรุณาตรวจสอบในชีต')
        if not pending and (re.match(r'^(?:แก้|เปลี่ยน)', command_text(text)) or CLEAR.fullmatch(text.strip())):
            return self._no_draft()
        if any(re.search(r'รายรับ|รับเงิน|เงินเดือน|เงินเข้า|\bincome\b', command, re.I) for command in commands):
            return Result('income_unconfigured', 'ยังไม่ได้ตั้งค่ารายรับ จึงยังไม่บันทึกรายการนี้')
        if any(re.match(r'^\s*(?:(?:ช่วย|ขอ|กรุณา|please)\s*)*(?:นัด|เตือน|ตารางวันนี้|วันนี้มีอะไร|วันนี้มีนัด)', command, re.I)
               for command in commands):
            return Result('unsupported', 'Calendar ยังไม่ได้เปิดใช้งานในรุ่นรายจ่ายนี้')
        if pending and pending.kind == 'conversation' and 'loop_context' not in pending.payload:
            # Import an existing v01 draft, retaining facts but asking for fresh review.
            pending.token = secrets.token_hex(4); pending.payload.pop('preview_digest', None)
            self._show(pending, taxonomy)
        if pending and pending.kind == 'conversation' and re.match(r'^(?:แก้|เปลี่ยน)', command_text(text)):
            pending.payload.update(ready=False, editing=True)
            pending.payload.pop('preview_digest', None)
            pending.source_timestamp = stamp; pending.token = secrets.token_hex(4)
            self._show(pending, taxonomy)
        if self.loop is None: return Result('model_unavailable', 'ยังไม่ได้ตั้งค่าตัวแปลคำสั่งจำลอง')
        if pending and pending.kind == 'tool_source': pending.source_timestamp = stamp
        # A failed initial interpretation retains the authenticated owner source
        # inside the existing workspace Pending TTL, not in lossy dialog history.
        # It is explicitly not a business draft and cannot be confirmed.
        source_draft = expense_source(command_text(text), occurred_at, taxonomy.catalog) if not pending else None
        if source_draft and all(e['item'] and e['amount'] is not None for e in source_draft.entries):
            self._pending('tool_source', event_id, stamp, {'source': text, 'source_at': occurred_at.isoformat(),
                'bangkok_date': occurred_at.astimezone(BANGKOK).date().isoformat()})
            self.state.checkpoint()
        result = self.loop.run(self, text, event_id, stamp, occurred_at, taxonomy)
        return self._remember(text, result)

    def _show(self, pending, taxonomy, prefix=''):
        if pending.kind != 'conversation': return self._no_draft()
        result = super()._show(pending, taxonomy, prefix)
        stage = result.data['stage']; index = pending.payload.get('focus_index', 0)
        if stage == 'date':
            parts = pending.payload['entries'][index]['date_parts']
            field = 'month' if parts.get('day') and not parts.get('month') else 'date'
        else: field = stage
        pending.payload['loop_context'] = {'awaiting': {'field': field, 'entry_id': self._entry_ids(pending)[index]},
                                          'question': result.message[:200]}
        actions = result.data['line_message']['contents']['footer']['contents']
        choices = [(b['action']['label'], *b['action']['data'].split('|')[3:]) for b in actions]
        if stage == 'category':
            all_categories = list(taxonomy.categories.items())
            page = pending.payload.get('category_page', 0)
            if type(page) is not int or not 0 <= page < max(1, (len(all_categories) + 7) // 8): page = 0
            pending.payload['category_page'] = page
            choices = [(label, 'category', key) for key, label in all_categories[page * 8:(page + 1) * 8]]
            if len(all_categories) > 8:
                choices.append(('หมวดเพิ่มเติม' if (page + 1) * 8 < len(all_categories) else 'กลับหมวดแรก',
                                'category_page', str(page + 1 if (page + 1) * 8 < len(all_categories) else 0)))
            choices.append(('ยกเลิก', 'cancel', ''))
            result = Result(result.status, result.message + '\nพิมพ์ชื่อหมวดที่ต้องการได้ หรือกด หมวดเพิ่มเติม', result.data)
            pending.payload['loop_context']['question'] = result.message[:200]
        pending.payload['allowed'] = [[op, value] for _, op, value in choices]
        # Regenerate after control metadata changes, so every button binds current version.
        result.data['line_message'] = message(result.message, pending, choices)
        return result

    def _button(self, text, event_id, stamp, taxonomy):
        parts = text.split('|'); pending = self.state.pending
        if pending and pending.kind != 'conversation':
            return Result('stale', 'ไม่มีร่างที่ใช้ปุ่มนี้ได้ กรุณาส่งรายการใหม่')
        if len(parts) == 5 and parts[3] == 'category_page':
            if (not pending or parts[0] != 'v1' or parts[1] != pending.token or parts[2] != version(pending)
                    or parts[3:] not in pending.payload['allowed'] or stamp <= pending.source_timestamp):
                return Result('stale', 'ปุ่มนี้หมดอายุหรือรายการเปลี่ยนแล้ว กรุณาตอบคำถามล่าสุด')
            pending.payload['category_page'] = int(parts[4]); pending.source_timestamp = stamp
            pending.token = secrets.token_hex(4); return self._show(pending, taxonomy)
        return super()._button(text, event_id, stamp, taxonomy)

    def domain_call(self, call, text, stamp, at, taxonomy):
        try:
            args = MODELS[call['name']].model_validate(json.loads(call['arguments']))
            if call['name'] in {'catalog_search', 'category_search'}:
                return self._search(call['name'], args, taxonomy), None
            if call['name'] == 'draft_edit':
                outcome = self._edit(args, text, stamp, at, taxonomy)
                # Review/questions are entirely runtime-owned; another paid
                # preview request supplies no new information or authorization.
                return outcome, self._show(self.state.pending, taxonomy)
            if call['name'] == 'preview_draft':
                if not self.state.pending or self.state.pending.kind != 'conversation':
                    return {'ok': False, 'error': {'code': 'no_active_draft'}}, self._no_draft()
                result = self._show(self.state.pending, taxonomy)
                return {'ok': True, 'status': result.status}, result
            if call['name'] == 'summary_query':
                if not re.search(r'สรุป|ยอดรายจ่าย|\bsummary\b', command_text(text), re.I):
                    raise ToolError('summary_not_requested')
                plan = SpendingSummary.model_validate(args.model_dump())
                grounded = read_request(text, at)
                if isinstance(grounded, PlannerNotice): raise ToolError('summary_not_requested')
                if grounded and grounded.validated_arguments() != plan: raise ToolError('summary_range_mismatch')
                totals = self.sheets.summary(plan.start_date, plan.end_date)
                result = Result('summary', f"รายจ่าย {totals['expense']} บาท ({self.expense_label})", totals)
                return {'ok': True, 'status': 'summary'}, result
        except (ToolError, NoteError) as error:
            outcome = {'ok': False, 'error': {'code': error.code}, 'awaiting': self.loop_context(taxonomy)['awaiting']}
            return outcome, self._no_draft() if error.code == 'no_active_draft' else None
        except (ValidationError, ValueError, TypeError, KeyError):
            return {'ok': False, 'error': {'code': 'invalid_arguments'}}, None
        except Exception:
            return {'ok': False, 'error': {'code': 'tool_unavailable'}}, None

    def _search(self, name, args, taxonomy):
        from .catalog import normalized
        query = normalized(args.query)
        if name == 'catalog_search':
            values = [{'id': key, 'label': row.item} for key, row in taxonomy.items.items()
                      if not query or query in normalized(row.item)]
            size = 5
        else:
            values = [{'id': key, 'label': label, 'subcategories': [{'id': k, 'label': v} for k, v in list(taxonomy.pairs[key].items())[:8]]}
                      for key, label in taxonomy.categories.items() if not query or query in normalized(label)]
            size = 8
        page = values[args.offset:args.offset + size]
        snapshot = identifier('set', json.dumps([[r.item, r.category, r.subcategory] for r in taxonomy.rows], ensure_ascii=False))
        result_id = identifier('result', snapshot + name + query + str(args.offset))
        self._search_sets[name] = {'id': result_id, 'snapshot': snapshot, 'matches': deepcopy(page)}
        return {'ok': True, 'result_set_id': result_id, 'matches': page,
                'next_offset': args.offset + size if args.offset + size < len(values) else None}

    def _selection(self, patch, taxonomy):
        updates = {}
        for field, tool in [('item_id', 'catalog_search'), ('category_id', 'category_search'), ('subcategory_id', 'category_search')]:
            key = getattr(patch, field)
            if key is None: continue
            saved = self._search_sets.get(tool)
            if not saved or saved['id'] != patch.result_set_id: raise ToolError('unknown_result_set')
            # Refresh before accepting an ID: same label/ID with changed mapping
            # is stale too. A hash identifies data; it never authorizes an effect.
            fresh = self._taxonomy()
            snapshot = identifier('set', json.dumps([[r.item, r.category, r.subcategory] for r in fresh.rows], ensure_ascii=False))
            if snapshot != saved['snapshot']: raise ToolError('search_results_changed')
            values = saved['matches']
            if field == 'subcategory_id':
                category = patch.category_id or (self.state.pending.payload['entries'][self._entry_ids(self.state.pending).index(patch.entry_id)]['category_id']
                    if self.state.pending and self.state.pending.kind == 'conversation' and patch.entry_id in self._entry_ids(self.state.pending) else None)
                values = [v for row in values if row['id'] == category for v in row.get('subcategories', [])]
            row = next((v for v in values if v['id'] == key), None)
            if not row: raise ToolError('unknown_result_id')
            target = {'item_id': 'item', 'category_id': 'category', 'subcategory_id': 'subcategory'}[field]
            if getattr(patch, target) is not None and getattr(patch, target) != row['label']:
                raise ToolError('result_label_mismatch')
            updates[target] = row['label']
        return patch.model_copy(update=updates)

    def _edit(self, args, text, stamp, at, taxonomy):
        pending = self.state.pending
        retained = pending if pending and pending.kind == 'tool_source' else None
        if args.mode == 'create' and pending and not retained: raise ToolError('active_draft_exists')
        if args.mode == 'update' and (not pending or retained): raise ToolError('no_active_draft')
        if args.mode == 'update':
            # Failed edits cannot leave a previous approval button usable.
            pending.payload.update(ready=False, editing=True)
            pending.payload.pop('preview_digest', None)
            pending.source_timestamp = stamp; pending.token = secrets.token_hex(4)
        creating = args.mode == 'create'
        if not creating and len({p.entry_id for p in args.entries}) != len(args.entries): raise ToolError('duplicate_target')
        payload = deepcopy(pending.payload) if not creating else {'version': 2, 'entries': [blank() for _ in args.entries],
            'raw_items': [''] * len(args.entries), 'ready': False, 'allowed': [], 'category_proposals': {}, 'new_name_confirmed': []}
        if creating and any(p.entry_id is not None for p in args.entries): raise ToolError('invalid_target')
        ids = self._entry_ids(pending) if not creating else [identifier('entry', (retained.origin_event_id if retained else self._active_event_id) + ':' + str(i)) for i in range(len(args.entries))]
        payload['entry_ids'] = ids
        date_error = None; warnings = []
        for ordinal, patch in enumerate(args.entries):
            if not creating and patch.entry_id not in ids: raise ToolError('invalid_target')
            index = ordinal if creating else ids.index(patch.entry_id)
            patch = self._selection(patch, taxonomy)
            entry = payload['entries'][index]
            recovering = bool(retained and retained.origin_event_id != self._active_event_id)
            source = retained.payload['source'] if retained else text
            source_at = datetime.fromisoformat(retained.payload['source_at']) if retained else at
            if creating and len(args.entries) > 1:
                lines = source.splitlines()
                if len(lines) != len(args.entries): raise ToolError('one_item_per_line')
                source = lines[index]
            if not creating and len(payload['entries']) > 1:
                named = [i for i, e in enumerate(payload['entries']) if e['item'] and e['item'] in command_text(source)]
                if named != [index]: raise ToolError('ambiguous_target')
            command = command_text(source); original = entry['item']
            if patch.item is not None:
                item_command = command
                if creating and patch.date_source and patch.date_source in command:
                    date_span = list(re.finditer(re.escape(patch.date_source), command))
                    item_matches = list(re.finditer(re.escape(patch.item), command))
                    if len(date_span) == 1 and not any(date_span[0].start() < m.end() and date_span[0].end() > m.start() for m in item_matches):
                        item_command = erase(command, [date_span[0].span()])
                name = grounded_item(item_command, patch.item, source_at, taxonomy) if creating else edited_item(command, patch.item)
                if not name.strip() or (name not in command and name != original): raise ToolError('unbacked_item')
                if name != original:
                    entry.update(item=name, item_id=None, category_id=None, subcategory_id=None, new_item=False)
                    payload['new_name_confirmed'] = [i for i in payload['new_name_confirmed'] if i != index]
                    payload['category_proposals'].pop(str(index), None)
            spans = [m.span() for m in re.finditer(re.escape(entry['item']), command)] if entry['item'] else []
            if recovering:
                # First ground original date at its own time. Then the current
                # follow-up may supply missing/replaced date parts only. We never
                # concatenate histories to manufacture a date_source span.
                from .language import dates
                evidence = dates(command, source_at)
                day = re.search(r'วันที่\s*(\d{1,2})(?!\d)', command.translate(DIGITS))
                seed = Patch(day=int(day[1]), date_source=day[0]) if day else Patch()
                date_spans, error = semantic_date(entry, seed, command, source_at, spans, relative=self.relative_dates)
                if evidence.value:
                    entry.update(date=evidence.value.isoformat(), date_parts={}, date_required=False)
                if any(getattr(patch, k) is not None for k in ('day', 'month', 'year')):
                    if patch.date_source and patch.date_source in command and patch.date_source not in command_text(text):
                        date_spans, error = semantic_date(entry, patch, command, source_at, spans, relative=self.relative_dates)
                    else:
                        values = {k: None for k in ('day', 'month', 'year') if getattr(patch, k) == entry['date_parts'].get(k)}
                        current_command = command_text(text)
                        current_items = [m.span() for m in re.finditer(re.escape(entry['item']), current_command)] if entry['item'] else []
                        _, error = semantic_date(entry, patch.model_copy(update=values), current_command, at, current_items, relative=self.relative_dates)
            else:
                date_spans, error = semantic_date(entry, patch, command, source_at, spans, relative=self.relative_dates)
            date_error = error or date_error
            if patch.amount is not None:
                found = amounts(command, (*spans, *date_spans))
                if len(found) != 1 or found[0].value is None or Decimal(found[0].value) != Decimal(patch.amount):
                    raise ToolError('unbacked_amount')
                entry['amount'] = found[0].value
            note_source = source
            if recovering:
                original_note = section(source, entry['item'])
                if original_note: entry['note'] = original_note[0]
                if section(text, entry['item']) or CLEAR.fullmatch(text.strip()): note_source = text
            entry['note'], warning = apply_note(patch.note, entry['note'], note_source, entry['item'], creating=creating and not recovering)
            if warning:
                warnings.append({'entry_id': ids[index], 'field': 'note', 'code': warning})
            if patch.category is not None:
                choices, explicit = category_choice(text if recovering else source, taxonomy, allow_bare=(not creating or recovering) and entry['item'] != command, item=entry['item'] or '')
                desired = taxonomy.category_matches(patch.category)
                if not explicit or len(choices) != 1 or choices != desired: raise ToolError('category_not_owner_choice')
                entry.update(category_id=choices[0], subcategory_id=None, new_item=True)
                if index not in payload['new_name_confirmed']: payload['new_name_confirmed'].append(index)
            if patch.subcategory is not None:
                choices, explicit = subcategory_choice(text if recovering else source, taxonomy, entry['category_id'])
                if not explicit or len(choices) != 1: raise ToolError('subcategory_not_owner_choice')
                entry['subcategory_id'] = choices[0]
            match = taxonomy.catalog.match(entry['item'] or '')
            if match.status == 'exact':
                entry.update(item=match.canonical, item_id=identifier('item', match.canonical), new_item=False, category_id=None, subcategory_id=None)
            if entry['category_id'] and len(taxonomy.pairs[entry['category_id']]) == 1:
                entry['subcategory_id'] = next(iter(taxonomy.pairs[entry['category_id']]))
            payload['raw_items'][index] = entry['item'] or ''
        payload.update(ready=False, editing=False); payload.pop('preview_digest', None)
        payload['loop_event'] = self._active_event_id
        if creating:
            pending = self._pending('conversation', retained.origin_event_id if retained else self._active_event_id, stamp, payload)
            if retained: pending.expires_at = min(pending.expires_at, retained.expires_at)
        else:
            pending.payload = payload; pending.source_timestamp = stamp; pending.token = secrets.token_hex(4)
        shown = self._show(pending, taxonomy)
        self.state.checkpoint()
        result = {'ok': not bool(date_error), 'status': 'draft_updated', 'entry_ids': self._entry_ids(pending),
                  'awaiting': pending.payload['loop_context']['awaiting']}
        if warnings: result['warnings'] = warnings
        if date_error: result['error'] = {'code': date_error}
        return result
