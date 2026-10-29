"""Model-selected expense groups, checked against immutable owner quotations.

This path does not parse conjunctions, price lists or name/amount adjacency.
The model supplies boundaries and field evidence; the server checks literal
coordinates, typed values, numeric coverage and occurrence reuse. Semantic
interpretation still requires an owner-reviewed proposal before any write.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Literal

from pydantic import Field
from .tables import Strict
from .table_workflow import schema
from .tool_errors import PlanningError
from .language import BANGKOK, DIGITS, WORD_TOKEN, money_string, thai_integer
from .history_context import digest


class Quote(Strict):
    source_ref: str
    text: str = Field(min_length=1, max_length=2000)
    occurrence: int = Field(ge=0, le=1999, description='Zero-based exact occurrence of text in this original owner message. Usually 0.')


class Group(Strict):
    group_ref: str = Field(min_length=1, max_length=40)
    item: str | None = Field(max_length=200, description='Full item name, preserving internal words/spaces, or a proposed real catalog spelling. Null only when genuinely missing.')
    item_source: Quote | None
    sources: list[Quote] = Field(min_length=1, max_length=16,
        description='Exact portions belonging to this item group, including separate portions in the same message when groups are interleaved. Different groups cannot overlap. Shared global dates/counts may be outside groups.')


class Record(Strict):
    group_ref: str
    amount: str | int | None = Field(description='Positive THB amount as digits, with at most two decimals; no currency text or arithmetic. Null only when genuinely missing.')
    amount_source: Quote | None = Field(description='Quote just the original numeric amount for this occurrence. Reuse a numeric span only with an explicit occurrences count claim.')
    date_source: Quote | None = Field(description='Literal date expression, including shared dates, or null for Bangkok today. Resolve relative wording at the original source timestamp.')
    date_value: str | None = Field(default=None, description='ISO YYYY-MM-DD interpreted from date_source and that original message timestamp. Supply together with date_source to resolve clear wording in this same call. Never guess a genuinely ambiguous date: use date_ambiguous=true instead. Null when omitted, ambiguous or an offered date_ref is selected. This is a reviewed proposal, never consent.')
    date_ambiguous: bool = Field(default=False, description='True only when a quoted date/reference remains genuinely ambiguous after using visible history. Leave date_value/date_ref null; the server retains the other facts and asks for the date without a repair round. False when omitted or resolvable.')
    date_ref: str | None = Field(description='An offered date_references ref selected by the current conversational meaning, or null. Do not combine with date_source.')
    note: str | None = Field(max_length=300)
    note_source: Quote | None = Field(description='Literal owner note value, separate from the complete item name. Null when no note was requested.')


class Count(Strict):
    source: Quote = Field(description='Quote only the original integer count, including Thai number words when applicable.')
    entries: list[int] = Field(min_length=1, max_length=5, description='Zero-based entry indexes whose count this is.')
    kind: Literal['occurrences', 'batch'] = Field(description='occurrences authorizes repeated use of one per-payment amount. batch only checks the total number of entries. Never interpret an aggregate total as a per-payment amount.')


class ContextNumber(Strict):
    source: Quote = Field(description='Quote one numeric token that is context rather than a payment, such as a referenced record ordinal. Never omit a requested payment this way.')
    reason: str = Field(min_length=1, max_length=200)


class Proposal(Strict):
    draft_ref: None
    draft_version: None
    groups: list[Group] = Field(min_length=1, max_length=5)
    entries: list[Record] = Field(min_length=1, max_length=5)
    counts: list[Count] = Field(max_length=10)
    context_numbers: list[ContextNumber] = Field(max_length=16)
    only_if_absent: bool = Field(description='True only when owner requests checking saved records and preparing these payments only if absent. The server checks exact item/date/amount before creating a proposal. Never writes.')


def definition(date_references=()):
    parameters = schema(Proposal)
    parameters['properties']['entries']['items']['properties']['date_ref']['enum'] = [None, *[v['date_ref'] for v in date_references]]
    return {'type': 'function', 'name': 'prepare_expenses', 'strict': True,
        'description': 'Prepare NEW payments for owner review, never save. You interpret grouping and field ownership. Supply each payment as an entry referring to its item group and exact original owner quotations. Keep repeated payments separate. Preserve full names; item may propose a catalog spelling but item_source always quotes the original name. Account for every number in the current message and chosen group portions as item, amount, date, note, count or explicit non-payment context. Do not invent evidence or infer missing facts. Null fields are genuinely missing. Existing pending records use patch_pending_records.',
        'parameters': parameters}


def fail(code, field, required=()):
    raise PlanningError(code, fields=[field], required=required, tools=['prepare_expenses', 'resolve_temporal'])


def quote(h, q, field):
    row = h._owner_sources([q.source_ref])[0]
    starts = [i for i in range(len(row['text'])) if row['text'].startswith(q.text, i)]
    if q.occurrence >= len(starts):
        fail('expense_quote_not_found', field, ['Use an exact original owner quotation and its zero-based occurrence.'])
    start = starts[q.occurrence]
    return {**row, 'text': q.text, 'span': [start, start + len(q.text)]}


def overlaps(a, b):
    return a['source_ref'] == b['source_ref'] and a['span'][0] < b['span'][1] and b['span'][0] < a['span'][1]


def inside(a, b):
    return a['source_ref'] == b['source_ref'] and b['span'][0] <= a['span'][0] <= a['span'][1] <= b['span'][1]


def number(text):
    text = text.translate(DIGITS)
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?', text):
        return Decimal(text.replace(',', ''))
    return Decimal(thai_integer(text))


def numeric_quote(h, q, field):
    row = quote(h, q, field)
    try: value = number(row['text'])
    except (ValueError, ArithmeticError): fail('expense_numeric_quote_required', field)
    original = h._owner_sources([row['source_ref']])[0]['text'].translate(DIGITS)
    a, b = row['span']
    # A quote cannot crop digits, decimal fractions or signed numbers. Commas
    # deliberately have no grouping meaning here: the model selects whether
    # they belong to one literal number or separate payment occurrences.
    if (a and (original[a-1].isdigit() or original[a-1] in '.-−+')
            or b < len(original) and (original[b].isdigit() or original[b] == '.')):
        fail('expense_partial_numeric_quote', field)
    if any(start <= a < b <= end and (start, end) != (a, b)
           for start, end in numeric_spans(original)):
        fail('expense_partial_numeric_quote', field)
    return row, value


def numeric_spans(text):
    """Lexical numeric positions only; no dates, counts or payment grammar."""
    for m in re.finditer(r'\d+(?:\.\d+)?', text.translate(DIGITS)):
        yield m.span()
    for m in re.finditer('(?:' + WORD_TOKEN + ')+', text):
        try: thai_integer(m[0])
        except ValueError: continue
        yield m.span()


def compile_proposal(h, args):
    p = Proposal.model_validate(args)
    groups = {}; all_portions = []; claims = []
    def resolved(q, field):
        row = quote(h, q, field)
        claims.append(row)
        return row
    for i, group in enumerate(p.groups):
        path = f'groups[{i}]'
        if group.group_ref in groups: fail('expense_duplicate_group', path + '.group_ref')
        portions = [quote(h, q, path + '.sources') for q in group.sources]
        if any(overlaps(a, b) for a in portions for b in all_portions):
            fail('expense_groups_overlap', path + '.sources', ['Choose disjoint original portions for different item groups.'])
        all_portions += portions
        item = resolved(group.item_source, path + '.item_source') if group.item_source else None
        if bool(group.item) != bool(item) or item and len(item['text']) > 200: fail('expense_item_evidence_required', path + '.item_source')
        if item and not any(inside(item, v) for v in portions): fail('expense_field_outside_group', path + '.item_source')
        groups[group.group_ref] = (group, portions, item)
    entries = []; contexts = []; used = defaultdict(list)
    for i, record in enumerate(p.entries):
        path = f'entries[{i}]'
        if record.group_ref not in groups: fail('expense_unknown_group', path + '.group_ref')
        group, portions, item = groups[record.group_ref]
        fields = {'item': item}; amount = None
        if record.amount is not None:
            if record.amount_source is None: fail('expense_amount_evidence_required', path + '.amount_source')
            proof, actual = numeric_quote(h, record.amount_source, path + '.amount_source')
            claims.append(proof)
            if not any(inside(proof, v) for v in portions): fail('expense_field_outside_group', path + '.amount_source')
            if item and overlaps(item, proof): fail('expense_field_overlap', path + '.amount_source')
            try:
                amount = money_string(Decimal(str(record.amount)))
                if Decimal(amount) != actual: raise ValueError()
            except (ValueError, ArithmeticError): fail('expense_amount_mismatch', path + '.amount')
            fields['amount'] = proof
            used[(proof['source_ref'], *proof['span'])].append(i)
        elif record.amount_source: fail('expense_amount_value_required', path + '.amount')
        for field, q in [('date', record.date_source), ('note', record.note_source)]:
            fields[field] = resolved(q, path + '.' + field + '_source') if q else None
            if fields[field] and any(overlaps(fields[field], v) for v in [item, fields.get('amount')] if v):
                fail('expense_field_overlap', path + '.' + field + '_source')
        if record.date_ref and record.date_source: fail('conflicting_date_reference', path + '.date_ref')
        resolution=None
        if record.date_ambiguous and (not record.date_source or record.date_value is not None or record.date_ref):
            fail('expense_date_evidence_required',path+'.date_ambiguous')
        if record.date_value is not None:
            if not record.date_source or record.date_ref:fail('expense_date_evidence_required',path+'.date_value')
            from .temporal import execute, get
            resolved_date=execute(h,{'source_ref':record.date_source.source_ref,'source_text':record.date_source.text,
                'kind':'date','mode':'components','date':record.date_value})
            if not resolved_date.get('ok'):fail('expense_date_value_invalid',path+'.date_value')
            _,resolution=get(h,resolved_date['temporal_ref'],'date',source_ref=record.date_source.source_ref,source_text=record.date_source.text)
        if record.note is not None and (not fields['note'] or record.note != fields['note']['text']):
            fail('expense_note_mismatch', path + '.note')
        if record.note is None and fields['note']: fail('expense_note_value_required', path + '.note')
        refs = list(dict.fromkeys([h._current['source_ref'], *[v['source_ref'] for v in portions],
            *[v['source_ref'] for v in fields.values() if v]]))
        entries.append({'item': group.item, 'item_text': item['text'] if item else None,
            'amount': amount, 'date_text': fields['date']['text'] if fields['date'] else None,
            'date_ref': record.date_ref, 'note': record.note, 'source_refs': refs,
            'field_sources': {k: [v['source_ref']] if v else [] for k, v in fields.items()}})
        contexts.append({'fields': fields, 'portions': portions, 'date_ambiguous':record.date_ambiguous,
            **({'date_resolution':resolution} if resolution else {})})
    if set(groups) != {e.group_ref for e in p.entries}: fail('expense_unused_group', 'groups')
    money_proofs = [c['fields']['amount'] for c in contexts if c['fields'].get('amount')]
    other_proofs = [c['fields'][k] for c in contexts for k in ['item', 'date', 'note'] if c['fields'].get(k)]
    if any(overlaps(a, b) for a in money_proofs for b in other_proofs): fail('expense_field_overlap', 'entries')
    counts = []
    for i, count in enumerate(p.counts):
        proof, value = numeric_quote(h, count.source, f'counts[{i}].source'); claims.append(proof)
        if len(set(count.entries)) != len(count.entries) or any(j < 0 or j >= len(entries) for j in count.entries) or value != len(count.entries):
            fail('expense_count_mismatch', f'counts[{i}].entries')
        if any(overlaps(proof, v) for c in contexts for v in c['fields'].values() if v): fail('expense_field_overlap', f'counts[{i}].source')
        counts.append((count, proof))
    for key, indexes in used.items():
        if len(indexes) > 1 and not any(c.kind == 'occurrences' and set(c.entries) == set(indexes) for c, _ in counts):
            fail('expense_amount_reused_without_count', f'entries[{indexes[0]}].amount_source', ['Cite each distinct amount occurrence, or an explicit per-payment repetition count covering exactly these entries.'])
        for ordinal, i in enumerate(indexes):
            proof = contexts[i]['fields']['amount']
            occurrence = {'source_ref': proof['source_ref'], 'span': proof['span'], 'ordinal': ordinal,
                'amount': entries[i]['amount'], 'at': proof['at']}
            occurrence['occurrence_ref'] = 'occ_' + digest(occurrence)[:20]
            contexts[i]['occurrence'] = occurrence
    disclosures = []
    for i, context in enumerate(p.context_numbers):
        proof, _ = numeric_quote(h, context.source, f'context_numbers[{i}].source')
        if any(overlaps(proof, v) for c in contexts for v in c['fields'].values() if v): fail('expense_field_overlap', f'context_numbers[{i}].source')
        claims.append(proof); disclosures.append({'text': proof['text'], 'reason': context.reason, 'source_ref': proof['source_ref']})
    # Require coverage across the whole current message, plus chosen historical
    # portions. Group boundaries alone never explain away omitted numbers.
    coverage = [*all_portions, {**h._current, 'span': [0, len(h._current['text'])]}]
    for row in coverage:
        for a, b in numeric_spans(row['text']):
            a += row['span'][0]; b += row['span'][0]
            if not all(any(v['source_ref'] == row['source_ref'] and v['span'][0] <= n < v['span'][1] for v in claims) for n in range(a, b)):
                fail('expense_unaccounted_number', 'entries', [f"Account for original numeric text {row['text'][a-row['span'][0]:b-row['span'][0]]!r} at {row['source_ref']}[{a}:{b}] without dropping a requested payment."])
    if disclosures: contexts[0]['context_numbers'] = disclosures
    return {'draft_ref': None, 'draft_version': None, 'entries': entries, 'only_if_absent': p.only_if_absent}, contexts


def prepare(h, args):
    translated, contexts = compile_proposal(h, args)
    before = getattr(h, '_literal_expense', None)
    try:
        h._literal_expense = contexts
        return h._prepare(translated)
    finally:
        h._literal_expense = before


def entry(h, value, old, index):
    """Prepare typed facts from already checked field evidence, no regrouping."""
    from .catalog_names import plan
    from .history_dates import resolve, cues
    from . import semantic_tools as semantic
    contexts = getattr(h, '_literal_expense', None)
    context = contexts[index] if contexts is not None else None
    patch = getattr(h, '_patch_field_sources', {}).get(value.entry_ref)
    fields = context['fields'] if context else {('date' if k == 'date_text' else k): v for k, v in patch.items()}
    sources = h._entry_sources(value, old)
    tax = h._taxonomy(); missing = []; invalid = []
    literal = (value.item_text or value.item if patch is not None else fields['item']['text']) if fields.get('item') else old['item'] if old else None
    selected = plan(value.model_copy(update={'item_text': literal}), [fields['item']] if fields.get('item') else [], tax.catalog)
    item = selected.get('canonical') or literal
    if not item: missing.append('item')
    metadata = {}
    if item and literal != item: metadata['item_resolution'] = {'from': literal, 'to': item}
    if selected.get('candidates') and not selected.get('canonical'): metadata['name_candidates'] = selected['candidates']
    same_item = bool(old and item == old['item'])
    if same_item:
        for key in ['item_resolution', 'name_candidates']:
            if key in old: metadata.setdefault(key, deepcopy(old[key]))
    if item and not same_item and not selected.get('canonical') and not metadata.get('name_candidates') and getattr(h, 'name_matching_enabled', False):
        from .name_matching import candidates
        found = candidates(h, item, tax)
        if found: metadata['name_candidates'] = found
    amount = old['amount'] if old and value.amount is None else None
    price = h.previous_prices.selection(value, old)
    if price: amount = price['amount']
    elif value.amount is not None:
        try: amount = money_string(Decimal(str(value.amount)))
        except (ValueError, ArithmeticError): invalid.append('amount')
    if amount is None: missing.append('amount')
    note = old['note'] if old and value.note is None else value.note or ''
    date_field = fields.get('date')
    units = [(date_field['text'], datetime.fromisoformat(date_field['at']), date_field['source_ref'])] if date_field else []
    if not old and not units: units = [('', datetime.fromisoformat(h._current['at']), h._current['source_ref'])]
    ref = date_field['source_ref'] if date_field else h._current['source_ref']
    resolution=context.get('date_resolution') if context else None
    ambiguous=bool(context and context.get('date_ambiguous'))
    if ambiguous:
        calculated=None;parts={};issue='date_clarification_required'
        basis={'policy':'owner-date-clarification-v1','source_refs':[date_field['source_ref']],
            'original_date_text':date_field['text'],'anchor_date':datetime.fromisoformat(date_field['at']).astimezone(BANGKOK).date().isoformat()}
    elif resolution:
        calculated=resolution['values']['date'];parts={};issue=None
        basis={'policy':'semantic-temporal-v1','temporal':deepcopy(resolution),'source_refs':[resolution['source_ref']]}
    else:calculated, parts, basis, issue = resolve(units, old, ref, h.clock())
    if date_field and not resolution and not ambiguous and not value.date_ref and not any(cues(text, at)[0] or cues(text, at)[2] for text, at, _ in units): invalid.append('date_expression_unresolved')
    if value.date_ref:
        available = {v['date_ref']: v for v in semantic.date_targets(h)}
        target = available.get(value.date_ref)
        if not target or h._request_date_views.get(value.date_ref) != target: invalid.append('date_reference_unavailable')
        elif date_field and not date_field.get('temporal'): invalid.append('conflicting_date_reference')
        elif target.get('temporal'):
            from .temporal import get
            calculated, proof = get(h, value.date_ref, 'date')
            parts = {}; issue = None; basis = {'policy': 'semantic-temporal-v1', 'temporal': proof, 'source_refs': [proof['source_ref']]}
        else:
            calculated = target['date']; parts = {}; issue = None
            basis = {'policy': 'previous-question-date-v01' if target.get('previous_question') else 'verified-date-reference-v01',
                'date_ref': value.date_ref, 'referenced_date': calculated, 'source_refs': [],
                **({'confirmed_event': target['confirmed_event']} if target.get('confirmed_event') else {})}
    if calculated and date.fromisoformat(calculated) > h.clock().astimezone(BANGKOK).date(): issue = 'future_expense_date'
    if issue and issue not in {'missing_day', 'missing_month_for_future_day','date_clarification_required'}: invalid.append(issue)
    if not calculated: missing.append('date.month' if issue == 'missing_month_for_future_day' else 'date')
    mapping = deepcopy(old['mapping']) if same_item else None
    enrollment = deepcopy(old.get('enrollment')) if same_item else None
    if item and tax.catalog.match(item).status != 'exact':
        if enrollment:
            category = enrollment.get('category_id'); subcategory = enrollment.get('subcategory_id')
            if category not in tax.categories and category is not None: raise ValueError('category_not_in_taxonomy')
            if subcategory is not None and subcategory not in tax.pairs.get(category, {}): raise ValueError('subcategory_not_in_selected_category')
            mapping = vars(tax.mapping(item, category, subcategory)) if category and subcategory else None
            if category is None: missing.append('category')
            elif subcategory is None: missing.append('subcategory')
        elif mapping is None: missing.append('item_resolution')
    fact_sources = deepcopy(old.get('fact_sources', {})) if old else {}
    for field in ['item', 'amount', 'date', 'note']:
        if not old or field in fields and fields[field]:
            fact_sources[field] = basis.get('source_refs', []) if field == 'date' else [fields[field]['source_ref']] if fields.get(field) else []
    if price: fact_sources['amount'] = [h._current['source_ref']]
    times = deepcopy(old.get('fact_times', {})) if old else {}
    for field in ['amount', 'date']:
        if not old or fields.get(field): times[field] = (basis.get('temporal', {}).get('at') if field == 'date' else None) or (fields[field]['at'] if fields.get(field) else h._current['at'])
    if price: times['amount'] = h._current['at']
    result = {'entry_ref': old['entry_ref'] if old else 'entry_' + digest([h._active_event_id, index])[:20],
        'item': item, 'amount': amount, 'date': calculated, 'date_parts': parts if not calculated else {},
        'note': note, 'date_basis': basis, 'date_issue': issue, 'kind': 'expense', 'mapping': mapping,
        'source_protocol': 'quoted-expenses-v1',
        'fact_sources': fact_sources, 'fact_times': times, **metadata,
        'source_quote': next((r['text'][:200] for r in reversed(sources) if fields.get('item') and r['source_ref'] == fields['item']['source_ref']), old.get('source_quote', '') if old else ''),
        'source_refs': list(dict.fromkeys((old['source_refs'] if old else []) + [r['source_ref'] for r in sources] + basis.get('source_refs', [])))}
    if enrollment: result['enrollment'] = enrollment
    if context and context.get('occurrence'): result['amount_occurrence'] = deepcopy(context['occurrence'])
    elif old and amount == old['amount'] and old.get('amount_occurrence'): result['amount_occurrence'] = deepcopy(old['amount_occurrence'])
    if context and context.get('context_numbers'): result['context_numbers'] = deepcopy(context['context_numbers'])
    elif old and old.get('context_numbers'): result['context_numbers'] = deepcopy(old['context_numbers'])
    if price: result['previous_price'] = price
    elif old and old.get('previous_price') and amount == old['amount'] and not fields.get('amount'): result['previous_price'] = deepcopy(old['previous_price'])
    return result, missing, invalid, sources
