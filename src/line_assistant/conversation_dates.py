"""Preserve partial day/month evidence without guessing an omitted month."""
from datetime import date
import re
from .language import BANGKOK, DIGITS, MONTH_LOOKUP, MONTH_PATTERN, dates

def resolve(text, at, previous=None):
    previous = dict(previous or {})
    source = text.translate(DIGITS)
    evidence = dates(text, at)
    day = re.search(r"วันที่\s*(\d{1,2})(?!\d)", source)
    month = re.search(MONTH_PATTERN, source)
    year = re.search(r"(?:พ\.?ศ\.?|ค\.?ศ\.?)\s*(\d{4})(?!\d)", source)
    bare_year = re.match(r'\s*(\d{4})(?!\d)(?!\s*บาท)', source[month.end():]) if month and not year else None
    spans = list(evidence.spans)
    if evidence.explicit and evidence.value is not None:
        return evidence.value.isoformat(), {}, spans
    # A rejected complete date, competing dates, or unsupported relative qualifier
    # must never be "repaired" by choosing its first day/month.
    if (len(re.findall(r'วันที่\s*\d{1,2}(?!\d)', source)) > 1
            or len(re.findall(MONTH_PATTERN, source)) > 1
            or (evidence.explicit and evidence.value is None and day and month)
            or re.search(r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d|สัปดาห์|เดือนที่แล้ว|ปีที่แล้ว|วัน(?:จันทร์|อังคาร|พุธ|พฤหัส|ศุกร์|เสาร์|อาทิตย์)', source)):
        return None, {}, spans
    if day:
        previous['day'] = int(day[1]); spans.append(day.span())
    if month:
        previous['month'] = MONTH_LOOKUP[month[0].replace('.', '')]; spans.append(month.span())
    if year:
        number = int(year[1]); previous['year'] = number - 543 if number >= 2400 else number
        spans.append(year.span())
    elif bare_year:
        number = int(bare_year[1]); previous['year'] = number - 543 if number >= 2400 else number
        spans.append((month.end() + bare_year.start(), month.end() + bare_year.end()))
    if previous.get('day') and previous.get('month'):
        try:
            resolved_year = previous.get('year', at.astimezone(BANGKOK).year)
            if not 1900 <= resolved_year <= 2100: raise ValueError()
            value = date(resolved_year, previous['month'], previous['day'])
            if value > at.astimezone(BANGKOK).date(): raise ValueError()
            return value.isoformat(), {}, spans
        except ValueError:
            return None, previous, spans
    if previous or evidence.explicit:
        return None, previous, spans
    return evidence.value.isoformat(), {}, spans
