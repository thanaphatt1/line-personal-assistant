"""Opt-in LLM-first expense conversations; interpretation never grants write consent."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import re
import secrets

from .cloud_run import ExpenseAssistant
from .catalog import normalized
from .contracts import AddTransactions
from .conversation_dates import resolve
from .conversation_evidence import category_choice, subcategory_choice, command_text, grounded_item, edited_item, item_selection
from .conversation_ui import message, version
from .intent_api import Intent
from .language import amounts, dates, intent_guard, read_request, BANGKOK
from .planner import PlannerNotice
from .state import Result
from .taxonomy import Taxonomy, identifier


class ConversationAssistant(ExpenseAssistant):
    supports_postback = True
    confirmation = r'(?:ยืนยัน|บันทึก(?:เลย|ได้เลย)?|ตกลง(?:บันทึก)?|confirm|save|yes)(?:ครับ|ค่ะ)?'

    def _taxonomy(self):
        self.state.before_effect()
        taxonomy = self.sheets.taxonomy()
        self.catalog = taxonomy.catalog
        return taxonomy

    def _migrate(self):
        pending = self.state.pending
        if pending and pending.kind in {'item', 'new_item', 'expense_draft', 'expense_preview'}:
            entries = []
            for e in pending.payload['entries']:
                entries.append({**e, 'date_parts': {}, 'item_id': None, 'category_id': None,
                                'subcategory_id': None, 'new_item': False, 'date_required': not e['date']})
            pending.kind = 'conversation'
            pending.payload = {'version': 1, 'entries': entries, 'ready': False,
                               'raw_items': pending.payload.get('raw_items', [e['item'] or '' for e in entries]),
                               'allowed': []}
            pending.token = secrets.token_hex(4)
        if pending and pending.kind == 'conversation' and pending.payload.get('version') != 2:
            # Old drafts cannot prove whether a category came from the owner or model.
            proposals = {}
            for index, entry in enumerate(pending.payload['entries']):
                if entry.get('category_id') and not self.catalog.match(entry['item'] or '').canonical:
                    proposals[str(index)] = {'category_id': entry['category_id'], 'subcategory_id': entry.get('subcategory_id')}
                entry.update(category_id=None, subcategory_id=None, new_item=False)
            pending.payload.update(version=2, category_proposals=proposals, new_name_confirmed=[], ready=False, allowed=[])
            pending.payload.pop('preview_digest', None)
            pending.token = secrets.token_hex(4)

    def _context(self, taxonomy, text):
        pending = self.state.pending
        payload = pending.payload if pending and pending.kind == 'conversation' else None
        # Never send server tokens, event/workbook/owner IDs or authorization state.
        safe = {'entries': deepcopy(payload['entries']), 'ready': payload['ready'], 'editing': payload.get('editing', False)} if payload else None
        if safe:
            for entry in safe['entries']: entry['note'] = entry['note'][:300]
        choices = taxonomy.choices(text, [e['item'] or '' for e in payload['entries']] if payload else ())
        if payload:
            selected = [e['category_id'] for e in payload['entries'] if e['category_id'] in taxonomy.categories]
            for key in reversed(list(dict.fromkeys(selected))):
                choices['categories'] = [v for v in choices['categories'] if v['id'] != key]
                choices['categories'].insert(0, {'id': key, 'label': taxonomy.categories[key],
                    'subcategories': [{'id': k, 'label': v} for k,v in list(taxonomy.pairs[key].items())[:8]]})
            choices['categories'] = choices['categories'][:8]
        return {'pending': safe,
                'recent': [{'role': t['role'], 'text': t['text']} for t in self.state.recent_turns],
                'choices': choices}

    def _remember(self, text, result):
        expires = (self.clock() + timedelta(seconds=self.settings.pending_ttl_seconds)).isoformat()
        self.state.recent_turns.extend([{'role': 'user', 'text': text[:240], 'expires_at': expires},
                                       {'role': 'assistant', 'text': result.message[:200], 'expires_at': expires}])
        self.state.recent_turns = self.state.recent_turns[-6:]
        return result

    def _receipt(self, event_id, fingerprint, result):
        run = self.state.model_runs.get(event_id)
        if run and run.status == 'completed':
            # Receipt replay replaces the proposal cache atomically in the same
            # checkpoint. Keep its fingerprint/reservation/attempt proof.
            run.decision = None
        return super()._receipt(event_id, fingerprint, result)

    def _route(self, text, event_id, stamp, occurred_at):
        self._migrate()
        taxonomy = self._taxonomy()
        if self._active_event_kind == 'postback':
            return self._button(text, event_id, stamp, taxonomy)
        context = self._context(taxonomy, text)
        # All authenticated free text reaches the model, including pending answers,
        # reads, cancellation and confirmation. Budget failure cannot grant consent.
        intent = self.planner.plan(text, occurred_at, context)
        if isinstance(intent, PlannerNotice): return self._remember(text, Result(intent.status, intent.message))
        intent = Intent.model_validate(intent)
        pending = self.state.pending
        if pending and stamp <= pending.source_timestamp:
            return self._remember(text, Result('stale', 'คำตอบนี้เกิดก่อนคำถามล่าสุด กรุณาตอบคำถามล่าสุด'))
        blocked = self._blocked_action()
        if blocked: return self._remember(text, self._uncertain(blocked))
        guard = intent_guard(text)
        if guard:
            if guard == 'negated': self.state.pending = None
            return self._remember(text, Result('no_action', 'ไม่มีการบันทึกข้อความนี้'))
        if (re.search(r'(?:แก้|เปลี่ยน).*(?:ที่บันทึกแล้ว|บันทึกไปแล้ว|รายการล่าสุด|รายจ่ายล่าสุด)', text)
                or (not pending and re.match(r'^(?:แก้|เปลี่ยน)', text))):
            return self._remember(text, Result('unsupported', 'ยังแก้รายจ่ายที่บันทึกแล้วไม่ได้ กรุณาตรวจสอบในชีต'))
        source_commands = [re.split(r'(?:หมายเหตุ|\bnote)\s*:?', line, maxsplit=1, flags=re.I)[0]
                           for line in text.splitlines()]
        if any(re.search(r'รายรับ|รับเงิน|เงินเดือน|เงินเข้า|\bincome\b', command, re.I) for command in source_commands):
            return self._remember(text, Result('income_unconfigured', 'ยังไม่ได้ตั้งค่ารายรับ จึงยังไม่บันทึกรายการนี้'))
        if any(re.match(r'^\s*(?:(?:ช่วย|ขอ|กรุณา|please)\s*)*(?:นัด|เตือน|ตารางวันนี้|วันนี้มีอะไร|วันนี้มีนัด)', command, re.I)
               for command in source_commands):
            return self._remember(text, Result('unsupported', 'Calendar ยังไม่ได้เปิดใช้งานในรุ่นรายจ่ายนี้'))
        if not pending and re.fullmatch(self.confirmation, text, re.I):
            return self._remember(text, Result('no_pending', 'ไม่มีรายการที่รออยู่ หรือรายการหมดอายุแล้ว'))
        result = self._interpret(intent, text, event_id, stamp, occurred_at, taxonomy,
                                 getattr(self.planner, 'admitted_choices', context['choices']))
        return self._remember(text, result)

    def _interpret(self, intent, text, event_id, stamp, at, taxonomy, choices):
        pending = self.state.pending
        if intent.intent == 'cancel':
            # Model can classify intent; the owner message must independently say cancel.
            if not re.fullmatch(r'(?:ยกเลิก(?:รายการ(?:นี้|ที่รอ)?)?|cancel)(?:ครับ|ค่ะ)?', text, re.I):
                return Result('clarification', 'ต้องการยกเลิกรายการหรือไม่? ตอบ ยกเลิก')
            self.state.pending = None
            return Result('cancelled', 'ยกเลิกรายการที่รอแล้ว')
        if intent.intent == 'confirm':
            if not pending: return Result('no_pending', 'ไม่มีรายการที่รออยู่ หรือรายการหมดอายุแล้ว')
            if not re.fullmatch(self.confirmation, text, re.I):
                return self._show(pending, taxonomy, 'กรุณาตรวจสอบแล้วกด บันทึก หรือพิมพ์ บันทึก')
            return self._confirm(pending, taxonomy, stamp)
        if intent.intent in {'edit_recorded', 'unsupported'}:
            return Result('unsupported', 'ยังแก้รายจ่ายที่บันทึกแล้วไม่ได้ กรุณาตรวจสอบในชีต')
        if intent.intent == 'no_action': return Result('no_action', 'ไม่มีการบันทึกข้อความนี้')
        if intent.intent in {'summary', 'date_question'}:
            # Reads use independently parsed ranges, never model-invented dates.
            read = read_request(text, at)
            if isinstance(read, PlannerNotice): return Result(read.status, read.message)
            if read and read.name == 'spending_summary':
                args = read.validated_arguments(); totals = self.sheets.summary(args.start_date, args.end_date)
                return Result('summary', f"รายจ่าย {totals['expense']} บาท ({self.expense_label})", totals)
            return Result('clarification', 'ต้องการสรุปวันใด? ระบุ สรุปวันนี้ หรือ สรุปเมื่อวาน')
        if pending and pending.kind != 'conversation':
            return Result('clarification', 'รายการเดิมต้องตรวจสอบก่อน กรุณาพิมพ์ ยกเลิก')
        if not pending:
            if intent.intent not in {'expense', 'edit_draft'} or not intent.entries:
                return Result('help', 'ส่งชื่อรายการและจำนวนเงิน เช่น รถเมล์ 20 บาท')
            entries = [{'item': None, 'amount': None, 'date': None, 'note': '', 'kind': 'expense',
                        'date_parts': {}, 'item_id': None, 'category_id': None,
                        'subcategory_id': None, 'new_item': False, 'date_required': False} for _ in intent.entries]
            pending = self._pending('conversation', event_id, stamp,
                                    {'version': 2, 'entries': entries, 'raw_items': [''] * len(entries),
                                     'ready': False, 'allowed': [], 'category_proposals': {}, 'new_name_confirmed': []})
        before = deepcopy(pending.payload)
        entries = pending.payload['entries']
        named_indices = set()
        evidenced = False
        if intent.entries:
            if len(intent.entries) not in {1, len(entries)}: raise ValueError('Ambiguous draft indices')
            for index, part in enumerate(intent.entries):
                if len(intent.entries) == 1 and len(entries) > 1:
                    named = [i for i,e in enumerate(entries) if part.item and normalized(e['item'] or '') == normalized(part.item) and part.item in text]
                    missing = [i for i,e in enumerate(entries) if any(getattr(part,k) is not None and e[k] is None for k in ('item','amount','date'))]
                    indices = named or missing
                    if len(indices) != 1:
                        pending.payload['editing'] = True; pending.token = secrets.token_hex(4); pending.source_timestamp = stamp
                        return self._show(pending, taxonomy, 'มีหลายรายการ กรุณาระบุชื่อรายการที่แก้ หรือส่งข้อมูลทุกรายการทีละบรรทัด')
                    index = indices[0]
                # In a multi-line expense each amount must be evidenced on its own line.
                source = text
                if len(intent.entries) > 1:
                    lines = text.splitlines()
                    if len(lines) != len(entries): raise ValueError('Use one expense per line')
                    source = lines[index]
                if part.item and part.item in command_text(source): named_indices.add(index)
                if intent.intent == 'expense' and part.item:
                    part = part.model_copy(update={'item': grounded_item(source, part.item, at, taxonomy)})
                elif part.item:
                    part = part.model_copy(update={'item': edited_item(source, part.item)})
                old_name = entries[index]['item']
                evidenced = self._merge(entries[index], part, source, at) or evidenced
                if entries[index]['item'] != old_name:
                    pending.payload['category_proposals'].pop(str(index), None)
                    pending.payload['new_name_confirmed'] = [i for i in pending.payload['new_name_confirmed'] if i != index]
                    pending.payload['subcategory_rejected'] = [i for i in pending.payload.get('subcategory_rejected', []) if i != index]
                pending.payload['raw_items'][index] = entries[index]['item'] or ''
        if len(entries) > 1 and intent.intent == 'expense' and all(taxonomy.catalog.match(e['item'] or '').status == 'exact' for e in entries):
            # A global redundant ID cannot retarget one row of a fully grounded
            # known-item batch. Classification stays with Database for every row.
            supplied = ((intent.item_id, {v['id'] for v in choices['items']}),
                        (intent.category_id, {v['id'] for v in choices['categories']}),
                        (intent.subcategory_id, {s['id'] for c in choices['categories'] for s in c['subcategories']}))
            if any(value and value not in allowed for value, allowed in supplied): raise ValueError('ID outside choices')
            for index, e in enumerate(entries):
                match = taxonomy.catalog.match(e['item'])
                e.update(item=match.canonical, item_id=identifier('item', match.canonical), new_item=False, category_id=None, subcategory_id=None)
                pending.payload['category_proposals'].pop(str(index), None)
            if evidenced: pending.payload['editing'] = False
            if pending.payload != before:
                pending.source_timestamp = stamp; pending.token = secrets.token_hex(4)
            return self._show(pending, taxonomy)
        controls = any((intent.item_id, intent.category_id, intent.category_text, intent.subcategory_id, intent.subcategory_text)) or intent.intent == 'enroll_item'
        controls = controls or any(category_choice(text, taxonomy, allow_bare=True, item=e['item'] or '')[1] for e in entries)
        candidates = [i for i, e in enumerate(entries) if not self._resolved(e, taxonomy)]
        named_indices.update(i for i, e in enumerate(entries) if e['item'] and e['item'] in command_text(text))
        if controls and len(entries) > 1 and len(named_indices) != 1 and len(candidates) != 1:
            pending.payload['editing'] = True; pending.token = secrets.token_hex(4); pending.source_timestamp = stamp
            return self._show(pending, taxonomy, 'มีหลายรายการ กรุณาระบุชื่อรายการที่เลือกหมวดหรือชื่อเดิม')
        target = next(iter(named_indices)) if len(named_indices) == 1 else candidates[0] if candidates else 0
        active = entries[target]
        if intent.item_id:
            if intent.item_id not in {v['id'] for v in choices['items']}: raise ValueError('Item ID outside choices')
            row = taxonomy.items[intent.item_id]
            # Selection is a proposal shown in the final preview, never a write.
            if item_selection(text, row.item) or active['item'] == row.item:
                active.update(item=row.item, item_id=intent.item_id, new_item=False, category_id=None, subcategory_id=None)
                pending.payload['category_proposals'].pop(str(target), None)
                pending.payload['new_name_confirmed'] = [i for i in pending.payload['new_name_confirmed'] if i != target]
                pending.payload['subcategory_rejected'] = [i for i in pending.payload.get('subcategory_rejected', []) if i != target]
                evidenced = evidenced or item_selection(text, row.item)
        # Validate all IDs, then independently derive any owner choice from source.
        proposal = deepcopy(pending.payload['category_proposals'].get(str(target), {}))
        if intent.category_id:
            if intent.category_id not in {v['id'] for v in choices['categories']}: raise ValueError('Category ID outside choices')
            if proposal.get('category_id') != intent.category_id: proposal = {'category_id': intent.category_id, 'subcategory_id': None}
        if intent.category_text and not intent.category_id:
            ids = taxonomy.category_matches(intent.category_text)
            if len(ids) == 1 and ids[0] in {v['id'] for v in choices['categories']}:
                proposal = {'category_id': ids[0], 'subcategory_id': None}
        category = proposal.get('category_id') or active['category_id']
        if intent.subcategory_id:
            available = {s['id'] for c in choices['categories'] if c['id'] == category for s in c['subcategories']}
            if intent.subcategory_id not in available: raise ValueError('Subcategory ID outside choices')
            proposal.update(category_id=category, subcategory_id=intent.subcategory_id)
        if intent.subcategory_text:
            ids = [k for k, label in taxonomy.pairs.get(category, {}).items() if normalized(label) == normalized(intent.subcategory_text)]
            if len(ids) == 1: proposal.update(category_id=category, subcategory_id=ids[0])
        ids, explicit_category = category_choice(text, taxonomy, allow_bare=bool(before['entries'][target]['item']) and active['item'] != command_text(text), item=active['item'] or '')
        if explicit_category:
            proposal = {}
            active.update(category_id=ids[0] if len(ids) == 1 else None, subcategory_id=None, new_item=explicit_category != 'rejected')
            if explicit_category == 'rejected': pending.payload['new_name_confirmed'] = [i for i in pending.payload['new_name_confirmed'] if i != target]
            if len(ids) == 1:
                if target not in pending.payload['new_name_confirmed']: pending.payload['new_name_confirmed'].append(target)
                pending.payload['subcategory_rejected'] = [i for i in pending.payload.get('subcategory_rejected', []) if i != target]
                evidenced = True
        if active['category_id']:
            ids, explicit_subcategory = subcategory_choice(text, taxonomy, active['category_id'])
            if len(ids) == 1:
                active['subcategory_id'] = ids[0]; proposal = {}
                evidenced = True
                pending.payload['subcategory_rejected'] = [i for i in pending.payload.get('subcategory_rejected', []) if i != target]
            elif explicit_subcategory == 'rejected':
                active['subcategory_id'] = None; proposal = {}
                pending.payload.setdefault('subcategory_rejected', [])
                if target not in pending.payload['subcategory_rejected']: pending.payload['subcategory_rejected'].append(target)
        if active['category_id'] and proposal.get('category_id') != active['category_id']: proposal = {}
        if proposal:
            pair = taxonomy.pairs.get(proposal.get('category_id'), {})
            if len(pair) == 1: proposal['subcategory_id'] = next(iter(pair))
            pending.payload['category_proposals'][str(target)] = proposal
        else: pending.payload['category_proposals'].pop(str(target), None)
        for index, e in enumerate(entries):
            match = taxonomy.catalog.match(e['item'] or '')
            if match.status == 'exact':
                e.update(item=match.canonical, item_id=identifier('item', match.canonical), new_item=False, category_id=None, subcategory_id=None)
                pending.payload['category_proposals'].pop(str(index), None)
                pending.payload['new_name_confirmed'] = [i for i in pending.payload['new_name_confirmed'] if i != index]
            if e['category_id'] and len(taxonomy.pairs.get(e['category_id'], {})) == 1 and index not in pending.payload.get('subcategory_rejected', []):
                e['subcategory_id'] = next(iter(taxonomy.pairs[e['category_id']]))
        if evidenced: pending.payload['editing'] = False
        elif intent.intent == 'edit_draft' or re.match(r'^(?:แก้|เปลี่ยน)', text):
            pending.payload['editing'] = True
        if pending.payload != before or (evidenced and intent.intent == 'edit_draft'):
            pending.source_timestamp = stamp; pending.token = secrets.token_hex(4)
        return self._show(pending, taxonomy)

    def _candidate(self, pending, index, taxonomy):
        entry = pending.payload['entries'][index]
        proposal = pending.payload.get('category_proposals', {}).get(str(index), {})
        category, subcategory = proposal.get('category_id'), proposal.get('subcategory_id')
        if category not in taxonomy.categories or subcategory not in taxonomy.pairs.get(category, {}): return None
        if index in pending.payload.get('subcategory_rejected', []): return None
        match = taxonomy.catalog.match(entry['item'] or '')
        if match.status == 'exact' or (match.status == 'possible_typo' and index not in pending.payload.get('new_name_confirmed', [])): return None
        return proposal

    def _review(self, pending, taxonomy):
        rows = []
        for index, entry in enumerate(pending.payload['entries']):
            candidate = self._candidate(pending, index, taxonomy) if not self._resolved(entry, taxonomy) else None
            category = candidate['category_id'] if candidate else entry['category_id']
            subcategory = candidate['subcategory_id'] if candidate else entry['subcategory_id']
            mapping = asdict(taxonomy.mapping(entry['item'], category, subcategory)) if candidate or entry['new_item'] else None
            rows.append({'entry': {k: entry[k] for k in ('item','amount','date','note','kind')}, 'mapping': mapping,
                         'suggested': bool(candidate)})
        return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _merge(entry, part, text, at):
        evidenced = False
        note_match = re.search(r'(?:หมายเหตุ|\bnote)\s*:?\s*(.+)$', text, re.I)
        date_source = text[:note_match.start(1)] if note_match else text
        if part.item is not None:
            matches = list(re.finditer(re.escape(part.item), text, re.I))
            if not part.item.strip() or (not matches and part.item != entry['item']): raise ValueError('Item lacks source span')
            evidenced = bool(matches)
            if normalized(entry['item'] or '') != normalized(part.item):
                entry.update(item=part.item, item_id=None, category_id=None, subcategory_id=None, new_item=False)
        else: matches = list(re.finditer(re.escape(entry['item']), text, re.I)) if entry['item'] else []
        value, parts, spans = resolve(date_source, at, entry['date_parts'])
        explicit = dates(date_source, at).explicit or bool(parts) or bool(entry['date_parts'])
        evidenced = evidenced or explicit
        if part.date is not None and value is not None and part.date not in {value, entry['date']}:
            raise ValueError('Date lacks source evidence')
        if explicit or (entry['date'] is None and not entry.get('date_required')):
            entry['date'], entry['date_parts'] = value, parts
            entry['date_required'] = value is None
        if part.amount is not None:
            # Only the owner's explicit trailing note delimiter can remove a
            # competing numeric source span. A model-selected note cannot.
            note_spans = [note_match.span(1)] if note_match else []
            values = amounts(text, (*spans, *((m.start(), m.end()) for m in matches), *note_spans))
            if values or part.amount != entry['amount']:
                if len(values) != 1 or values[0].value is None or Decimal(values[0].value) != Decimal(part.amount):
                    raise ValueError('Amount lacks unambiguous source evidence')
                entry['amount'] = values[0].value
                evidenced = True
            elif values: evidenced = True
        if part.note is not None:
            if part.note and part.note not in text and part.note != entry['note']: raise ValueError('Note lacks source span')
            entry['note'] = part.note
            evidenced = evidenced or bool(part.note and part.note in text)
        elif note_match:
            if len(note_match[1]) > 300: raise ValueError('Bounded note required')
            entry['note'] = note_match[1]; evidenced = True
        return evidenced

    @staticmethod
    def _resolved(entry, taxonomy):
        if entry['item_id'] in taxonomy.items:
            return taxonomy.items[entry['item_id']].item == entry['item']
        return (entry['new_item'] and entry['category_id'] in taxonomy.categories
                and entry['subcategory_id'] in taxonomy.pairs[entry['category_id']])

    def _show(self, pending, taxonomy, prefix=''):
        entries = pending.payload['entries']; buttons = []
        missing = None
        proposed = []
        for index, entry in enumerate(entries):
            for field in ('item', 'amount', 'date'):
                if not entry[field]: missing = field; break
            if missing: break
            if not self._resolved(entry, taxonomy):
                candidate = self._candidate(pending, index, taxonomy)
                if candidate:
                    proposed.append((index, candidate)); continue
                missing = 'category' if entry['new_item'] else 'resolution'
                if entry['category_id']: missing = 'subcategory'
                break
        if pending.payload.get('editing'): missing = 'correction'
        if missing:
            pending.payload.pop('preview_digest', None)
            pending.payload['focus_index'] = index
            if missing == 'correction': question = 'ต้องการแก้ข้อมูลใด? เช่น แก้ยอดเป็น 250 บาท หรือ วันที่ 2026-10-10'
            elif missing == 'date':
                question = 'วันที่ ' + str(entry['date_parts']['day']) + ' เป็นเดือนใด?' if entry['date_parts'].get('day') and not entry['date_parts'].get('month') else 'รายการนี้เป็นวันที่เท่าไร? ระบุ YYYY-MM-DD'
            elif missing == 'item': question = 'รายการชื่ออะไร?'
            elif missing == 'amount': question = 'จำนวนเงินเท่าไร?'
            elif missing == 'resolution':
                suggestions = taxonomy.catalog.match(entry['item']).suggestions
                question = f"ไม่พบชื่อที่ตรงกับ {entry['item']} ต้องการเลือกชื่อเดิม หรือเพิ่มรายการใหม่ในหมวดใด?"
                buttons = [(name, 'item', identifier('item', name)) for name in suggestions[:5]]
                buttons.append(('เพิ่มรายการใหม่', 'new', ''))
            elif missing == 'category':
                question = f"เพิ่ม {entry['item']} ในหมวดใด? เลือกหมวดที่มีอยู่แล้ว"
                choices = taxonomy.choices(entry['item'])['categories']
                buttons = [(v['label'], 'category', v['id']) for v in choices]
            else:
                question = f"เลือกหมวดย่อยของ {taxonomy.categories.get(entry['category_id'], '')}"
                buttons = [(v or '(ไม่มีหมวดย่อย)', 'subcategory', k) for k,v in list(taxonomy.pairs.get(entry['category_id'], {}).items())[:8]]
            pending.payload['ready'] = False
            text = (prefix + '\n' if prefix else '') + question
            status = 'clarification'
        else:
            displayed = deepcopy(entries)
            for index, candidate in proposed: displayed[index].update(**candidate, new_item=True)
            proposed_indices = {i for i, _ in proposed}
            text = 'ตรวจสอบรายการ:\n' + '\n'.join(f"{e['item']} {e['amount']} บาท วันที่ {e['date']}" +
                    (f"\nหมายเหตุ: {e['note']}" if e['note'] else '') +
                    (f"\nชื่อใหม่ — เพิ่มใน Database: {taxonomy.categories[e['category_id']]}" +
                     (f" / {taxonomy.pairs[e['category_id']][e['subcategory_id']]}" if taxonomy.pairs[e['category_id']][e['subcategory_id']] else '')
                     if e['new_item'] else '') +
                    ('\nหมวดแนะนำจากโมเดล (รอยืนยัน)' if index in proposed_indices else '')
                    for index, e in enumerate(displayed))
            if proposed: text += '\nหมวดแนะนำจากโมเดล ยังไม่ใช่หมวดที่คุณเลือก\nการยืนยันจะเพิ่มชื่อใหม่พร้อมหมวดข้างต้นและบันทึกรายจ่าย'
            text += '\nกด บันทึก หรือพิมพ์ บันทึก / แก้ยอดหรือวันที่ / ยกเลิก'
            if prefix: text = prefix + '\n' + text
            pending.payload['ready'] = True
            pending.payload['preview_digest'] = self._review(pending, taxonomy)
            buttons = [('ยืนยันเพิ่ม+บันทึก' if proposed else 'บันทึก', 'confirm', ''), ('แก้ไข', 'edit', '')]
            status = 'preview'
        buttons.append(('ยกเลิก', 'cancel', ''))
        pending.payload['allowed'] = [[op, value] for _, op, value in buttons]
        return Result(status, text, {'stage': missing or 'ready', 'line_message': message(text, pending, buttons)})

    def _button(self, text, event_id, stamp, taxonomy):
        pending = self.state.pending
        parts = text.split('|')
        if (not pending or pending.kind != 'conversation' or len(parts) != 5 or parts[0] != 'v1'
                or parts[1] != pending.token or parts[2] != version(pending)
                or [parts[3], parts[4]] not in pending.payload['allowed'] or stamp <= pending.source_timestamp):
            return Result('stale', 'ปุ่มนี้หมดอายุหรือรายการเปลี่ยนแล้ว กรุณาตอบคำถามล่าสุด')
        blocked = self._blocked_action()
        if blocked: return self._uncertain(blocked)
        op, value = parts[3:]
        if op == 'cancel': self.state.pending = None; return Result('cancelled', 'ยกเลิกรายการที่รอแล้ว')
        if op == 'confirm': return self._confirm(pending, taxonomy, stamp)
        if op == 'edit':
            pending.payload['editing'] = True
            pending.token = secrets.token_hex(4); pending.source_timestamp = stamp
            return self._show(pending, taxonomy)
        index = pending.payload.get('focus_index', 0)
        entry = pending.payload['entries'][index]
        if op == 'new':
            entry['new_item'] = True
            if index not in pending.payload['new_name_confirmed']: pending.payload['new_name_confirmed'].append(index)
        elif op == 'item':
            if value not in taxonomy.items: raise ValueError('Item removed')
            entry.update(item=taxonomy.items[value].item, item_id=value, new_item=False, category_id=None, subcategory_id=None)
            pending.payload['category_proposals'].pop(str(index), None)
            pending.payload['new_name_confirmed'] = [i for i in pending.payload['new_name_confirmed'] if i != index]
        elif op == 'category':
            if value not in taxonomy.categories: raise ValueError('Category removed')
            entry.update(category_id=value, subcategory_id=None, new_item=True)
            pending.payload['category_proposals'].pop(str(index), None)
            if index not in pending.payload['new_name_confirmed']: pending.payload['new_name_confirmed'].append(index)
            pending.payload['subcategory_rejected'] = [i for i in pending.payload.get('subcategory_rejected', []) if i != index]
            if len(taxonomy.pairs[value]) == 1: entry['subcategory_id'] = next(iter(taxonomy.pairs[value]))
        elif op == 'subcategory':
            if value not in taxonomy.pairs.get(entry['category_id'], {}): raise ValueError('Subcategory removed')
            entry['subcategory_id'] = value
            pending.payload['category_proposals'].pop(str(index), None)
            pending.payload['subcategory_rejected'] = [i for i in pending.payload.get('subcategory_rejected', []) if i != index]
        pending.token = secrets.token_hex(4); pending.source_timestamp = stamp
        return self._show(pending, taxonomy)

    def _confirm(self, pending, taxonomy, stamp):
        if pending.kind != 'conversation' or not pending.payload['ready']:
            return self._show(pending, taxonomy)
        entries = pending.payload['entries']
        if any(not self._resolved(e, taxonomy) and not self._candidate(pending, i, taxonomy) for i, e in enumerate(entries)):
            pending.token = secrets.token_hex(4)
            return self._show(pending, taxonomy, 'Database เปลี่ยนแล้ว กรุณาตรวจสอบอีกครั้ง')
        if pending.payload.get('preview_digest') != self._review(pending, taxonomy):
            pending.token = secrets.token_hex(4); pending.source_timestamp = stamp
            return self._show(pending, taxonomy, 'รายการหรือ Database เปลี่ยนแล้ว กรุณาตรวจสอบและยืนยันอีกครั้ง')
        entries = deepcopy(entries)
        for index, entry in enumerate(entries):
            if not self._resolved(entry, taxonomy):
                entry.update(**self._candidate(pending, index, taxonomy), new_item=True)
        args = AddTransactions.model_validate({'entries': [{k: e[k] for k in ('item','amount','date','note','kind')} for e in entries]})
        mappings = [asdict(taxonomy.mapping(e['item'], e['category_id'], e['subcategory_id'])) for e in entries if e['new_item']]
        mappings = [dict(v) for v in {tuple(m.items()):m for m in mappings}.values()]
        payload = {'entries': args.model_dump(mode='json')['entries'], 'catalog': mappings}
        key = self._key(pending.origin_event_id, 'catalog_expense' if mappings else 'add_transactions')
        return self._execute(key, 'catalog_expense' if mappings else 'add_transactions', payload if mappings else {'entries': payload['entries']},
                             pending.origin_event_id, {'raw_items': pending.payload['raw_items']})
