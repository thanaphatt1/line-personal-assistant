"""Date defaults for the default-off prototype only; owner evidence, no model guesses."""
from datetime import date,timedelta
import re
from .language import BANGKOK,DIGITS,MONTH_LOOKUP,MONTH_PATTERN
from .draft_patch import MONTH_WORDS
from .domain_runtime import relative_parts

POLICY='bangkok-date-defaults-v02'
ALIASES='|'.join(re.escape(w)+r'(?![\u0e01-\u0e4eA-Za-z])' if not w.isascii() else r'\b'+re.escape(w)+r'\b' for w in sorted(MONTH_WORDS,key=len,reverse=True))
MONTH='(?:'+MONTH_PATTERN+'|'+ALIASES+')'
RELATIVE=r'เมื่อวานซืน|เมื่อวานนี้|เมื่อวาน|วานนี้|วันนี้|พรุ่งนี้|มะรืน(?:นี้)?|เดือนก่อน|เดือนที่แล้ว|เดือนนี้|ปีที่แล้ว|ปีก่อน|ปีนี้'
WEEKDAYS={'จันทร์':0,'อังคาร':1,'พุธ':2,'พฤหัส':3,'พฤหัสบดี':3,'ศุกร์':4,'เสาร์':5,'อาทิตย์':6}
PAST_WEEKDAY=re.compile(r'(?<![\u0e01-\u0e4eA-Za-z])(?:เมื่อ)?วัน(พฤหัสบดี|พฤหัส|จันทร์|อังคาร|พุธ|ศุกร์|เสาร์|อาทิตย์)\s*(?:ที่ผ่านมา|ที่แล้ว|ก่อน)(?![\u0e01-\u0e4eA-Za-z])')

def year(number):return number-543 if number>=2400 else number

def answer_text(text):
    """A bounded day answer, only for an already-bound missing-date question."""
    match=re.fullmatch(r'\s*([ก-ฮะ-์\s]*)\s*(\d{1,2})\s*(?:ครับ|ค่ะ|คะ)?\s*',text.translate(DIGITS))
    if not match or not 1<=int(match[2])<=31:return None
    prefix=''.join(match[1].split())
    def one_edit(a,b):
        if abs(len(a)-len(b))>1:return False
        if len(a)==len(b):return sum(x!=y for x,y in zip(a,b))<=1
        short,long=(a,b) if len(a)<len(b) else (b,a)
        return any(long[:i]+long[i+1:]==short for i in range(len(long)))
    if prefix and not any(one_edit(prefix,v) for v in ['วันที่','ลงวันที่','วัน']):return None
    return 'วันที่ '+match[2]

def cues(text,at):
    """Return explicit components and original-coordinate date spans."""
    text=text.translate(DIGITS);parts={};spans=[];issue=None
    def put(k,v,span):
        nonlocal issue
        if k in parts and parts[k]!=v:issue='ambiguous_date_source'
        parts[k]=v;spans.append(span)
    for m in re.finditer(r'(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)|(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{4}))?(?![\d/])',text):
        values=dict(zip(('year','month','day'),map(int,m.groups()[:3]))) if m[1] else {'day':int(m[4]),'month':int(m[5]),**({'year':int(m[6])} if m[6] else {})}
        for k,v in values.items():put(k,year(v) if k=='year' else v,m.span())
    masked=list(text)
    for a,b in spans:masked[a:b]='_'*(b-a)
    clean=''.join(masked)
    for m in re.finditer(r'วันที่\s*(\d{1,2})(?!\d)',clean):put('day',int(m[1]),m.span())
    for m in re.finditer(MONTH,clean,re.I):
        word=m[0].replace('.','').lower();number=MONTH_LOOKUP.get(word,MONTH_WORDS.get(word))
        put('month',number,m.span())
        before=re.search(r'(?<!\d)(\d{1,2})\s*$',clean[:m.start()])
        if before:put('day',int(before[1]),before.span())
        after=re.match(r'\s+(?:(?:พ\.?ศ\.?|ค\.?ศ\.?)\s*)?(\d{4})(?!\d)',clean[m.end():])
        if after:put('year',year(int(after[1])),(m.end()+after.start(),m.end()+after.end()))
    for m in re.finditer(r'เดือน\s*(\d{1,2})(?!\d)',clean):put('month',int(m[1]),m.span())
    for m in re.finditer(r'(?:ปี\s*(?:(?:พ\.?ศ\.?|ค\.?ศ\.?)\s*)?|พ\.?ศ\.?\s*|ค\.?ศ\.?\s*)(\d{4})(?!\d)',clean):put('year',year(int(m[1])),m.span())
    for m in PAST_WEEKDAY.finditer(clean):
        # "ที่ผ่านมา" names the most recent occurrence strictly before the
        # original owner turn, including across Bangkok midnight/year edges.
        today=at.astimezone(BANGKOK).date()
        value=today-timedelta(days=(today.weekday()-WEEKDAYS[m[1]])%7 or 7)
        for k,v in [('day',value.day),('month',value.month),('year',value.year)]:put(k,v,m.span())
    relative=list(re.finditer(RELATIVE,clean))
    if len(relative)>1:issue='ambiguous_date_source'
    if relative:
        m=relative[0];word=m[0];today=at.astimezone(BANGKOK).date()
        if word=='พรุ่งนี้' or word.startswith('มะรืน'):
            value=today+timedelta(days=1 if word=='พรุ่งนี้' else 2);values={'day':value.day,'month':value.month,'year':value.year}
        else:values=relative_parts(word,at)
        for k,v in (values or {}).items():put(k,v,m.span())
    remaining=list(text)
    for a,b in spans:remaining[a:b]='_'*(b-a)
    leftover=''.join(remaining)
    if re.search(r'วันที่\s*\d|\d+/\d|เดือน\s*(?=[ก-ฮ0-9])|สัปดาห์|วัน(?:จันทร์|อังคาร|พุธ|พฤหัส|ศุกร์|เสาร์|อาทิตย์)',leftover):issue=issue or 'unsupported_date_source'
    return parts,list(dict.fromkeys(spans)),issue

def resolve(units,old,current_ref,now):
    """Units already have relevant owner sources and item/note text masked."""
    known=old.get('date') if old else None
    evidence=dict(old.get('date_basis',{}).get('explicit_parts',{})) if old else {}
    defaults=dict(old.get('date_basis',{}).get('defaulted_parts',{})) if old else {}
    anchor=units[0][1] if units else now
    changed=False;changed_parts={};issue=None;refs=[]
    for text,at,ref in units:
        if old and ref!=current_ref:continue  # Previously validated facts are retained, not reinterpreted.
        parts,_,problem=cues(text,at)
        if not parts and not problem:continue
        changed=True;anchor=at;refs.append(ref)
        if problem:issue=problem
        changed_parts.update(parts)
        evidence.update(parts)
    if old and not changed:return known,dict(old.get('date_parts',{})),deepcopy_basis(old.get('date_basis')),old.get('date_issue')
    today=anchor.astimezone(BANGKOK).date()
    if not changed:
        evidence={};defaults={'year':today.year,'month':today.month,'day':today.day}
    else:
        # Explicit relevant context wins. Defaulted month/year from an old draft
        # do not become invented explicit evidence across month/year boundaries.
        defaults={'year':today.year,'month':today.month}
        if known and 'day' not in changed_parts:
            # A month/year-only correction changes those components of an
            # existing verified date; it does not reset its untouched day/month.
            previous=date.fromisoformat(known)
            defaults={'year':previous.year,'month':previous.month,'day':previous.day}
        elif 'day' not in evidence and known:defaults['day']=date.fromisoformat(known).day
    parts={**defaults,**evidence}
    basis={'policy':POLICY,'explicit_parts':evidence,'defaulted_parts':{k:v for k,v in defaults.items() if k not in evidence},
           'anchor_date':today.isoformat(),'source_refs':refs or ([units[0][2]] if units else [])}
    if known and changed and 'day' not in changed_parts:basis['preserved_from_draft']=True
    calculated=None
    if issue is None:
        if 'day' not in parts:issue='missing_day'
        else:
            try:
                if not 1900<=parts['year']<=2100:raise ValueError()
                calculated=date(parts['year'],parts['month'],parts['day']).isoformat()
            except (ValueError,KeyError):issue='invalid_calendar_date'
    if calculated and date.fromisoformat(calculated)>now.astimezone(BANGKOK).date():
        if set(evidence)=={'day'}:
            # The calendar date exists, but only its day was supplied. Keep
            # the source day and ask for a month rather than guessing backward.
            basis['default_month_candidate']=calculated
            basis['defaulted_parts'].pop('month',None)
            parts.pop('month',None)
            calculated=None;issue='missing_month_for_future_day'
        else:issue='future_expense_date'
    return calculated,parts if not calculated else {},basis,issue

def deepcopy_basis(value):
    from copy import deepcopy
    return deepcopy(value)
