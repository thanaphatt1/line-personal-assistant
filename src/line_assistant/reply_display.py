"""Display values only; provider timestamps and confirmation evidence stay intact."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from .language import BANGKOK, MONTHS


def money(value, missing='?'):
    """One owner-facing amount format: thousands separators, satang only when present."""
    if value is None or value == '':
        return missing
    try:
        amount = Decimal(str(value).replace(',', ''))
    except InvalidOperation:
        return str(value)
    if not amount.is_finite():
        return str(value)
    return f'{amount:,.0f}' if amount == amount.to_integral_value() else f'{amount:,.2f}'


def quantity_text(value, unit=None):
    """Format a verified currency unit without rounding a computed fact."""
    if unit not in {'บาท','THB'}:
        return str(value)
    try:amount=Decimal(str(value))
    except InvalidOperation:return str(value)
    if not amount.is_finite():return str(value)
    places=0 if amount==amount.to_integral_value() else max(2,-amount.as_tuple().exponent)
    return f'{amount:,.{places}f}'


def polite(text):
    """End a sentence with ครับ, keeping a space after Latin script."""
    return text + (' ครับ' if text[-1:].isascii() and text[-1:].strip() else 'ครับ')


def named(name):
    """Spaces only where a Latin-script name meets Thai text."""
    return (' ' if name[:1].isascii() else '') + name + (' ' if name[-1:].isascii() else '')


def day(value):
    at=date.fromisoformat(value)
    return f'{at.day} {MONTHS[at.month-1][1]} {at.year}'


def day_or(value,missing):
    """Thai display date for an optional ISO date; the fallback is shown verbatim."""
    return day(value) if value else missing


def weekday_short(value):
    return ['จ.','อ.','พ.','พฤ.','ศ.','ส.','อา.'][date.fromisoformat(value[:10]).weekday()]


def clock(at):
    if at.microsecond:return at.strftime('%H:%M:%S.%f').rstrip('0')
    return at.strftime('%H:%M:%S' if at.second else '%H:%M')


def calendar_parts(value):
    if value['all_day']:
        first=day(value['start']);last=(date.fromisoformat(value['end'])-timedelta(days=1)).isoformat()
        return first+(' – '+day(last) if last!=value['start'] else ''),'ทั้งวัน'
    start=datetime.fromisoformat(value['start']);end=datetime.fromisoformat(value['end'])
    if start.utcoffset() is None or end.utcoffset() is None:raise ValueError('Aware display times required')
    start=start.astimezone(BANGKOK);end=end.astimezone(BANGKOK)
    period=day(start.date().isoformat())
    if start.date()!=end.date():period+=' – '+day(end.date().isoformat())
    return period,clock(start)+'–'+clock(end)


def appointment_parts(start,end,all_day=False):
    """Date and time cells for one appointment; an overnight span keeps both days."""
    if all_day:return calendar_parts({'start':start,'end':end,'all_day':True})
    a=datetime.fromisoformat(start);b=datetime.fromisoformat(end)
    if a.utcoffset() is None or b.utcoffset() is None:raise ValueError('Aware display times required')
    a=a.astimezone(BANGKOK);b=b.astimezone(BANGKOK)
    if a.date()==b.date():return day(a.date().isoformat()),clock(a)+'–'+clock(b)
    return day(a.date().isoformat())+' '+clock(a)+' – '+day(b.date().isoformat())+' '+clock(b),None


def appointment_text(start,end,all_day=False):
    period,hours=appointment_parts(start,end,all_day)
    return period+(' '+hours if hours=='ทั้งวัน' else ' เวลา '+hours if hours else '')


def expense_lines(entries):
    """Group adjacent dates without sorting, deduplicating or combining payments."""
    rows=[];previous=object()
    for entry in entries:
        when=entry.get('date')
        if when!=previous:
            rows.append(day(when) if when else 'ยังไม่ระบุวันที่');previous=when
        rows.append(f"{entry.get('item') or 'ยังไม่ระบุชื่อ'} — {money(entry.get('amount'),'ยังไม่ระบุยอด')} บาท"+
                    ('\nหมายเหตุ: '+entry['note'] if entry.get('note') else ''))
    return '\n'.join(rows)
