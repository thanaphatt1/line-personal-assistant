"""Source units for bounded multi-appointment requests; model output is evidence only.

Unknown substantive clauses stay as explicit unresolved slots. A title/date from
one clause cannot be borrowed by another; explicitly shared facts remain shared.
"""
from copy import deepcopy
import re
from .calendar_dates import event_date,event_time,bangkok_zone,DAY
from .history_dates import MONTH
from .language import DIGITS
from .history_context import digest
from .calendar_event_types import ALL_DAY

DATE=re.compile(r'\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\s*/\s*\d{1,2}(?:\s*/\s*\d{2,4})?|(?:วันที่\s*)?\d{1,2}\s*(?:'+MONTH+r')(?:\s*(?:(?:พ\.?ศ\.?|ค\.?ศ\.?)\s*)?\d{4})?|(?:วัน)?(?:'+DAY+r')\s*(?:หน้า|นี้|ที่ผ่านมา|ที่แล้ว|ก่อน)|มะรืน|พรุ่งนี้|วันนี้|อีก\s*(?:[0-9๑-๙]+|หนึ่ง|สอง|สาม)\s*(?:วัน|สัปดาห์)|วันที่\s*\d{1,2}(?![\d:])',re.I)
CLOCK=re.compile(r'(?<![\d:])[0-9๐-๙]{1,2}[:.][0-9๐-๙]{2}(?:\s*(?:am|pm|น\.?))?(?![\d:])|บ่าย[ \t]*(?:[0-9๐-๙]+[ \t]*(?:โมง(?:ครึ่ง)?)?|[หนึ่งสองสามสี่ห้าหกเจ็ดแปดเก้าสิบครึ่งโมง]+)|(?:เก้า|แปด|สิบ|สิบเอ็ด|หก|เจ็ด)โมง(?:เช้า|ครึ่ง)?|(?:ห้า|สี่|หก)โมงเย็น|เที่ยง(?:คืน|วัน)?|(?:หนึ่ง|สอง|สาม|สี่|ห้า)ทุ่ม',re.I)
SHARED=re.compile(r'ทั้ง\s*(?:(?:สอง|2|๒)(?:\s*(?:วัน|นัด|รายการ))?|คู่|หมด|สาม(?:\s*(?:วัน|นัด|รายการ))?|สี่(?:\s*(?:วัน|นัด|รายการ))?)|ทุก(?:นัด|รายการ)|เหมือนกัน|เวลาเดียวกัน|วันเดียวกัน|\bboth(?:\s+(?:days|events|appointments))?\b|\ball\s+(?:events|appointments)\b|\bsame\s+(?:time|date)\b',re.I)
FILLER=re.compile(r'กันเวลา(?:ใน)?(?:ตาราง|ปฏิทิน)?|(?:ยังไม่(?:ต้อง)?|เดี๋ยว(?:ค่อย)?|ค่อย)(?:ใส่|ระบุ|กำหนด|บอก|เติม)(?:วัน(?:ที่)?|(?:ช่วง)?เวลา|ชื่อ)|(?:ยัง)?ไม่(?:รู้|ทราบ)(?:วัน(?:ที่)?|(?:ช่วง)?เวลา|ชื่อ)|(?:ลง|ใส่|จัด)(?:ใน)?(?:ตาราง|ปฏิทิน)(?:ไว้)?ก่อน|เพิ่ม(?:เข้า)?(?:ปฏิทิน|นัด)?|สร้าง(?:นัด)?|ลง(?:ปฏิทิน|ตาราง)?|ปฏิทิน|นัด(?:หมาย)?|กรุณา|รบกวน|ฝาก|บันทึก|จด|ขอให้|ขอ|ช่วย|อยาก|จัดตาราง|ไว้ก่อน|ไว้ให้หน่อย|ยังไม่ต้องระบุเวลา|จัด|จอง|ให้ที|ให้|หน่อย|ด้วย|ครับ|ค่ะ|คะ|นะ|สำหรับ|เริ่ม(?:เวลา)?|เวลา|ช่วง|ตั้งแต่|จนถึง|ถึง|สิ้นสุด|จบ|วันละ|ครั้งเดียว|กิจกรรม|ใส่(?:ตาราง|ปฏิทิน)?|ใน|รอบแรก|อีกรอบ|เข้าในชุดนี้|เข้าชุดนี้|ในชุดนี้|มี|ตอน|แล้วก็|แล้ว|รายการ(?:ที่)?\s*[1-8๑-๘]|ที่\s*[1-8๑-๘]|จำนวน\s*[1-8๑-๘]\s*(?:นัด|รายการ)|ทั้งสองวัน|ทั้งสองนัด|ทั้งคู่|ทั้งหมด|ทุกนัด|เหมือนกัน|เวลาเดียวกัน|วันเดียวกัน|และ|กับ|จาก|เป็น|อีกครั้ง|อีกวัน|อีก|(?:สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด)(?=นัด|รายการ)|[\s,;:!?！？.。|()\[\]"“”\-–—]+',re.I)

def clock_pair(text,matches):
    """Accept an explicit range or separately labelled start/end clocks."""
    if len(matches)!=2:return False
    first,last=matches
    gap=text[first.end():last.start()]
    if re.fullmatch(r'\s*(?:[-–—]|ถึง|จนถึง|to|until)\s*',gap,re.I):return True
    return bool(re.search(r'(?:เริ่ม(?:เวลา|ตอน)?|ตั้งแต่(?:เวลา)?)\s*$',text[:first.start()])
        and re.fullmatch(r'\s*(?:และ|,)?\s*(?:จบ|สิ้นสุด)(?:เวลา|ตอน)?\s*',gap))

def masked(text,spans):
    value=list(text)
    for a,b in spans:value[a:b]=' '*(b-a)
    return ''.join(value)
def explicit_count(text):
    text=masked(text,[m.span() for pattern in [DATE,CLOCK] for m in pattern.finditer(text)])
    match=re.search(r'(?<![0-9๐-๙:./+\-])(สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|[0-9๐-๙]+)\s*(?:นัด|รายการ|ครั้ง|appointments?)',text,re.I)
    if not match:return None
    words={'สอง':2,'สาม':3,'สี่':4,'ห้า':5,'หก':6,'เจ็ด':7,'แปด':8}
    count=words.get(match[1]) or int(match[1].translate(DIGITS))
    if not 1<=count<=8:raise ValueError('calendar_event_capacity')
    return count
def substantive(text,titles=(),*,facts=None):
    spans=[m.span() for title in titles if title for m in re.finditer(re.escape(title),text)]
    spans.extend(m.span() for m in re.finditer(r'รอบ(?=[ \t]*(?:วันที่|[0-9๐-๙]))',text) if not any(m.start()<b and a<m.end() for a,b in spans))
    date_view=masked(text,spans)
    spans.extend(m.span() for m in DATE.finditer(date_view))
    if facts:
        from .calendar_workflow import literal_fact
        for field,value in facts.items():
            if field not in {'date_text','end_date_text'} or not value:continue
            literal=literal_fact(value,text,field)
            if literal:spans.extend(m.span() for m in re.finditer(re.escape(literal),text))
    value=masked(text,spans)
    value=re.sub(r'รอบ(?=[ \t]*(?:วันที่|[0-9๐-๙]))',' ',value)
    try:value=bangkok_zone(value)
    except ValueError:pass
    value=re.sub(r'(?<=[ก-๙])(?=(?:ไว้ก่อน(?:นะ)?|ทั้งคู่|ด้วย|ครับ|ค่ะ|คะ|นะ|ให้ที)(?:[\s!?.。！？]|$))',' ',value)
    value=masked(value,[m.span() for pattern in [DATE,CLOCK] for m in pattern.finditer(value)])
    value=re.sub(r'[0-9๑-๙]+(?:\s*(?:ชั่วโมง|ชม\.?|นาที|น\.?))',' ',value)
    value=SHARED.sub(' ',value)
    value=ALL_DAY.sub(' ',value)
    value=re.sub(r'(?:สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|[0-9๐-๙]+)\s*(?:นัด|รายการ|ครั้ง|appointments?)',' ',value,flags=re.I)
    value=re.sub(r'\b(?:and|also|please|add|calendar|appointments?|on|from|to|until)\b',' ',value,flags=re.I)
    return FILLER.sub('',value).strip()

def units(row,titles):
    text=row['text'];spans=[m.span() for title in titles if title for m in re.finditer(re.escape(title),text)]
    view=masked(text,spans)
    cuts=[0];cut_view=list(view)
    # Protect numeric date slashes only. Masking all temporal text would hide
    # line breaks or merge two connectors across an otherwise masked event.
    for match in DATE.finditer(text):
        for index in range(*match.span()):
            if text[index]=='/':cut_view[index]=' '
    cut_view=''.join(cut_view)
    for match in re.finditer(r'\n|;|แล้วก็|ส่วน|แล้ว|(?:และ|กับ)(?:\s*และ)*|\band\b|,(?=\s*(?:วันที่|\d|นัด))|(?<=\s)/(?=\s)',cut_view,re.I):cuts.extend([match.start(),match.end()])
    cuts.append(len(text));out=[]
    for a,b in zip(cuts[::2],cuts[1::2]):
        part=text[a:b];known=[t for t in titles if t and t in part]
        if not part.strip():continue
        if not known and not substantive(part):
            temporal=bool(DATE.search(part) or CLOCK.search(part) or re.search(r'อีกนัด|อีกรอบ|อีกครั้ง',part))
            first_title=min((start for start,end in spans),default=len(text))
            common_prefix=b<=first_title and (len(list(DATE.finditer(text)))<=1 if DATE.search(part) else len(list(CLOCK.finditer(text)))<=2)
            purely_shared=SHARED.search(part) and (not DATE.search(part) or len(list(DATE.finditer(text)))<=1)
            if not temporal or purely_shared or common_prefix:continue
        # Several fully named events without a connector can still be divided
        # at their independent leading dates. Otherwise keep an unresolved unit.
        hits=sorted((m.start(),m.end(),t) for t in known for m in re.finditer(re.escape(t),part))
        boundaries=[a]
        for previous,current in zip(hits,hits[1:]):
            dates=[m for m in DATE.finditer(part,previous[1],current[0])]
            # A date immediately following the previous title belongs to it.
            # Only a new leading date after its completed clock range starts
            # the next unnamed clause here. Explicit connectors split above.
            clocks=list(CLOCK.finditer(part,previous[1],current[0]))
            prefix_dates=list(DATE.finditer(part,boundaries[-1]-a,previous[0]))
            leading=dates[-1] if dates and (prefix_dates or clocks and dates[-1].start()>clocks[-1].end()) else None
            boundaries.append(a+(leading.start() if leading else current[0]))
        boundaries.append(b)
        for start,end in zip(boundaries,boundaries[1:]):
            chunk=text[start:end]
            if not chunk.strip():continue
            out.append({'source_ref':row['source_ref'],'start':start,'end':end,'text':chunk,'at':row['at'],'occurrence':0,
                'slot_key':digest([row['source_ref'],start,end,0])[:20]})
    count=explicit_count(view)
    if count and count>len(out):
        for occurrence in range(len(out),count):
            out.append({'source_ref':row['source_ref'],'start':0,'end':len(text),'text':text,'at':row['at'],'occurrence':occurrence,
                'slot_key':digest([row['source_ref'],0,len(text),occurrence])[:20]})
    return out

def sharing(row,field,value,titles):
    """A group marker binds its nearby fact, not every fact in the message."""
    if field=='title':return False
    original=row['text']
    spans=[m.span() for title in titles if title for m in re.finditer(re.escape(title),original)]
    positions=[a for a,b in spans];text=masked(original,spans)
    pattern=DATE if field in {'date_text','end_date_text'} else ALL_DAY if field=='all_day' else CLOCK
    found=text.find(value)
    if len(positions)>=2 and 0<=found<min(positions):
        first=min(positions)
        if any(m.end()<=first for m in pattern.finditer(text)) and not any(m.start()>first for m in pattern.finditer(text)):
            return True
    markers=list(SHARED.finditer(text))
    if not markers:return False
    if field=='all_day':
        for hit in re.finditer(re.escape(value),text):
            for marker in markers:
                lo,hi=sorted((hit.end(),marker.start())) if hit.end()<=marker.start() else sorted((marker.end(),hit.start()))
                if not CLOCK.search(text[lo:hi]) and not DATE.search(text[lo:hi]) and not any(a<hi and b>lo for a,b in spans):return True
        return False
    if field in {'date_text','end_date_text'}:
        dates=list(DATE.finditer(text))
        if len({m[0].translate(DIGITS).replace(' ','') for m in dates})!=1:return False
        for date in dates:
            for marker in markers:
                lo,hi=(date.end(),marker.start()) if date.end()<=marker.start() else (marker.end(),date.start())
                if hi<lo:continue
                # An intervening clock range or event title means the marker
                # belongs to that event/time. It cannot share the earlier date.
                if CLOCK.search(text[lo:hi]) or any(a<hi and b>lo for a,b in spans):continue
                return True
        return False
    if field in {'time_text','end_time_text'}:
        clocks=list(CLOCK.finditer(text))
        return len(clocks)==1 or clock_pair(text,clocks)
    if field=='duration_text':
        from .calendar_workflow import literal_fact
        literal=literal_fact(value,text,field)
        if literal is None:return False
        for hit in re.finditer(re.escape(literal),text):
            for marker in markers:
                lo,hi=(hit.end(),marker.start()) if hit.end()<=marker.start() else (marker.end(),hit.start())
                if hi>=lo and not CLOCK.search(text[lo:hi]) and not DATE.search(text[lo:hi]) and not any(a<hi and b>lo for a,b in spans):return True
    return False

def repeated_names(edits,rows):
    """Repeated-occurrence annotations stay outside the common literal name.

    Only a group of new proposals sharing the same owner-written stem qualifies.
    Explicit quoted/named titles and unrelated event names remain unchanged.
    """
    result=list(edits);texts=[row['text'] for row in rows]
    def explicitly_named(title):
        return any(title in m[1] for text in texts for m in re.finditer(r'["“]([^"“”]+)["”]',text)) or any(re.search(r'ชื่อ(?:เรื่อง|ว่า)?\s*'+re.escape(title),text) for text in texts)
    for index,e in enumerate(edits):
        if e.draft_ref or not e.title or explicitly_named(e.title):continue
        request=re.match(r'(?:นัด|ลง|จด|เพิ่ม|จัด|ใส่|มี)(?=ตรวจ|คุย|ประชุม|ซ้อม|อบรม|เรียน|พบ)',e.title)
        if request:
            occurrences=[(text,m) for text in texts for m in re.finditer(re.escape(e.title),text)]
            if occurrences and all(not re.search(r'นัด\s*$',text[:m.start()]) for text,m in occurrences):
                result[index]=e.model_copy(update={'title':e.title[request.end():]})
    edits=result;groups={}
    for index,e in enumerate(edits):
        if e.draft_ref or not e.title:continue
        match=re.fullmatch(r'(.+?)(รอบแรก|อีกรอบ|อีกครั้ง|อีกวัน|อีกนัด)',e.title)
        stem=match[1].strip() if match else e.title;marker=match[2] if match else None
        if not match and e.title.endswith('รอบ') and any(re.search(re.escape(e.title)+r'\s*(?:วันที่|[0-9๐-๙])',text) for text in texts):stem=e.title[:-3].strip();marker='รอบ'
        groups.setdefault(stem,[]).append((index,e,marker))
    for stem,members in groups.items():
        if len(members)<2 or not any(marker for i,e,marker in members):continue
        texts=[row['text'] for row in rows]
        if not any(stem in text for text in texts):continue
        if any(marker and not any(marker in text for text in texts) for i,e,marker in members):continue
        quoted=[m[1] for text in texts for m in re.finditer(r'["“]([^"“”]+)["”]',text)]
        named=any(re.search(r'(?:ชื่อ(?:เรื่อง|ว่า)?)\s*'+re.escape(e.title),text) for i,e,marker in members for text in texts)
        if named or any(e.title in quote for i,e,marker in members for quote in quoted):continue
        for index,e,marker in members:result[index]=e.model_copy(update={'title':stem})
    return result

def shared_title(row,unit,title,titles):
    """One explicit repeated name may bind several independently dated slots."""
    names=set(titles)
    if names!={title} or row['text'].count(title)!=1 or title in unit['text']:return False
    view=masked(row['text'],[m.span() for m in re.finditer(re.escape(title),row['text'])])
    count=explicit_count(view)
    if count is None and re.search(r'รอบแรก.*(?:อีกรอบ|อีกครั้ง)|(?:เพิ่ม)?อีก(?:นัด|ครั้ง|รอบ)',view) and len(units(row,[title]))==2:count=2
    return bool(count and count>1 and (DATE.search(unit['text']) or CLOCK.search(unit['text']) or re.search(r'อีกนัด|อีกรอบ|อีกครั้ง',unit['text'])) and not substantive(unit['text']))

def bind(row,unit,title,field,value,titles):
    if value is None:return deepcopy(row)
    from .calendar_workflow import literal_fact
    if field=='title':
        if literal_fact(value,unit['text'],field) is None:
            if shared_title(row,unit,title,titles):return deepcopy(row)
            raise ValueError('calendar_title_source')
    elif literal_fact(value,unit['text'],field) is None and not sharing(row,field,value,titles):
        raise ValueError('calendar_event_fact_pairing')
    if field in {'date_text','end_date_text'} and re.fullmatch(r'(?:วันที่\s*)?[0-9๑-๙]{1,2}',value.strip()):
        if re.search(MONTH,row['text'],re.I) and not re.search(MONTH,unit['text'],re.I):raise ValueError('calendar_shared_month_unclear')
    # Keep the original source reference and timestamp. Only its eligible text
    # changes for the ordinary literal certifier; provenance never becomes new.
    scoped=deepcopy(row)
    if field!='title' and sharing(row,field,value,titles):return scoped
    scoped['text']=unit['text'];return scoped

def shared_clock(row,count):
    if not SHARED.search(row['text']):return {}
    expected=2 if re.search(r'ทั้ง\s*(?:สอง|2|๒|คู่)|\bboth\b',row['text'],re.I) else 3 if re.search(r'ทั้ง\s*สาม',row['text']) else 4 if re.search(r'ทั้ง\s*สี่',row['text']) else count
    if expected!=count:raise ValueError('calendar_shared_target_count')
    matches=list(CLOCK.finditer(row['text']))
    if clock_pair(row['text'],matches):
        event_time(matches[0][0]);event_time(matches[1][0])
        return {'time_text':matches[0][0],'end_time_text':matches[1][0]}
    return {}

def shared_changes(row,count,edits,titles):
    result=shared_clock(row,count)
    if not SHARED.search(row['text']):return result
    modes={e.all_day for e in edits if e.all_day is not None}
    if len(modes)==1 and (ALL_DAY.search(row['text']) or result):result['all_day']=next(iter(modes))
    from .calendar_workflow import FIELDS,literal_fact
    for field in FIELDS:
        if field=='title' or field in result:continue
        values={getattr(e,field) for e in edits if getattr(e,field) is not None and literal_fact(getattr(e,field),row['text'],field) is not None}
        if len(values)==1:
            value=next(iter(values))
            if sharing(row,field,value,titles):result[field]=value
    return result

def shadow_of(unit,draft):
    """Prove that an empty historical slot repeats one certified appointment.

    No matching by title alone: retain different dates/times, extra content and
    explicit repeated occurrences. Facts keep their original owner timestamps.
    """
    title=draft['facts']['title']
    if (not title or not draft['start'] or not draft['end'] or draft['missing_fields'] or draft['issues']
        or unit['source_ref']==draft['source_unit']['source_ref'] or unit['occurrence']):return False
    if title not in unit['text'] or substantive(unit['text'],[title]):return False
    text=masked(unit['text'],[m.span() for m in re.finditer(re.escape(title),unit['text'])])
    if (explicit_count(text) or 1)>1:return False
    dates=list(DATE.finditer(text.translate(DIGITS)))
    if len(dates)!=1:return False
    from datetime import datetime
    try:
        day=event_date(dates[0][0],datetime.fromisoformat(unit['at']))
        clocks=[event_time(m[0]).strftime('%H:%M') for m in CLOCK.finditer(text)]
    except ValueError:return False
    start=datetime.fromisoformat(draft['start']);end=datetime.fromisoformat(draft['end'])
    if draft.get('all_day'):return day==start.date() and not clocks and bool(ALL_DAY.search(text))
    return day==start.date() and (not clocks or clocks==[start.strftime('%H:%M')] or clocks==[start.strftime('%H:%M'),end.strftime('%H:%M')])
