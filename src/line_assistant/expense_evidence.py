"""Owner-source occurrence evidence; labels are never transaction identities."""
from collections import Counter
from datetime import datetime
import re
from .history_context import digest
from .history_dates import cues
from .language import amounts,intent_guard,FILLER,DIGITS
from .conversation_evidence import command_text
from .source_spans import finditer,contains
from .tool_errors import PlanningError

from .expense_sources import COUNTS,COUNT,PER_UNIT,GROUP,SELECTOR,roles,clauses
ORDINALS={'แรก':0,'หนึ่ง':0,'สอง':1,'สาม':2,'สี่':3,'ห้า':4}

def canonical(proposed,sources,tax):
    if not proposed:return proposed
    match=tax.catalog.match(proposed)
    if match.status=='exact':return match.canonical
    # Derive the label/count boundary from original catalog and role spans,
    # even when item_text accidentally includes only the count's digits.
    # A real full catalog identity always wins above; no arbitrary suffix trim.
    for row in sources:
        text=command_text(row['text']);known=item_mentions(text,tax)
        facts=roles(text,datetime.fromisoformat(row['at']),[(a,b) for a,b,name in known])
        if not facts['money'] or not all(v.value is not None for v in facts['money']):continue
        for mention in finditer(text,proposed):
            for a,b,name in known:
                for count in facts['counts']:
                    if (mention.start()==a and b<mention.end()<=count.end() and b<=count.start()
                        and re.fullmatch(r'\s*(?:ลบ\s*)?',text[b:count.start()])):return text[a:b]
    # ค่า is a possible expense introducer, never a blanket catalog rewrite.
    candidate=proposed[3:] if proposed.startswith('ค่า') else None
    if candidate and tax.catalog.match(candidate).status=='exact' and any(contains(command_text(r['text']),candidate) for r in sources):return tax.catalog.match(candidate).canonical
    return proposed

def item_mentions(text,tax):
    """Prefer a full known label over a catalog label contained inside it."""
    clean=command_text(text);spans=[]
    from .catalog_names import spans as name_spans
    for row in tax.rows:
        spans.extend((m.start(),m.end(),row.item) for m in name_spans(clean,row.item,tax.catalog))
    accepted=[]
    for span in sorted(spans,key=lambda v:v[1]-v[0],reverse=True):
        if not any(span[0]<b and a<span[1] for a,b,_ in accepted):accepted.append(span)
    return accepted

def literal_items(text,tax):return {name for _,_,name in item_mentions(text,tax)}

def connector(text):
    text=COUNT.sub(' ',text.translate(DIGITS))
    text=re.sub(r'[—–!?！？"“”]|ให้ที|ลงแยก(?=\s|$)|มี(?=\s|[,;:]|$)|ช่วย(?=\s*บันทึก)',' ',text)
    text=re.sub(r'อีก|เพิ่ม',' ',PER_UNIT.sub(' ',text))
    return bool(re.fullmatch(FILLER,text))


def price_counts(text, values, facts):
    """Bind local repetition counts to their adjacent original price spans.

    Several price/count cells disambiguate per-price repetition. An optional
    leading group count must equal their sum. A lone price still requires an
    explicit per-unit basis, and unrelated prose/totals cannot be separators.
    """
    counts=facts['counts'];numbers=facts['count_values']
    if any(not 1<=v<=5 for v in numbers):raise ValueError('unsupported_expense_count')
    if len(counts)<=1:return (numbers[0] if numbers else None),[1]*len(values),False
    local=[]
    for i,value in enumerate(values):
        stop=values[i+1].start if i+1<len(values) else len(text)
        matches=[j for j,c in enumerate(counts) if value.end<=c.start()<stop
                 and re.fullmatch(r'\s*(?:บาท\s*)?',text[value.end:c.start()])]
        if len(matches)!=1:raise ValueError('unsupported_expense_count')
        local.append(matches[0])
    if len(values)<2:raise ValueError('unsupported_expense_count')
    other=[j for j in range(len(counts)) if j not in local]
    total=sum(numbers[j] for j in local)
    if total>5:raise ValueError('unsupported_expense_count')
    if len(other)>1 or other and (counts[other[0]].end()>values[0].start or numbers[other[0]]!=total):
        raise ValueError('count_amount_mismatch')
    for i in range(len(values)-1):
        gap=text[counts[local[i]].end():values[i+1].start]
        if not re.fullmatch(r'\s*(?:(?:กับ|และ|,|/)\s*)?',gap):raise ValueError('count_price_basis_ambiguous')
    return total,[numbers[j] for j in local],True

def occurrences(row,item,bound=False,*,catalog=None,items=(),interpreted=False):
    """Return independently selectable monetary occurrences and masked views.

    Views mask other amounts/counts at original coordinates; persisted evidence
    always refers to the immutable original source, span and explicit ordinal.
    """
    if not item or not interpreted and intent_guard(row['text']):return []
    result=[];offset=0;at=datetime.fromisoformat(row['at'])
    originals=[];line_offset=0
    from .catalog_names import spans as catalog_spans
    for original in row['text'].splitlines(keepends=True):
        line=original.rstrip('\r\n')
        find=lambda text,name:catalog_spans(text,name,catalog) if catalog else finditer(text,name)
        labels=[item,*items,*(catalog.items.values() if catalog else [])]
        originals.extend((part['view'],line_offset) for part in clauses(line,at,labels,find)
                         if find(part['view'],item) or bound and (part['item'] is None or SELECTOR.search(part['view'])))
        line_offset+=len(original)
    for line,offset in originals:
        text=line.translate(DIGITS)
        if catalog is None:names=finditer(text,item)
        else:
            from .catalog_names import spans
            names=spans(text,item,catalog)
        overlap=lambda match:any(match.start()<n.end() and n.start()<match.end() for n in names)
        selectors=[m for m in SELECTOR.finditer(text) if not overlap(m)]
        if not names and not bound:continue
        facts=roles(text,at,[m.span() for m in names]);dates=facts['dates']
        groups=[m for m in GROUP.finditer(text) if not overlap(m)]
        counts=facts['counts']
        spans=[m.span() for m in names]+[m.span() for m in counts]+[m.span() for m in selectors]+[m.span() for m in groups]+dates
        parsed=amounts(text,spans,list_separators=True)
        # An invalid numeric cell is not evidence that the other cells form a
        # complete group. In particular, never drop a malformed first price.
        if any(v.value is None for v in parsed):continue
        values=parsed
        if not values:continue
        count,repetitions,local_counts=price_counts(text,values,facts)
        if any(re.match(r'ทั้ง(?:สอง|2)',m[0]) for m in groups):
            if count is not None and count!=2:raise ValueError('count_amount_mismatch')
            count=2
        if len(values)>1 and (len(names)==1 or not names and not selectors):
            separators=[text[a.end:b.start] for a,b in zip(values,values[1:])]
            if not local_counts and not all(re.fullmatch(r'\s*(?:บาท\s*)?(?:กับ|และ|,|/|อีก\s*(?:หนึ่ง\s*)?(?:รายการ|ยอด))\s*',s) for s in separators):continue
            if not local_counts and count is not None and count!=len(values):raise ValueError('count_amount_mismatch')
        if len(names)>1:
            if any(not any(m.end()<=v.start and (i+1==len(names) or v.end<=names[i+1].start()) for i,m in enumerate(names)) for v in values):continue
        per_unit=bool(PER_UNIT.search(text))
        if count and count>1 and len(values)==1 and not per_unit:raise ValueError('count_price_basis_ambiguous')
        if len(values)==1 and count and per_unit:repetitions=[count]
        for value_index,value in enumerate(values):
            for ordinal in range(repetitions[value_index]):
                masked=list(line)
                for m in counts:
                    masked[m.start():m.end()]=' '*(m.end()-m.start())
                for m in [*selectors,*groups]:masked[m.start():m.end()]=' '*(m.end()-m.start())
                for other in values:
                    if other is not value:masked[other.start:other.end]=' '*(other.end-other.start)
                if len(values)>1:
                    for before,after in zip(values,values[1:]):
                        masked[before.end:after.start]=' '*(after.start-before.end)
                # A repeated literal name on a different line is naturally scoped.
                # On one line, mask unrelated same-name clauses as well.
                if len(names)>1:
                    owner=max((i for i,m in enumerate(names) if m.end()<=value.start),default=None)
                    if owner is None:continue
                    for i,m in enumerate(names):
                        if i!=owner:masked[m.start():m.end()]=' '*(m.end()-m.start())
                proof={'source_ref':row['source_ref'],'span':[offset+value.start,offset+value.end],
                       'ordinal':ordinal,'amount':value.value,'at':row['at']}
                proof['occurrence_ref']='occ_'+digest(proof)[:20]
                start=values[value_index-1].end if value_index else 0
                targets=[m for m in selectors if start<=m.start() and m.end()<=value.start]
                target=(int(targets[0][1])-1 if targets[0][1].isdigit() else ORDINALS[targets[0][1]]) if len(targets)==1 else None
                result.append({**proof,'view':''.join(masked),'line_offset':offset,'target_index':target,'declared_count':count})
    return result

def expand_counted_entries(entries,source_lists,*,catalog,interpreted=False):
    """Complete a model's single item proposal only from a complete owned group.

    One explicit count must match the independently verified original prices
    (or an explicit per-unit rate). A proposed value must be a group price,
    omitted, or the independently identified count role; other prices reject.
    Totals, mismatches, multiple sources and already-expanded rows never qualify.
    Every resulting row still receives its own validated occurrence proof.
    """
    from .catalog_names import identity
    from decimal import Decimal
    groups=Counter(identity(e.item,catalog) for e in entries if e.item)
    result=[]
    for index,(entry,rows) in enumerate(zip(entries,source_lists)):
        proofs=[]
        if entry.item and groups[identity(entry.item,catalog)]==1:
            proofs=[v for row in rows for v in occurrences(row,entry.item,bound=bool(entry.field_sources and entry.field_sources.amount),catalog=catalog,items=[e.item for e in entries],interpreted=interpreted)]
        complete=(len(proofs)>1 and len({v['source_ref'] for v in proofs})==1
                  and all(v['declared_count']==len(proofs) for v in proofs))
        consistent=(entry.amount is None or any(Decimal(v['amount'])==Decimal(str(entry.amount)) for v in proofs)
                    or complete and Decimal(str(entry.amount))==Decimal(proofs[0]['declared_count']))
        if complete and consistent:
            result.extend((entry.model_copy(update={'amount':v['amount']},deep=True),rows,index) for v in proofs)
        else:result.append((entry.model_copy(deep=True),rows,index))
    if len(result)>5:raise ValueError('unsupported_expense_count')
    return result

def assign(entries,source_lists,targets=None,*,catalog=None,interpreted=False):
    """One amount occurrence per row, scoped to that row's cited originals."""
    from .catalog_names import identity
    key=lambda item:identity(item,catalog) if catalog is not None else item
    groups=Counter(key(e.item) for e in entries if e.item)
    if targets is None:
        # Even a model that returns only one row cannot silently erase an
        # explicit multiplicity. Share the same occurrence/count grammar used
        # by multirow allocation, rather than trusting proposed row count.
        for entry,rows in zip(entries,source_lists):
            if entry.item and groups[key(entry.item)]==1:
                proofs=[v for row in rows for v in occurrences(row,entry.item,bound=bool(entry.field_sources and entry.field_sources.amount),catalog=catalog,items=[e.item for e in entries],interpreted=interpreted)]
                if len(proofs)>1:raise ValueError('count_amount_mismatch' if any(v['declared_count'] for v in proofs) else 'expense_batch_incomplete')
    chosen={};used=set()
    for index,(entry,sources) in enumerate(zip(entries,source_lists)):
        if groups[key(entry.item)]<2 or entry.amount is None:continue
        wanted=str(entry.amount)
        try:
            from decimal import Decimal
            wanted=str(Decimal(wanted).normalize())
        except Exception:raise ValueError('amount_source')
        label=key(entry.item) if catalog is not None and catalog.resolve(entry.item or '').canonical else entry.item
        candidates=[v for r in sources for v in occurrences(r,label,bound=targets is not None or bool(entry.field_sources and entry.field_sources.amount),catalog=catalog,items=[e.item for e in entries],interpreted=interpreted) if str(Decimal(v['amount']).normalize())==wanted]
        candidate=next((v for v in candidates if v['occurrence_ref'] not in used and (targets is None or v['target_index']==targets[index])),None)
        if candidate is None:
            raise PlanningError('amount_occurrence_missing_or_reused',
                fields=[f'entries[{index}].amount'],required=['unused_original_amount_occurrence'])
        used.add(candidate['occurrence_ref']);chosen[index]=candidate
    return chosen

def selected_ordinals(text,size,items=()):
    values=set()
    text=text.translate(DIGITS)
    for item in sorted(set(v for v in items if v),key=len,reverse=True):
        for match in finditer(text,item.translate(DIGITS)):text=text[:match.a]+' '*(match.b-match.a)+text[match.b:]
    for m in SELECTOR.finditer(text):
        value=int(m[1])-1 if m[1].isdigit() else ORDINALS[m[1]]
        if value<size:values.add(value)
    return values

def catalog_mappings(entries):
    from .catalog import normalized
    result={}
    for entry in entries:
        mapping=entry.get('mapping')
        if not mapping:continue
        name=normalized(mapping['item'])
        if name in result and result[name]!=mapping:raise ValueError('conflicting_item_mapping')
        result[name]=mapping
    return list(result.values())
