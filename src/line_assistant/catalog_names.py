"""Catalog identity proposals stay separate from literal owner name evidence."""
from .catalog import normalized
from .source_spans import finditer
from .conversation_evidence import command_text
from .catalog import ItemCatalog
from .language import expense_source
from datetime import datetime


def spans(text,item,catalog):
    match=catalog.match(item or '')
    if match.status!='exact':return finditer(text,item)
    # A canonical name may use an original whitespace variant only when the
    # original itself resolves to that identity. Another exact name wins.
    return [m for m in finditer(text,item,whitespace=True)
            if catalog.resolve(m.group()).canonical==match.canonical]


def identity(item,catalog):
    match=catalog.resolve(item or '')
    return match.canonical or normalized(item or '')


def plan(value,sources,catalog):
    literal=value.item_text if value.item_text is not None else value.item
    if not literal:return {'literal':literal,'canonical':None,'candidates':[],'proposal':None}
    matches=[m for row in sources for m in finditer(command_text(row['text']),literal)]
    if not matches and (value.item_text is not None or catalog.resolve(literal).canonical):
        matches=[m for row in sources for m in finditer(command_text(row['text']),literal,whitespace=True)]
        if not matches and value.item_text is None:
            # Compatibility for a proposed catalog correction without an
            # explicit quote: only the existing complete source grammar may
            # recover an original name. Never fuzzy-search arbitrary prose.
            originals=[]
            for row in sources:
                parsed=expense_source(command_text(row['text']),datetime.fromisoformat(row['at']),ItemCatalog([],{}))
                if parsed:originals.extend(e['item'] for e in parsed.entries if e['item'] and e['amount'] is not None)
            names=list(dict.fromkeys(originals))
            target=catalog.resolve(literal).canonical
            related=[name for name in names if catalog.resolve(name).canonical==target or target in catalog.resolve(name).suggestions]
            if len(names)==1:literal=names[0]
            elif len(related)==1:literal=related[0]
            matches=[m for row in sources for m in finditer(command_text(row['text']),literal)]
    if matches:
        # The model can copy a surface label or propose a real catalog label.
        # Coordinates/proof always refer to the original surface, never to a
        # rewritten source string. Different mentions need explicit item_text.
        surfaces={normalized(m.group()) for m in matches}
        if len(surfaces)==1:literal=matches[0].group()
    matched=catalog.resolve(literal)
    proposed=catalog.match(value.item or '')
    proposal=proposed.canonical if proposed.status=='exact' else None
    candidates=list(matched.suggestions)
    if matched.canonical is None and proposal and proposal not in candidates:candidates.insert(0,proposal)
    return {'literal':literal,'canonical':matched.canonical,'candidates':candidates,'proposal':proposal}


def choices(entry,catalog):
    suggested=[*entry.get('name_candidates',[]),*catalog.resolve(entry['item']).suggestions]
    result=[]
    for name in suggested:
        match=catalog.match(name)
        if match.status=='exact' and match.canonical not in result:result.append(match.canonical)
    return result


def clarification(h,output):
    if output.get('validation_issues'):return None
    view=next((v for v in h._workflow() if v['draft_ref']==output.get('draft_ref')),None)
    pending=[r for r in view['item_resolutions'] if r['step']=='choose_existing_or_new'] if view else []
    if not pending:return None
    selected=next((r for r in pending if r['entry_ref']==output.get('catalog_entry_ref')),pending[0])
    offered=selected['existing_item_choices'];names=[c['item'] for c in offered]
    label=selected['item']
    if len(names)==1:question=f'ชื่อ {label} หมายถึง {names[0]} ใช่ไหมครับ หรือเป็นรายการใหม่?'
    elif names:question=f'ชื่อ {label} หมายถึงชื่อไหนครับ: '+ ' / '.join(names)+' หรือเป็นรายการใหม่?'
    else:question=f'ชื่อ {label} ตรงกับชื่อเดิมใน Database หรือเป็นรายการใหม่ครับ?'
    rows='\n'.join(f"{e['item'] or '(ยังไม่ระบุชื่อ)'} {e['amount'] or '?'} บาท วันที่ {e['date'] or 'ยังไม่ระบุ'}" for e in output['entries'])
    enhanced={**output,'catalog_matches':offered,'catalog_query':label,'catalog_entry_ref':selected['entry_ref']}
    return enhanced,'ร่างที่ยังไม่บันทึก:\n'+rows+'\n'+question


def focused_choice(h,d,entry):
    # A failed attempt or an informational reply must not erase a still-valid
    # delivered question. Use its entry+version, not merely the label (several
    # unresolved rows may share a spelling).
    from .semantic_tools import message_hash,rendered_message
    for row in reversed(h.state.history):
        if row['role']!='assistant':continue
        receipt=h.state.receipts.get(row['event'])
        if not receipt or receipt.expires_at<=h.clock():continue
        output=receipt.result.data.get('tool_result',{});target=output.get('catalog_entry_ref')
        pending=h.state.drafts.get(output.get('draft_ref'))
        if not target or not pending or pending['version']!=output.get('draft_version'):continue
        if getattr(h,'production',False):
            reply=h.state.replies.get(row['event'])
            if not reply or reply.status!='sent' or reply.content_hash!=message_hash(rendered_message(receipt.result)):continue
        return pending['draft_ref']==d['draft_ref'] and target==entry['entry_ref']
    return None
