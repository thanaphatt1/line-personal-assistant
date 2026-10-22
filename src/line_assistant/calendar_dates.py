"""Calendar-only temporal parsing of literal owner evidence in Bangkok.

No default start time, duration or all-day conversion. Future event dates have
their own policy; expense date defaults/restrictions are unchanged.
"""
from datetime import date,time,timedelta
from decimal import Decimal
import re
from .history_dates import cues,WEEKDAYS,MONTH
from .tool_errors import PlanningError
from .language import BANGKOK,DIGITS

WORDS={'ศูนย์':0,'หนึ่ง':1,'นึง':1,'สอง':2,'สาม':3,'สี่':4,'ห้า':5,'หก':6,'เจ็ด':7,'แปด':8,'เก้า':9,'สิบ':10,'สิบเอ็ด':11,'สิบสอง':12}
DAY='พฤหัสบดี|พฤหัส|จันทร์|อังคาร|พุธ|ศุกร์|เสาร์|อาทิตย์'

def integer(text):
    value=text.strip()
    if value.isdigit():return int(value)
    if value in WORDS:return WORDS[value]
    match=re.fullmatch(r'(ยี่|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า)?สิบ(เอ็ด|หนึ่ง|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า)?',value)
    if match:return (2 if match[1]=='ยี่' else WORDS[match[1]] if match[1] else 1)*10+(1 if match[2]=='เอ็ด' else WORDS[match[2]] if match[2] else 0)
    return None

def bangkok_zone(text):
    """Accept explicit Bangkok annotations, never silently discard another zone."""
    value=re.sub(r'Asia/Bangkok(?![A-Za-z_])|(?<![A-Za-z])ICT(?![A-Za-z])|เวลา(?:ไทย|กรุงเทพ(?:มหานคร|ฯ)?)|(?:UTC|GMT)\s*\+\s*0?7(?::?00)?(?![\d:])|\+07:?00(?![\d:])',' ',text.translate(DIGITS),flags=re.I)
    # A complete ascending wall-clock interval has a range separator, not an
    # offset. Keep the original text for fact certification/parsing. ISO T
    # timestamps, explicit zone names, positive offsets and descending/invalid
    # clocks remain subject to the original foreign-zone guard.
    def interval(match):
        a,b=match['a'],match['b']
        try:start=time(*map(int,a.split(':')));end=time(*map(int,b.split(':')))
        except ValueError:return match[0]
        return match[0].replace('-', ' ') if start<end else match[0]
    checked=re.sub(r'(?<![A-Za-z0-9+:.\-])(?P<a>[0-9]{1,2}:[0-9]{2})\s*[-–—]\s*(?P<b>[0-9]{1,2}:[0-9]{2})(?![0-9:])',interval,value)
    if re.search(r'(?<![A-Za-z])(?:UTC|GMT|JST|PST|PDT|EST|EDT|CET|CEST|SGT|BST|AEST)(?![A-Za-z])|(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific|Etc)/[A-Za-z_]+|[+-]\d{1,2}:\d{2}|\d{1,2}:\d{2}\s*[+-]\d{4}(?!\d)|\d:\d{2}Z\b|เวลา(?:ญี่ปุ่น|โตเกียว|สิงคโปร์|ลอนดอน|นิวยอร์ก|อเมริกา|อังกฤษ)',checked,re.I):
        raise PlanningError('calendar_timezone_unsupported',kind='unsupported')
    return value

def event_date(text,at):
    if not text:return None
    if at.utcoffset() is None:raise PlanningError('calendar_date_unclear')
    value=re.sub(r'\s*(?:ครับ|ค่ะ|คะ)$','',text.translate(DIGITS).strip());today=at.astimezone(BANGKOK).date()
    value=re.sub(r'\s*/\s*','/',value)
    def valid(value):
        if not 1900<=value.year<=2100:raise PlanningError('calendar_date_invalid')
        return value
    relative=re.fullmatch(r'อีก\s*(.+?)\s*(วัน|สัปดาห์)',value)
    if relative:
        count=integer(relative[1])
        if count is None or not 1<=count<=365:raise PlanningError('calendar_date_unclear')
        return valid(today+timedelta(days=count*(7 if relative[2]=='สัปดาห์' else 1)))
    weekday=re.fullmatch(r'(?:วัน)?('+DAY+r')\s*(หน้า|นี้|ที่ผ่านมา|ที่แล้ว|ก่อน)',value)
    if weekday:
        day=WEEKDAYS[weekday[1]];mode=weekday[2]
        delta=((day-today.weekday())%7 or 7) if mode=='หน้า' else (day-today.weekday())%7 if mode=='นี้' else -((today.weekday()-day)%7 or 7)
        return valid(today+timedelta(days=delta))
    weekday=re.match(r'^(?:วัน)?('+DAY+r')(?=\s*(?:ที่\s*)?\d)',value)
    expected=WEEKDAYS[weekday[1]] if weekday else None
    if weekday:value=value[weekday.end():]
    suffix=re.fullmatch(r'(.+?)\s*(?:วัน)?('+DAY+r')',value)
    if suffix:expected=WEEKDAYS[suffix[2]];value=suffix[1]
    value=re.sub('('+MONTH+r')(?=(?:(?:พ\.?ศ\.?|ค\.?ศ\.?)\s*)?\d{4}(?!\d))',r'\1 ',value,flags=re.I)
    parts,spans,issue=cues(value,at)
    if issue or 'day' not in parts:raise PlanningError('calendar_date_unclear')
    remaining=list(value)
    for a,b in spans:remaining[a:b]='_'*(b-a)
    if not re.fullmatch(r'(?:[_\s,]|วันที่|วัน|ที่)*',''.join(remaining)):raise PlanningError('calendar_date_unclear')
    try:result=date(parts.get('year',today.year),parts.get('month',today.month),parts['day'])
    except (ValueError,TypeError):raise PlanningError('calendar_date_invalid') from None
    if not 1900<=result.year<=2100:raise PlanningError('calendar_date_invalid')
    if expected is not None and result.weekday()!=expected:raise PlanningError('calendar_weekday_conflict')
    return valid(result)

def event_time(text,*,allow_bare=False):
    if not text:return None
    value=''.join(bangkok_zone(text.translate(DIGITS)).split()).lower()
    value=re.sub(r'^(?:เวลา|ช่วง|ตอน|เริ่ม(?:เวลา|ตอน)?|ตั้งแต่|จนถึง|ถึง)','',value)
    half=value.endswith('ครึ่ง')
    if half:value=value[:-5]
    if value in {'เที่ยงคืน','เที่ยง','เที่ยงวัน'}:return time(0 if value=='เที่ยงคืน' else 12,30 if half else 0)
    numeric=re.fullmatch(r'(\d{1,2})[:.](\d{2})(?:น\.?|นาฬิกา|am|pm)?',value)
    if numeric:
        hour,minute=map(int,numeric.groups())
        if half or value.endswith(('am','pm')) and not 1<=hour<=12:raise PlanningError('calendar_time_invalid')
        if value.endswith('pm') and 1<=hour<=12:hour=hour%12+12
        elif value.endswith('am') and 1<=hour<=12:hour%=12
        try:return time(hour,minute)
        except ValueError:raise PlanningError('calendar_time_invalid') from None
    meridiem=re.fullmatch(r'(\d{1,2})(am|pm)',value)
    if meridiem:
        hour=int(meridiem[1])
        if not 1<=hour<=12:raise PlanningError('calendar_time_invalid')
        return time(hour%12+(12 if meridiem[2]=='pm' else 0),0)
    clock_minutes=re.fullmatch(r'(.+?(?:โมง(?:เช้า|เย็น)?|ทุ่ม|นาฬิกา))(.+?)(?:นาที)?',value)
    if clock_minutes:
        minute=integer(clock_minutes[2])
        if minute is not None:
            hour=event_time(clock_minutes[1])
            if half or not 0<=minute<=59:raise PlanningError('calendar_time_invalid')
            return time(hour.hour,minute)
    clock_hour=re.fullmatch(r'(\d{1,2})น\.?',value)
    if clock_hour:
        try:return time(int(clock_hour[1]),30 if half else 0)
        except ValueError:raise PlanningError('calendar_time_invalid') from None
    if allow_bare and value.isdigit() and (int(value)==0 or int(value)>=13):
        try:return time(int(value),0)
        except ValueError:raise PlanningError('calendar_time_invalid') from None
    patterns=[(r'บ่าย(.+?)(?:โมง)?',lambda h:h+12 if 1<=h<=6 else None),
              (r'ตี(.+)',lambda h:h if 1<=h<=5 else None),
              (r'(.+?)ทุ่ม',lambda h:h+18 if 1<=h<=5 else None),
              (r'(.+?)โมงเย็น',lambda h:h+12 if 1<=h<=6 else None),
              (r'(.+?)โมง(?:เช้า)?',lambda h:h if 6<=h<=12 else None),
              (r'(.+?)นาฬิกา',lambda h:h if 0<=h<=23 else None)]
    if value in {'บ่ายโมง','บ่ายหนึ่งโมง'}:return time(13,30 if half else 0)
    for pattern,convert in patterns:
        match=re.fullmatch(pattern,value)
        if match:
            hour=integer(match[1]);hour=convert(hour) if hour is not None else None
            if hour is None:break
            return time(hour,30 if half else 0)
    raise PlanningError('calendar_time_unclear')

def duration(text,*,allow_bare=False):
    if not text:return None
    value=''.join(text.translate(DIGITS).split()).lower().replace('ชม.','ชั่วโมง').replace('ชม','ชั่วโมง')
    value=re.sub(r'^(?:ใช้เวลา|กินเวลา|(?:กำหนด|มี)?ระยะเวลา(?:ไว้)?|เวลา|นาน)','',value)
    value=re.sub(r'พอดี$','',value)
    if value=='ครึ่งชั่วโมง':return 30
    if value in {'ชั่วโมงครึ่ง','หนึ่งชั่วโมงครึ่ง'}:return 90
    if value in {'ชั่วโมง','หนึ่งชั่วโมง','ชั่วโมงนึง'}:return 60
    hours=re.fullmatch(r'(.+?)ชั่วโมง(?:(.+?)นาที|(ครึ่ง))?',value)
    minutes=re.fullmatch(r'(.+?)นาที',value)
    if hours:
        h=integer(hours[1]);m=30 if hours[3] else integer(hours[2]) if hours[2] else 0
        if h is None and re.fullmatch(r'\d+\.\d+',hours[1]):
            value_minutes=Decimal(hours[1])*60
            h=value_minutes/60 if value_minutes==int(value_minutes) else None
        result=int(h*60+m) if h is not None and m is not None else None
    elif minutes:result=integer(minutes[1])
    elif allow_bare and value.isdigit():result=int(value)
    else:result=None
    if result is None or not 1<=result<=7*24*60:raise PlanningError('calendar_duration_unclear')
    return result
