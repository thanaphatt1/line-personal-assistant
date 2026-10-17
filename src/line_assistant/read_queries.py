"""Owner-only stored-data queries; dates and totals are computed without inference.

Reads share the existing fixed Sheets/Calendar ports. Query receipts carry only
scope for a delivered follow-up, never a draft, action, consent or write nonce.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import re

from .calendar_dates import event_date, bangkok_zone
from .contracts import SpendingSummary
from .history_dates import MONTH, cues, year
from .language import BANGKOK, DIGITS
from .catalog import normalized
from .state import Result

RELATIVE = r'เมื่อวานซืน|เมื่อวานนี้|เมื่อวาน|วันนี้|พรุ่งนี้|มะรืนนี้|มะรืน|(?:สัปดาห์|อาทิตย์|เดือน|ปี)(?:ที่ผ่านมา|ที่แล้ว|ก่อน|หน้า|นี้)'
DAY = r'(?:วัน)?(?:พฤหัสบดี|พฤหัส|จันทร์|อังคาร|พุธ|ศุกร์|เสาร์|อาทิตย์)\s*(?:ที่ผ่านมา|ที่แล้ว|ก่อน|หน้า|นี้)'
NUMERIC = r'(?:วันที่\s*)?(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{4})?)'
NAMED = r'(?:วันที่\s*)?\d{1,2}\s*' + MONTH + r'(?:\s*(?:พ\.?ศ\.?|ค\.?ศ\.?)?\s*\d{4})?'
MONTH_ONLY = r'(?:เดือน\s*)?' + MONTH + r'(?:\s*(?:พ\.?ศ\.?|ค\.?ศ\.?)?\s*\d{4})?'
PERIOD = re.compile(r'(?:' + DAY + '|' + RELATIVE + '|' + NUMERIC + '|' + NAMED + '|' + MONTH_ONLY + r'|วันที่\s*\d{1,2})', re.I)
COMPACT = re.compile(r'(\d{1,2})\s*[-–]\s*(\d{1,2})\s*(' + MONTH + r')((?:\s*(?:พ\.?ศ\.?|ค\.?ศ\.?)?\s*\d{4})?)', re.I)
READ = re.compile(r'สรุป|รวมยอด|ยอดรวม|เท่าไหร่|เท่าไร|กี่บาท|กี่รายการ|อะไรบ้าง|อะไรไป|รายการที่บันทึก|แยกรายการ|มี.*(?:ไหม|มั้ย|บ้าง|อะไร)|นัดถัดไป|นัดที่กำลังจะมาถึง')
EXPENSE = re.compile(r'รายจ่าย|ค่าใช้จ่าย|ใช้เงิน|ใช้ไป|จ่าย|ซื้อ|ยอด|บาท|หมวด|ค่าอาหาร|ค่าเดินทาง|ค่ารถ|รายการที่บันทึก')
CALENDAR = re.compile(r'นัด|ปฏิทิน|ตาราง')
WRITE = re.compile(r'เพิ่ม|สร้าง|ลงนัด|บันทึก|แก้|เปลี่ยน|เลื่อน|ลบ|ยกเลิก|ยืนยัน')
FILLERS = ('รายการที่บันทึกไว้', 'รายการที่บันทึกแล้ว', 'รายการที่บันทึก', 'นัดที่กำลังจะมาถึง',
           'ขอดู', 'ช่วยสรุป', 'สรุป', 'รวมยอด', 'ยอดรวม', 'ค่าใช้จ่าย', 'รายจ่าย',
           'ใช้เงินไป', 'ใช้เงิน', 'ใช้ไป', 'จ่ายไป', 'ซื้ออะไรไป', 'จ่ายอะไรไป', 'จ่าย',
           'เท่าไหร่', 'เท่าไร', 'กี่บาท', 'กี่รายการ', 'อะไรบ้าง', 'อะไรไปบ้าง',
           'แยกรายการ', 'เป็นรายการ', 'รายการ', 'ดู', 'เฉพาะ', 'หมวดย่อย', 'หมวด',
           'ตั้งแต่', 'ระหว่าง', 'ช่วง', 'วันที่', 'รวม', 'ยอด', 'ทั้งหมด', 'บาท', 'ให้หน่อย', 'หน่อย', 'ด้วย', 'ครับ', 'ค่ะ', 'คะ', 'แล้ว', 'ส่วน', 'ล่ะ', 'ละ')


def period(text, at):
    """Return inclusive Bangkok dates plus original non-date text; never guess."""
    text = bangkok_zone(text.translate(DIGITS))
    today = at.astimezone(BANGKOK).date()
    found = []
    masked = list(text)
    for m in COMPACT.finditer(text):
        a = event_date(m[1] + ' ' + m[3] + m[4], at)
        b = event_date(m[2] + ' ' + m[3] + m[4], at)
        found.append((m.start(), m.end(), a, b))
        masked[m.start():m.end()] = ' ' * len(m[0])
    for m in PERIOD.finditer(''.join(masked)):
        value = m[0].strip()
        mode = -1 if re.search(r'ก่อน|ที่แล้ว|ที่ผ่านมา', value) else 1 if value.endswith('หน้า') else 0
        if re.fullmatch(r'(?:สัปดาห์|อาทิตย์)(?:ที่ผ่านมา|ที่แล้ว|ก่อน|หน้า|นี้)', value):
            a = today - timedelta(days=today.weekday()) + timedelta(weeks=mode); b = a + timedelta(days=6)
        elif re.fullmatch(r'เดือน(?:ที่ผ่านมา|ที่แล้ว|ก่อน|หน้า|นี้)', value):
            index = today.year * 12 + today.month - 1 + mode
            y, month = divmod(index, 12); a = date(y, month + 1, 1); b = date(y, month + 1, monthrange(y, month + 1)[1])
        elif re.fullmatch(r'ปี(?:ที่ผ่านมา|ที่แล้ว|ก่อน|หน้า|นี้)', value):
            a, b = date(today.year + mode, 1, 1), date(today.year + mode, 12, 31)
        elif re.fullmatch(MONTH_ONLY, value, re.I):
            parts, _, issue = cues(value, at)
            if issue or 'month' not in parts:raise ValueError('date_scope')
            y = parts.get('year', today.year); month = parts['month']
            a, b = date(y, month, 1), date(y, month, monthrange(y, month)[1])
        else:
            numeric = re.sub(r'^วันที่\s*', '', value)
            if re.fullmatch(r'\d{1,2}/\d{1,2}(?:/\d{4})?', numeric):
                bits = list(map(int, numeric.split('/')))
                a = date(year(bits[2]) if len(bits) == 3 else today.year, bits[1], bits[0])
            elif re.fullmatch(r'\d{4}-\d{2}-\d{2}', numeric):a = date.fromisoformat(numeric)
            else:a = event_date(value, at)
            b = a
        found.append((m.start(), m.end(), a, b))
    found.sort()
    if not found:return None, text
    if len(found) > 2:raise ValueError('date_scope')
    if len(found) == 2:
        between = text[found[0][1]:found[1][0]].strip()
        if not re.fullmatch(r'ถึง|จนถึง|[-–]', between):raise ValueError('date_scope')
        a, b = found[0][2], found[1][3]
        masked[found[0][1]:found[1][0]] = ' ' * (found[1][0] - found[0][1])
    else:a, b = found[0][2:]
    SpendingSummary(start_date=a, end_date=b)
    if not 1900 <= a.year <= b.year <= 2100:raise ValueError('date_scope')
    for start, end, _, _ in found:masked[start:end] = ' ' * (end - start)
    return (a, b), ''.join(masked)


def summary_output(totals, dates):
    # Validate any supplied numeric field, even on mocks, before admitting facts.
    for key in ('expense', 'income'):
        if key == 'income' and totals.get('income_supported') is False and totals.get(key) is None:continue
        value = Decimal(totals[key])
        if not value.is_finite() or value < 0:raise ValueError('invalid_summary_result')
    return {'ok': True, 'status': 'summary', 'range': dates, 'expense': str(Decimal(totals['expense'])),
            'income_supported': False, 'read_only': True, 'financial_write_authorized': False}


def summary_result(output):
    return Result('summary', f"ช่วง {output['range']['start_date']} ถึง {output['range']['end_date']} รายจ่าย {Decimal(output['expense']):.2f} บาท\nยังไม่มีแหล่งข้อมูลรายรับที่รองรับ จึงสรุปรายรับหรือยอดคงเหลือไม่ได้", {'tool_result': output})


def label_text(value):
    """Bound untrusted labels for LINE; labels cannot introduce extra rows."""
    value = re.sub(r'[\r\n\t\x00-\x1f]', ' ', value)
    return value[:80] + ('…' if len(value) > 80 else '')


def filter_scope(text, tax):
    """Exact catalog labels/components only; unknown filters never become 'all'."""
    kind = 'subcategory' if 'หมวดย่อย' in text else 'category' if 'หมวด' in text else None
    value = text
    protected = {}
    # Catalog labels may themselves contain words such as ดู/รายการ/ยอด.
    # Remove query fillers only outside literal existing labels.
    for index, label in enumerate(sorted({v for r in tax.rows for v in (r.item, r.category, r.subcategory) if v}, key=lambda s: (-len(s), s))):
        token = '\x01' + str(index) + '\x02'
        value, count = re.subn(re.escape(label), lambda m: token, value, flags=re.I)
        if count:protected[token] = label
    for phrase in sorted(FILLERS, key=len, reverse=True):value = value.replace(phrase, ' ')
    for token, label in protected.items():value = value.replace(token, label)
    value = value.strip(' \t\n?!?ๆ:：,')
    if not value:
        if kind:raise ValueError('filter_scope')
        return None
    key = normalized(value)
    matches = set()
    if kind is None:
        matches.update(('item', r.item) for r in tax.rows if normalized(r.item) == key)
        if matches:
            if len(matches) != 1:raise ValueError('filter_scope')
            return dict([next(iter(matches))])
    if kind != 'subcategory':
        alias = {'เดินทาง': 'Transportation', 'ค่าเดินทาง': 'Transportation', 'ค่ารถ': 'Transportation', 'ค่าอาหาร': 'Food'}.get(value, value)
        matches.update(('category', tax.categories[k]) for k in tax.category_matches(alias))
    if kind != 'category':matches.update(('subcategory', r.subcategory) for r in tax.rows if r.subcategory and normalized(r.subcategory) == key)
    if len(matches) != 1:raise ValueError('filter_scope')
    return dict([next(iter(matches))])


def _last_scope(host):
    row, result = host._presented_result()
    scope = result.data.get('read_query') if result else None
    if not scope:return None
    if getattr(host, 'production', False) and (not row.get('delivery_verified') or host.state.event_workbooks.get(row['event']) != host.sheets.target.spreadsheet_id):return None
    if scope.get('workbook') != host.sheets.target.spreadsheet_id or scope.get('calendar') != host.calendar_flow.calendar_id:return None
    return deepcopy(scope)


def route(host, text, at):
    if not host.raw_history_enabled or host._active_event_kind == 'postback':return None
    previous = _last_scope(host)
    explicit = 'calendar' if CALENDAR.search(text) else 'expense' if EXPENSE.search(text) or 'รายรับ' in text or 'ยอดคงเหลือ' in text else None
    clean_write = re.sub(r'ที่บันทึก(?:ไว้|แล้ว)?|รายการที่บันทึก(?:ไว้|แล้ว)?', '', text)
    reading = bool(READ.search(text) or re.match(r'^(?:(?:ช่วย|ขอ|อยาก|รบกวน|แล้ว)\s*)?(?:ดู|แสดง|รายการที่บันทึก)', text))
    if previous and explicit and re.match(r'^(?:แล้ว|ส่วน|เฉพาะ|หมวด)', text):reading = True
    if not reading:
        try:
            dates_for_intent, without_dates = period(text, at)
            reading = bool(dates_for_intent and re.match(r'^\s*(?:ขอ)?(?:ดู|แสดง)', without_dates))
        except ValueError:pass
    following = bool(previous and not explicit and (reading or re.match(r'^(?:แล้ว|เฉพาะ|หมวด|เดือน|สัปดาห์|วันนี้|เมื่อวาน|พรุ่งนี้|วันที่)', text)))
    if 'ร่าง' in text:return None
    if WRITE.search(clean_write):
        if reading and explicit:return Result('clarification', 'ต้องการดูข้อมูลที่บันทึกไว้ หรือเปลี่ยนข้อมูลครับ? กรุณาเลือกอย่างใดอย่างหนึ่งก่อน')
        return None
    if not explicit and not reading:
        # A new terse title is not a date-only query follow-up, even when the
        # preceding delivered response happened to be a stored-data query.
        if (host.calendar_flow.enabled and not any(d['status'] not in {'recorded', 'cancelled'} for d in host.state.calendar_drafts.values())
                and not any(d['status'] != 'recorded' for d in host.state.drafts.values())):
            try:dates, rest = period(text, at)
            except ValueError:return None
            if dates and dates[0] == dates[1] and re.fullmatch(r'[A-Za-z][A-Za-z0-9 _-]{0,79}', rest.strip()):
                return Result('clarification', f"ต้องการเพิ่มนัด {rest.strip()} วันที่ {dates[0]} หรือดูนัดที่บันทึกไว้ในวันนั้นครับ?", {'read_only': True})
    if not (explicit and reading or following):
        return None
    domain = explicit or previous['domain']
    scope = {'domain': domain, 'workbook': host.sheets.target.spreadsheet_id, 'calendar': host.calendar_flow.calendar_id}
    data = {'read_only': True, 'financial_write_authorized': False, 'calendar_write_authorized': False, 'read_query': scope}
    def answer(status, message):return Result(status, message, deepcopy(data))
    if 'รายรับ' in text or 'ยอดคงเหลือ' in text:
        return answer('income_unconfigured', 'ยังไม่มีแหล่งข้อมูลรายรับที่รองรับ จึงสรุปรายรับหรือยอดคงเหลือไม่ได้ กรุณาถามยอดรายจ่ายแยกต่างหากครับ')
    try:dates, remaining = period(text, at)
    except (ValueError, TypeError):return answer('clarification', 'ช่วงวันที่หรือเขตเวลายังไม่ชัดเจน กรุณาระบุวันเริ่มและวันสิ้นสุดตามเวลาไทยครับ')
    upcoming = domain == 'calendar' and bool(re.search(r'นัดถัดไป|กำลังจะมาถึง|นัดต่อไป', text))
    if not dates and upcoming:dates = (at.astimezone(BANGKOK).date(), at.astimezone(BANGKOK).date() + timedelta(days=6))
    if not dates and previous and previous.get('start') and (not explicit or domain == previous['domain'] or re.match(r'^(?:แล้ว|ส่วน)', text)):dates = (date.fromisoformat(previous['start']), date.fromisoformat(previous['end']))
    if not dates:return answer('clarification', 'ต้องการดูช่วงวันไหนครับ? ระบุวัน เดือนนี้ หรือสัปดาห์ที่ต้องการได้ครับ')
    a, b = dates;scope.update(start=a.isoformat(), end=b.isoformat())
    heading = f'ช่วง {a} ถึง {b} (เวลาไทย)'
    if domain == 'calendar':
        if not host.calendar_flow.enabled:return answer('unavailable', 'ยังไม่ได้เปิดการอ่านปฏิทิน จึงตรวจนัดที่บันทึกไว้ไม่ได้ครับ')
        if (b - a).days > 6:return answer('clarification', 'อ่านปฏิทินได้ครั้งละไม่เกิน 7 วัน กรุณาเลือกวันหรือสัปดาห์ครับ')
        # Calendar filters are not supported; never silently ignore one.
        residual = remaining
        for phrase in ('นัดที่กำลังจะมาถึง', 'นัดถัดไป', 'นัดต่อไป', 'ปฏิทิน', 'ตาราง', 'นัด', 'ขอดู', 'ดู', 'มี', 'อะไรบ้าง', 'อะไร', 'บ้าง', 'ไหม', 'มั้ย', 'ให้หน่อย', 'หน่อย', 'ครับ', 'ค่ะ', 'แล้ว', 'ส่วน', 'ล่ะ', 'ตั้งแต่', 'ระหว่าง', 'ช่วง', 'วันที่'):
            residual = residual.replace(phrase, ' ')
        if residual.strip(' \t\n?!,'):return answer('clarification', 'ต้องการดูนัดทั้งหมดในช่วงนี้ใช่ไหมครับ? ตอนนี้ยังกรองชื่อหรือประเภทนัดไม่ได้ กรุณาระบุวันหรือสัปดาห์ที่จะดูครับ')
        start = datetime.combine(a, time.min, BANGKOK);end = datetime.combine(b + timedelta(days=1), time.min, BANGKOK)
        if upcoming:start = max(start, at.astimezone(BANGKOK))
        try:events = host.calendar_flow.adapter.saved_events(start, end)
        except Exception:return answer('unavailable', 'อ่านปฏิทินได้ไม่ครบหรือเชื่อมต่อไม่สำเร็จ จึงยังยืนยันรายการนัดหรือการว่างไม่ได้ครับ')
        data['tool_result'] = {'status': 'stored_query', 'read_only': True, 'complete': True, 'events': events[:20], 'count': len(events)}
        lines = []
        for e in events[:20]:
            start, end = e['start'], e['end']
            if e['all_day']:
                last = date.fromisoformat(end) - timedelta(days=1)
                when = start + (' ถึง ' + last.isoformat() if last.isoformat() != start else '') + ' ทั้งวัน'
            else:when = datetime.fromisoformat(start).astimezone(BANGKOK).strftime('%Y-%m-%d %H:%M') + ' ถึง ' + datetime.fromisoformat(end).astimezone(BANGKOK).strftime('%Y-%m-%d %H:%M')
            lines.append(when + ' — ' + label_text(e['title']))
        return answer('answer', heading + f'\nพบนัดที่บันทึกไว้ {len(events)} รายการ' + ('\n' + '\n'.join(lines) if lines else '') + ('\nแสดง 20 รายการแรกจากทั้งหมด ' + str(len(events)) if len(events) > 20 else '') + ('\nเฉพาะนัดที่ยังไม่จบตั้งแต่เวลาปัจจุบัน' if upcoming else ''))
    try:
        chosen = filter_scope(remaining, host._taxonomy())
        if chosen is None and previous and not explicit and previous['domain'] == domain and 'ทั้งหมด' not in text:chosen = previous.get('filter')
        scope['filter'] = chosen
        entries = host.sheets.saved_entries(a, b)
        if chosen:
            kind, label = next(iter(chosen.items()))
            if kind == 'item':entries = [e for e in entries if normalized(e['item']) == normalized(label)]
            else:
                mappings = {}
                for r in host._taxonomy().rows:mappings.setdefault(normalized(r.item), set()).add(getattr(r, kind))
                if any(len(mappings.get(normalized(e['item']), set())) != 1 for e in entries):raise ValueError('incomplete_mapping')
                entries = [e for e in entries if label in mappings[normalized(e['item'])]]
    except ValueError as error:
        if str(error) == 'filter_scope':return answer('clarification', 'ชื่อรายการหรือหมวดยังไม่ตรงกับ Database เพียงตัวเลือกเดียว กรุณาระบุชื่อรายการ หมวด หรือหมวดย่อยให้ชัดเจนครับ')
        return answer('unavailable', 'อ่านรายจ่ายหรือข้อมูลหมวดได้ไม่ครบ จึงยังสรุปยอดหรือยืนยันว่าไม่มีรายการไม่ได้ครับ')
    except Exception:return answer('unavailable', 'อ่านรายจ่ายได้ไม่ครบหรือเชื่อมต่อไม่สำเร็จ จึงยังสรุปยอดหรือยืนยันว่าไม่มีรายการไม่ได้ครับ')
    entries.sort(key=lambda e: (e['date'], e['item']))
    total = sum((Decimal(e['amount']) for e in entries), Decimal('0'))
    listing = bool(re.search(r'รายการ|อะไร', text)) or bool(previous and previous.get('listing') and not explicit)
    scope['listing'] = listing
    data['tool_result'] = {'status': 'stored_query', 'read_only': True, 'complete': True, 'expense': f'{total:.2f}', 'count': len(entries), 'entries': entries[:20] if listing else [], 'income_supported': False}
    message = heading + ('\nเฉพาะ ' + next(iter(scope['filter'].values())) if scope.get('filter') else '') + f'\nรายจ่ายรวม {total:.2f} บาท จาก {len(entries)} รายการ'
    if listing:message += ''.join(f"\n{e['date']} — {label_text(e['item'])} {Decimal(e['amount']):.2f} บาท" for e in entries[:20])
    if listing and len(entries) > 20:message += f'\nแสดง 20 รายการแรกจากทั้งหมด {len(entries)} รายการ ยอดรวมคำนวณจากทั้งหมดที่อ่านครบแล้ว'
    return answer('summary', message)
