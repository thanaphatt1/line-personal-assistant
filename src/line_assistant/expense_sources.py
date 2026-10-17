"""Shared original-coordinate roles and clause scope for owner expense evidence.

Counts, dates, item labels and money are separate roles. A date can be global
only before the item group or with an explicit group quantifier. An unmarked
last-clause date leaves earlier undated distinct items unresolved, never guessed.
"""
import re
from .language import amounts,DIGITS
from .history_dates import cues
from .domain_notes import LABEL

UNITS=r'รอบ|เที่ยว|ครั้ง|ขา|รายการ|ยอด'
COUNTS={'หนึ่ง':1,'สอง':2,'สาม':3,'สี่':4,'ห้า':5}
COUNT=re.compile(r'(?<![\d.])([+-]?\d+(?:\.\d+)?|หนึ่ง|สอง|สาม|สี่|ห้า)\s*(?:'+UNITS+r')(?:(?![ก-ฮ])|(?=นะ|ด้วย|ให้|ช่วย|ครับ|ค่ะ|คะ|วันที่|ทั้ง|โดย))')
PER_UNIT=re.compile(r'(?:'+UNITS+r')ละ|ต่อ(?:'+UNITS+r')')
GROUP=re.compile(r'วัน(?:ที่)?เดียวกัน(?:คือ)?|ทั้งหมด|ทุกรายการ|ทั้ง(?:สอง|2|๒)(?:รายการ)?|ทั้งคู่')
JOIN=re.compile(r'กับ|และ|;|,(?!\d)')

def mask(text,spans):
    view=list(text)
    for a,b in spans:view[a:b]=' '*(b-a)
    return ''.join(view)

def roles(text,at,name_spans=()):
    """Return all role spans without rewriting or discarding numeric evidence."""
    _,dates,issue=cues(mask(text,name_spans),at)
    groups=[m.span() for m in GROUP.finditer(mask(text,name_spans))]
    counts=[m for m in COUNT.finditer(text.translate(DIGITS))
            if not any(m.start()<b and a<m.end() for a,b in [*name_spans,*dates,*groups])]
    values=amounts(text,[*name_spans,*dates,*groups,*(m.span() for m in counts)])
    count_values=[COUNTS.get(m[1],int(m[1]) if m[1].isdigit() else 0) for m in counts]
    count_values=[0 if re.search(r'ลบ\s*$',text[:m.start()]) else v for m,v in zip(counts,count_values)]
    return {'dates':dates,'date_issue':issue,'counts':counts,'count_values':count_values,'money':values}

def clauses(text,at,items,find):
    """Produce same-length masked item views, plus a precise date-scope issue."""
    note=LABEL.search(text)
    limit=note.start() if note else len(text)
    candidates=[(m.start(),m.end(),name) for name in set(v for v in items if v)
                for m in find(text,name) if m.end()<=limit]
    names=[]
    for span in sorted(candidates,key=lambda s:(-(s[1]-s[0]),s[0],s[2])):
        if not any(span[0]<b and a<span[1] for a,b,_ in names):names.append(span)
    names.sort()
    if len(names)<2:return [{'view':text,'item':names[0][2] if names else None,'date_issue':None}]
    boundaries=[0]
    for before,after in zip(names,names[1:]):
        links=list(JOIN.finditer(text,before[1],after[0]))
        boundaries.append(links[-1].end() if links else after[0])
    boundaries.append(len(text))
    leading=cues(text[:names[0][0]],at)[1]
    local=[]
    for i,(a,b,name) in enumerate(names):
        start,end=boundaries[i],boundaries[i+1]
        ds=cues(mask(text,[(0,start),(end,len(text)),(a,b)]),at)[1]
        local.append([span for span in ds if span not in leading])
    # A group quantifier must occur outside labels and notes and in the final
    # clause with the date. Incidental prose/another source never propagates it.
    last_start=boundaries[-2]
    distinct=len({name.casefold() for _,_,name in names})>1
    shared=bool(local[-1] and (not distinct or GROUP.search(mask(text[:limit],[(a,b) for a,b,_ in names]),last_start)))
    suffix=bool(local[-1] and all(a>=names[-1][1] for a,b in local[-1]))
    result=[]
    for i,(a,b,name) in enumerate(names):
        start,end=boundaries[i],boundaries[i+1]
        keep=[(start,end)]
        if leading:keep+=leading
        if shared:
            if local[i] and i!=len(names)-1:
                # Explicit per-item and shared dates must agree at validation.
                keep+=local[-1]
            elif not local[i]:keep+=local[-1]
        view=mask(text,[(j,j+1) for j in range(len(text)) if not any(x<=j<y for x,y in keep)])
        if local[i] and leading and i>0:
            prefix_parts=cues(text[:names[0][0]],at)[0]
            local_parts=cues(mask(text,[(0,start),(end,len(text)),(a,b)]),at)[0]
            if set(prefix_parts)&set(local_parts):view=mask(view,leading)
        view=GROUP.sub(lambda m:' '*len(m[0]),view)
        unresolved=distinct and suffix and not shared and not leading and not local[i]
        result.append({'view':view,'item':name,'date_issue':'ambiguous_date_scope' if unresolved else None,
                       'scope_date_text':[text[x:y] for x,y in local[-1]] if unresolved else []})
    return result
