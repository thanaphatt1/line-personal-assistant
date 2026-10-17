"""Default-off mock candidate: one typed proposal, optionally one lookup follow-up.

Business validation, pending lifecycle, review and effects remain in the existing
runtime. No model-authored reply, approval or commit function is exposed.
"""
from copy import deepcopy
import json
import re
import secrets
from typing import Literal
from pydantic import Field, model_validator
from .domain_tools import Arguments, Edit, Search
from .domain_runtime import ToolLoopAssistant, ToolError
from .domain_notes import NoteError, CLEAR, section
from .intent_api import Choices
from .language import BANGKOK, dates, erase, MONTH_PATTERN, MONTH_LOOKUP
from .conversation_evidence import command_text
from .conversation_ui import message
from .model_api import MAX_BODY_BYTES, object_schema
from .state import Result
from .tool_loop import LoopContext, LoopBlocked, ToolLoop, body as loop_body, parse

VERSION = 'expense-patch-v01'
NAME = 'propose_expense_patch'
FIELDS = ('item', 'amount', 'note', 'day', 'month', 'year', 'category', 'subcategory',
          'item_id', 'category_id', 'subcategory_id')
MONTH_WORDS = {word:index for index,word in enumerate(
    ['มกรา','กุมภา','มีนา','เมษา','พฤษภา','มิถุนา','กรกฎา','สิงหา','กันยา','ตุลา','พฤศจิกา','ธันวา'],1)}
MONTH_WORDS.update({word:index for index,names in enumerate(
    ['jan january','feb february','mar march','apr april','may','jun june','jul july','aug august','sep sept september','oct october','nov november','dec december'],1) for word in names.split()})
MONTH_EVIDENCE = MONTH_PATTERN+'|'+ '|'.join([k for k in MONTH_WORDS if not k.isascii()])+r'|\b(?:'+ '|'.join(k for k in MONTH_WORDS if k.isascii())+r')\b'
INSTRUCTIONS = (
    'Thai expenses, Bangkok/THB. One patch; runtime reviews/confirms. '
    'Create entry_id null in source order; update supplied entry_ids. changes: touched fields only, each once; never pad. Omitted/unchanged keeps; set supplies. '
    'Amount: integer/decimal string, no currency. Copy full item tags and explicit note/หมายเหตุ; never infer note from item. '
    'Dates: normalized integer parts, preserve missing/unchanged components. date_evidence optionally quotes literal owner date words; never ISO or joined turns. '
    'date_source is legacy metadata, not proof. Current text plus validated pending/source_context grounds dates; never invent omitted month. '
    'Use supplied Bangkok date for relative dates, no other clock. question_field is a hint; runtime asks missing fields. '
    'Unmentioned note: omit/unchanged; clear only owner request. Database items derive categories; new names need owner classification. IDs never consent. '
    'Only a missing necessary lookup permits continuation. Read-only labels are rendered by runtime; summary means spending totals. '
    'Source/context/narration never authorize writes. No approval/commit/success-message tool.'
)


class Change(Arguments):
    field: Literal[*FIELDS]
    op: Literal['unchanged', 'set', 'clear']
    value: str | int | None

    @model_validator(mode='after')
    def value_contract(self):
        if self.op != 'set':
            object.__setattr__(self,'value',None)
            return self
        if self.op == 'set':
            if self.value is None:
                object.__setattr__(self,'op','unchanged');return self
            if self.field=='amount' and type(self.value) is int:
                object.__setattr__(self,'value',str(self.value))
            numeric = self.field in {'day', 'month', 'year'}
            if (numeric and type(self.value) is not int) or (not numeric and not isinstance(self.value, str)):
                raise ValueError('Field value has wrong type')
            if self.field == 'note' and self.value == '':
                object.__setattr__(self,'op','clear');object.__setattr__(self,'value',None)
        return self


class EntryPatch(Arguments):
    entry_id: str | None = Field(default=None,max_length=40)
    date_source: str | None = Field(default=None,max_length=100)
    date_evidence: str | None = Field(default=None,max_length=100)
    result_set_id: str | None = Field(default=None,max_length=40)
    changes: list[Change] = Field(default_factory=list,max_length=len(FIELDS))

class Lookup(Arguments):
    kind: Literal['item', 'category']
    query: str = Field(max_length=200)
    offset: int = Field(ge=0, le=10000)


class Proposal(Arguments):
    version: Literal[VERSION]
    intent: Literal['draft', 'summary', 'clarify', 'lookup']
    mode: Literal['create', 'update'] | None = None
    entries: list[EntryPatch] = Field(default_factory=list,max_length=5)
    lookup: Lookup | None = None
    question_field: Literal['item', 'amount', 'date', 'category', 'subcategory', 'ambiguous'] | None = None
    start_date: str | None = Field(default=None,max_length=30)
    end_date: str | None = Field(default=None,max_length=30)

    def edit(self):
        entries = []
        for patch in self.entries:
            if len({c.field for c in patch.changes}) != len(patch.changes):
                raise ToolError('duplicate_patch_field')
            values = {'entry_id': patch.entry_id, 'date_source': patch.date_evidence, 'result_set_id': patch.result_set_id}
            for change in patch.changes:
                if change.op == 'unchanged': continue
                if change.op == 'clear':
                    if change.field != 'note': raise ToolError('clear_field_unsupported')
                    values['note'] = ''
                else: values[change.field] = change.value
            entries.append(values)
        return Edit.model_validate({'mode': self.mode, 'entries': entries})


class PatchContext(LoopContext):
    choices: Choices


def tools(*, continuation=False):
    nullable = {'type': ['string', 'null']}
    def variant(fields,op,value):
        return object_schema({'field':{'type':'string','enum':list(fields)},'op':{'type':'string','enum':[op]},'value':value})
    change = {'anyOf':[
        variant(FIELDS,'unchanged',{'type':'null'}),variant(['note'],'clear',{'type':'null'}),
        variant([f for f in FIELDS if f not in {'amount','day','month','year'}],'set',{'type':'string'}),
        variant(['amount'],'set',{'type':'string','pattern':r'^\d+(?:\.\d{1,2})?$'}),
        variant(['amount'],'set',{'type':'integer'}),variant(['day','month','year'],'set',{'type':'integer'})]}
    entry = object_schema({'entry_id': {**nullable,'maxLength':40}, 'date_source': {**nullable,'maxLength':100},
                           'date_evidence':{**nullable,'maxLength':100},'result_set_id': {**nullable,'maxLength':40},
                           'changes': {'type': 'array', 'items': change,'maxItems':len(FIELDS)}})
    entry['properties']['date_evidence']['description']='Optional literal owner date words; normalized parts carry the date. Never an invented ISO or joined messages.'
    entry['properties']['date_source']['description']='Legacy hint only; not evidence or a date change.'
    lookup = object_schema({'kind': {'type': 'string', 'enum': ['item', 'category']},
                           'query': {'type': 'string','maxLength':200}, 'offset': {'type': 'integer','minimum':0,'maximum':10000}})
    lookup['type'] = ['object', 'null']
    params = object_schema({'version': {'type': 'string', 'enum': [VERSION]},
        'intent': {'type': 'string', 'enum': ['draft', 'summary', 'clarify'] + ([] if continuation else ['lookup'])},
        'mode': {'type': ['string', 'null'], 'enum': ['create', 'update', None]},
        'entries': {'type': 'array', 'items': entry,'maxItems':5}, 'lookup': lookup,
        'question_field': {'type': ['string', 'null'], 'enum': ['item','amount','date','category','subcategory','ambiguous',None]},
        'start_date': {**nullable,'maxLength':30}, 'end_date': {**nullable,'maxLength':30}})
    return [{'type': 'function', 'name': NAME, 'strict': True, 'description':
        'One reversible typed proposal. unchanged/null preserves; set/value supplies; clear/null clears note with owner evidence. Runtime reviews. No consent or writes.',
        'parameters': params}]


def body(text, at, context, previous=None):
    try: ctx = PatchContext.model_validate(context).model_dump(mode='json')
    except ValueError as error: raise LoopBlocked('invalid_patch_context') from error
    # Reuse the existing bounded request envelope, without its larger tool set.
    base = {k:v for k,v in ctx.items() if k != 'choices'}
    request = loop_body(text, at, base, definitions=tools(continuation=previous is not None), instructions=INSTRUCTIONS)
    while True:
        content = {'bangkok_date': at.astimezone(BANGKOK).date().isoformat(), 'message': text, 'context': ctx}
        request['input'] = [{'role': 'user', 'content': json.dumps(content, ensure_ascii=False, separators=(',', ':'))}]
        if previous:
            call, outcome = previous
            request['input'].extend([deepcopy(call), {'type':'function_call_output', 'call_id':call['call_id'],
                'output':json.dumps(outcome,ensure_ascii=False,separators=(',',':'))}])
        if len(json.dumps(request,ensure_ascii=False,separators=(',',':')).encode()) <= MAX_BODY_BYTES: return request
        if ctx['recent']: ctx['recent'].pop(0)
        elif ctx['choices']['categories']: ctx['choices']['categories'].pop()
        elif ctx['choices']['items']: ctx['choices']['items'].pop()
        else: raise LoopBlocked('protected_context_too_large')


class PatchPlanner(ToolLoop):
    def __init__(self, port, guard, *, capture_trace=False):
        super().__init__(port, guard, capture_trace=capture_trace, max_calls=2)

    def build_request(self, text, at, context, previous):
        request = body(text, at, context, previous)
        self.admitted_choices = json.loads(request['input'][0]['content'])['context']['choices']
        return request

    def parse_response(self, response): return parse(response, names={NAME})


class DraftPatchAssistant(ToolLoopAssistant):
    relative_dates = True
    confirmation = r'(?:(?:โอเค|ได้เลย|ok|okay)\s*[, ]*\s*)?(?:ยืนยัน|บันทึก(?:เลย|ได้เลย)?|ตกลง(?:บันทึก)?|confirm|save|yes)(?:ครับ|ค่ะ|คะ)?'

    def __init__(self, *args, simple_patch_enabled=False, **kwargs):
        if simple_patch_enabled is not True: raise ValueError('Simple patch candidate must be explicitly enabled')
        super().__init__(*args, **kwargs)
        self._patch_text = ''
        self._patch_stamp = 0

    def _route(self, text, event_id, stamp, occurred_at):
        self._patch_text, self._patch_stamp = text, stamp
        pending = self.state.pending
        if pending and pending.kind == 'conversation' and pending.payload.get('patch_schema') != VERSION:
            # Reversible adoption: retain facts, invalidate old review/approval.
            pending.payload.update(patch_schema=VERSION, ready=False)
            pending.payload.pop('preview_digest',None); pending.token=secrets.token_hex(4)
            if re.fullmatch(self.confirmation,text,re.I):
                if stamp <= pending.source_timestamp: return Result('stale','คำตอบนี้เกิดก่อนร่างล่าสุด')
                pending.source_timestamp=stamp
                return self._show(pending,self._taxonomy(),'กรุณาตรวจสอบร่างรุ่นนี้ก่อนยืนยันอีกครั้ง')
        return super()._route(text,event_id,stamp,occurred_at)

    def _show(self, pending, taxonomy, prefix=''):
        if pending.kind == 'conversation': pending.payload['patch_schema'] = VERSION
        return super()._show(pending,taxonomy,prefix)

    def loop_context(self, taxonomy):
        result = super().loop_context(taxonomy)
        result['choices'] = super()._context(taxonomy,self._patch_text)['choices']
        return result

    def _failed_proposal(self, code, taxonomy, *, preserve_pending=False):
        pending = self.state.pending
        if preserve_pending:
            return {'ok':False,'error':{'code':code}},Result('clarification','ยังตอบคำขอนี้ไม่ได้ กรุณาระบุให้ชัดเจนขึ้น ร่างเดิมยังอยู่')
        if pending and pending.kind == 'conversation':
            pending.payload.update(ready=False,editing=True); pending.payload.pop('preview_digest',None)
            pending.source_timestamp=self._patch_stamp; pending.token=secrets.token_hex(4)
            result=self._show(pending,taxonomy,'ข้อมูลที่เสนอไม่ผ่านการตรวจสอบ กรุณาระบุข้อมูลที่ต้องการแก้')
        else:
            question='ยังสร้างร่างไม่ได้ กรุณาส่งชื่อรายการ จำนวนเงิน และข้อมูลที่ต้องการเพิ่ม หรือพิมพ์ ยกเลิก'
            if pending and pending.kind=='tool_source': pending.payload['loop_context']={'awaiting':None,'question':question}
            result=Result('clarification',question)
        return {'ok':False,'error':{'code':code}},result

    def loop_fallback(self, status):
        if self.state.pending and self.state.pending.kind=='conversation':
            self._failed_proposal(status,self._taxonomy())
        return super().loop_fallback(status)

    def _date_patch(self, edit, proposal, text, at, taxonomy):
        """Ground normalized parts in actual source, never in an ISO hint."""
        from .domain_runtime import relative_parts
        patched=[];pending=self.state.pending
        for patch,wire in zip(edit.entries,proposal.entries):
            original=None
            if pending and pending.kind=='conversation' and patch.entry_id in self._entry_ids(pending):
                original=pending.payload['entries'][self._entry_ids(pending).index(patch.entry_id)]
            known=dict(original['date_parts']) if original else {}
            if original and original['date']:
                known=dict(zip(('year','month','day'),map(int,original['date'].split('-'))))
            values={k:getattr(patch,k) for k in ['day','month','year']}
            for key,value in values.items():
                normalized=value-543 if key=='year' and value is not None and value>=2400 else value
                if normalized is not None and normalized==known.get(key):values[key]=None
            if not any(v is not None for v in values.values()):
                patched.append(patch.model_copy(update={**values,'date_source':None}));continue
            retained=pending.payload['source'] if pending and pending.kind=='tool_source' and edit.mode=='create' else None
            candidates=[(text,at)]+([(retained,__import__('datetime').datetime.fromisoformat(pending.payload['source_at']))] if retained else [])
            witnesses=[]
            for source,reference in candidates:
                command=command_text(source)
                names=[r.item for r in taxonomy.rows]+[v for v in [patch.item,original['item'] if original else None] if v]
                spans=[m.span() for name in names for m in re.finditer(re.escape(name),command)]
                clean=erase(command,spans)
                markers=[m.span() for m in re.finditer(r'วันที่\s*\d{1,2}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|เดือน\s*\d{1,2}|ปี\s*\d{4}|พ\.?ศ\.?\s*\d{4}|ค\.?ศ\.?\s*\d{4}|เมื่อวานซืน|เมื่อวาน|วานนี้|วันนี้|เดือนก่อน|เดือนที่แล้ว|เดือนนี้|ปีที่แล้ว|ปีก่อน|ปีนี้',clean)]
                months=list(re.finditer(MONTH_EVIDENCE,clean,re.I));markers += [m.span() for m in months]
                for m in months:
                    day=re.search(r'(\d{1,2})\s*$',clean[:m.start()])
                    if day:markers.append(day.span())
                for m in months:
                    year=re.match(r'\s*\d{4}(?!\d)(?!\s*บาท)',clean[m.end():])
                    if year:markers.append((m.end()+year.start(),m.end()+year.end()))
                if not markers and clean.strip().isdigit() and self.loop_context(taxonomy)['awaiting'] and self.loop_context(taxonomy)['awaiting']['field']=='month':
                    markers=[(clean.index(clean.strip()),clean.index(clean.strip())+len(clean.strip()))]
                if markers:
                    start=min(a for a,b in markers);end=max(b for a,b in markers)
                    witnesses.append((command[start:end],reference,months))
            explicit=wire.date_evidence
            if explicit:
                witnesses=[w for w in witnesses if explicit in w[0]]
                if not witnesses:raise ToolError('date_evidence_not_in_owner_source')
            if not witnesses:raise ToolError('date_evidence_missing')
            witness,reference,months=witnesses[0]
            # Current follow-up is authoritative; history can supply known
            # original parts but cannot override an explicit changed component.
            relative=relative_parts(witness,reference)
            month_match=re.search(MONTH_EVIDENCE,witness,re.I)
            numeric_month=re.search(r'เดือน\s*(\d{1,2})(?!\d)',witness)
            if values['month'] is not None and numeric_month and values['month']!=int(numeric_month[1]):
                raise ToolError('date_parts_mismatch')
            if values['month'] is not None and month_match:
                key=month_match[0].replace('.','').lower()
                month=MONTH_LOOKUP.get(key,MONTH_WORDS.get(key))
                if values['month']!=month:raise ToolError('date_parts_mismatch')
            if values['year'] is not None and not re.search(r'\d{4}',witness) and not (relative and 'year' in relative):
                # The existing default year is accepted only for a new/partial
                # date with no known year, never to overwrite a historical year.
                if 'year' not in known and values['year']==reference.astimezone(BANGKOK).year:values['year']=None
            if retained and values['day'] is not None and witness in command_text(text):
                day=re.search(r'วันที่\s*(\d{1,2})',command_text(retained))
                if day and values['day']==int(day[1]) and not re.search(r'\d',witness):values['day']=None
            patched.append(patch.model_copy(update={**values,'date_source':witness}))
        return edit.model_copy(update={'entries':patched})

    def _context_refs(self, edit, taxonomy):
        patched=[];choices=self.loop.admitted_choices
        for patch in edit.entries:
            updates={}
            if patch.result_set_id is None:
                fresh=self._taxonomy()
                if fresh.rows!=taxonomy.rows:raise ToolError('context_choices_changed')
                for field,group,target in [('item_id','items','item'),('category_id','categories','category')]:
                    value=getattr(patch,field)
                    if value is None:continue
                    row=next((r for r in choices[group] if r['id']==value),None)
                    if not row:raise ToolError('unknown_context_choice')
                    label=fresh.items[value].item if field=='item_id' and value in fresh.items else fresh.categories.get(value)
                    if label!=row['label']:raise ToolError('context_choices_changed')
                    if getattr(patch,target) not in {None,label}:raise ToolError('result_label_mismatch')
                    updates.update({target:label,field:None})
                if patch.subcategory_id is not None:
                    category=patch.category_id
                    if category is None and patch.category is not None:
                        ids=taxonomy.category_matches(patch.category)
                        category=ids[0] if len(ids)==1 else None
                    if category is None and self.state.pending and self.state.pending.kind=='conversation':
                        ids=self._entry_ids(self.state.pending)
                        if patch.entry_id in ids:category=self.state.pending.payload['entries'][ids.index(patch.entry_id)]['category_id']
                    row=next((s for c in choices['categories'] if c['id']==category for s in c['subcategories'] if s['id']==patch.subcategory_id),None)
                    if not row or fresh.pairs.get(category,{}).get(patch.subcategory_id)!=row['label']:
                        raise ToolError('unknown_context_choice')
                    if patch.subcategory not in {None,row['label']}:raise ToolError('result_label_mismatch')
                    updates.update(subcategory=row['label'],subcategory_id=None)
            patched.append(patch.model_copy(update=updates))
        return edit.model_copy(update={'entries':patched})

    def _lookup_result(self):
        labels=[r['label'] for saved in self._search_sets.values() for r in saved['matches']]
        text='ชื่อที่พบ: '+', '.join(labels)+'\nกรุณาระบุชื่อรายการและจำนวนเงิน' if labels else 'ไม่พบชื่อที่ตรง กรุณาระบุชื่อรายการและจำนวนเงิน'
        return {'ok':True,'status':'clarification'},Result('clarification',text)

    def _normalize_empty_note_clears(self, proposal, text):
        """A clear that removes no supplied/validated note is a harmless keep."""
        pending=self.state.pending
        ids=self._entry_ids(pending) if pending and pending.kind=='conversation' else []
        entries=[]
        for entry in proposal.entries:
            if any(c.field=='note' and c.op=='clear' for c in entry.changes):
                original=pending.payload['entries'][ids.index(entry.entry_id)] if entry.entry_id in ids else None
                item=original['item'] if original else None
                evidence=section(text,item)
                requested=bool(CLEAR.fullmatch(text.strip())) or evidence==('',True)
                if not requested:
                    retained=section(pending.payload['source'],item) if pending and pending.kind=='tool_source' and proposal.mode=='create' else None
                    if (original and original['note']) or (evidence and evidence[0]) or (retained and retained[0]):
                        raise ToolError('note_clear_not_requested')
                    # Only the operation is neutralized; no note text, source,
                    # target, amount or other missing field is synthesized.
                    changes=[c.model_copy(update={'op':'unchanged','value':None}) if c.field=='note' and c.op=='clear' else c for c in entry.changes]
                    entry=entry.model_copy(update={'changes':changes})
            entries.append(entry)
        return proposal.model_copy(update={'entries':entries})

    def domain_call(self, call, text, stamp, at, taxonomy):
        if call['name'] != NAME: return self._failed_proposal('wrong_patch_function',taxonomy)
        proposal=None
        try:
            proposal=Proposal.model_validate(json.loads(call['arguments']))
            if proposal.intent=='draft':
                if not proposal.entries:raise ToolError('draft_fields_missing')
                if proposal.mode is None:
                    proposal=proposal.model_copy(update={'mode':'update' if self.state.pending and self.state.pending.kind=='conversation' else 'create'})
                proposal=self._normalize_empty_note_clears(proposal,text)
                if proposal.mode=='update' and all(c.op=='unchanged' for entry in proposal.entries for c in entry.changes) and re.search(r'แก้|เปลี่ยน|ยอดจริง',command_text(text)):
                    raise ToolError('requested_edit_missing')
                edit=self._context_refs(proposal.edit(),taxonomy)
                edit=self._date_patch(edit,proposal,text,at,taxonomy)
                outcome=self._edit(edit,text,stamp,at,taxonomy)
                return outcome,self._show(self.state.pending,taxonomy)
            if proposal.intent=='summary':
                if self._search_sets and proposal.start_date is None and proposal.end_date is None and not re.search(r'สรุป|ยอดรายจ่าย|summary',command_text(text),re.I):
                    return self._lookup_result()
                evidence=dates(command_text(text),at)
                if evidence.value and proposal.start_date is None and proposal.end_date is None:
                    proposal=proposal.model_copy(update={'start_date':evidence.value.isoformat(),'end_date':evidence.value.isoformat()})
                if evidence.value and (proposal.start_date!=evidence.value.isoformat() or proposal.end_date!=evidence.value.isoformat()):
                    raise ToolError('summary_range_mismatch')
                routed={'name':'summary_query','arguments':json.dumps({'start_date':proposal.start_date,'end_date':proposal.end_date})}
                outcome,result=super().domain_call(routed,text,stamp,at,taxonomy)
                return (outcome,result) if result else self._failed_proposal(outcome['error']['code'],taxonomy,preserve_pending=True)
            if proposal.intent=='lookup':
                if self._search_sets: raise ToolError('lookup_limit')
                if proposal.lookup is None:raise ToolError('lookup_query_missing')
                lookup=proposal.lookup;name='catalog_search' if lookup.kind=='item' else 'category_search'
                outcome=self._search(name,Search(query=lookup.query,offset=lookup.offset),taxonomy)
                if re.match(r'^(?:ค้นหา|ขอดู|ดูหมวด|ดูรายการ)',text):return self._lookup_result()
                admitted=self.loop.admitted_choices['items' if lookup.kind=='item' else 'categories']
                if outcome['matches'] and all(v['id'] in {c['id'] for c in admitted} for v in outcome['matches']):
                    raise ToolError('lookup_already_in_context')
                return outcome,None
            pending=self.state.pending
            questions={'item':'รายการชื่ออะไร?', 'amount':'จำนวนเงินเท่าไร?', 'date':'รายการนี้เป็นวันที่เท่าไร?',
                       'category':'ต้องการใช้หมวดใดที่มีอยู่แล้ว?', 'subcategory':'ต้องการใช้หมวดย่อยใดที่มีอยู่แล้ว?'}
            if pending and pending.kind=='conversation':
                if proposal.question_field:
                    pending.payload.update(ready=False,editing=True);pending.payload.pop('preview_digest',None)
                    pending.source_timestamp=stamp;pending.token=secrets.token_hex(4)
                    result=self._show(pending,taxonomy)
                    question=questions.get(proposal.question_field,'ต้องการแก้รายการใดหรือข้อมูลใด?')
                    index=pending.payload.get('focus_index',0)
                    pending.payload['loop_context']={'awaiting':{'field':proposal.question_field,'entry_id':self._entry_ids(pending)[index]},'question':question}
                    choices=[(b['action']['label'],*b['action']['data'].split('|')[3:]) for b in result.data['line_message']['contents']['footer']['contents']]
                    result.data['line_message']=message(question,pending,choices)
                    result=Result('clarification',question,result.data)
                else:result=self._show(pending,taxonomy)
                return {'ok':True,'status':result.status},result
            if self._search_sets:
                return self._lookup_result()
            return {'ok':True,'status':'clarification'},Result('clarification',questions.get(proposal.question_field,'กรุณาระบุรายการและข้อมูลที่ต้องการให้ชัดเจนขึ้น'))
        except (ToolError,NoteError) as error:
            return self._failed_proposal(error.code,taxonomy,preserve_pending=proposal is not None and proposal.intent in {'summary','lookup'})
        except (ValueError,TypeError,KeyError):
            return self._failed_proposal('invalid_patch',taxonomy,preserve_pending=proposal is not None and proposal.intent in {'summary','lookup'})
