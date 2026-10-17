"""Calendar-only date events; legacy timed contracts and payloads stay intact."""
from datetime import date,datetime,time,timedelta
from typing import Literal
import re
from pydantic import model_validator
from .contracts import Contract,CreateEvent,Text
from .language import BANGKOK

ALL_DAY=re.compile(r'(?:แบบ)?(?:ทั้งวัน|ตลอดวัน|เต็มวัน|\ball[ -]?day\b)',re.I)

def clarification_fields(draft):
    """Display one timing choice without changing certified missing facts."""
    fields=list(draft.get('missing_fields',[]));facts=draft['facts']
    if (not draft.get('all_day') and 'time' in fields
        and not any(facts.get(k) for k in ['time_text','end_time_text','duration_text'])):
        fields=['time_or_all_day' if k=='time' else k for k in fields if k!='end']
    return fields

def timing_question(ordinal=None):
    return ('นัดนี้' if ordinal is None else f'นัดที่ {ordinal} ')+'ทั้งวัน หรือเริ่มกี่โมงครับ?'

def range_requested(text):
    from .calendar_sources import DATE,CLOCK,SHARED
    points=list(DATE.finditer(text))
    if any(re.fullmatch(r'\s*(?:[-–—]|ถึง|จนถึง|to|until)\s*',text[a.end():b.start()],re.I) for a,b in zip(points,points[1:])):return True
    if re.search(r'(?<![0-9๐-๙-])[0-9๐-๙]{1,2}\s*(?:[-–—]|ถึง|จนถึง)\s*[0-9๐-๙]{1,2}(?![0-9๐-๙])',CLOCK.sub(' ',text)):return True
    return not SHARED.search(text) and bool(re.search(r'(?:สอง|สาม|สี่|ห้า|หก|เจ็ด|[2-9๒-๙])\s*วัน',DATE.sub(' ',text)))

class AllDayEvent(Contract):
    title:Text
    start:date
    end:date  # Exclusive date, never a manufactured midnight datetime.
    all_day:Literal[True]=True
    reminder_minutes:None=None

    @model_validator(mode='before')
    @classmethod
    def date_only(cls,value):
        if isinstance(value,dict):
            for field in ['start','end']:
                v=value.get(field)
                if type(v) is not date and (not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',v)):
                    raise ValueError('Date-only Calendar boundary required')
        return value

    @model_validator(mode='after')
    def one_day(self):
        if self.end-self.start!=timedelta(days=1):raise ValueError('One all-day date per event; split a range explicitly')
        return self

def parse_payload(value):
    return AllDayEvent.model_validate(value) if value.get('all_day') is True else CreateEvent.model_validate(value)

def interval(draft):
    if draft.get('all_day'):
        return tuple(datetime.combine(date.fromisoformat(draft[k]),time.min,BANGKOK) for k in ['start','end'])
    return tuple(datetime.fromisoformat(draft[k]) for k in ['start','end'])

def date_label(value):
    at=date.fromisoformat(value)
    months=['ม.ค.','ก.พ.','มี.ค.','เม.ย.','พ.ค.','มิ.ย.','ก.ค.','ส.ค.','ก.ย.','ต.ค.','พ.ย.','ธ.ค.']
    return f'{at.day} {months[at.month-1]} {at.year}'
