"""Bounded source parsers. No model output, provider access or authorization here.

Only a deliberately small, complete expense grammar earns source grounding.
Other prose remains a model proposal requiring an owner-confirmed preview.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from .catalog import ItemCatalog
from .contracts import ToolRequest
from .planner import PlannerNotice

BANGKOK = ZoneInfo("Asia/Bangkok")
DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
MONTHS = (
    ("มกราคม", "ม.ค.", "มค"), ("กุมภาพันธ์", "ก.พ.", "กพ"),
    ("มีนาคม", "มี.ค.", "มีค"), ("เมษายน", "เม.ย.", "เมย"),
    ("พฤษภาคม", "พ.ค.", "พค"), ("มิถุนายน", "มิ.ย.", "มิย"),
    ("กรกฎาคม", "ก.ค.", "กค"), ("สิงหาคม", "ส.ค.", "สค"),
    ("กันยายน", "ก.ย.", "กย"), ("ตุลาคม", "ต.ค.", "ตค"),
    ("พฤศจิกายน", "พ.ย.", "พย"), ("ธันวาคม", "ธ.ค.", "ธค"),
)
MONTH_LOOKUP = {n.replace(".", ""): i for i, names in enumerate(MONTHS, 1) for n in names}
MONTH_PATTERN = "(?:" + "|".join(re.escape(n) for names in MONTHS for n in names) + r")(?![\u0e01-\u0e4eA-Za-z])"
WORD_DIGITS = {n: i for i, n in enumerate(("ศูนย์", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"))}
UNITS = {"สิบ": 10, "ร้อย": 100, "พัน": 1000, "หมื่น": 10000, "แสน": 100000}
WORD_TOKEN = "|".join(sorted((*WORD_DIGITS, *UNITS, "ยี่", "เอ็ด", "ล้าน"), key=len, reverse=True))


def money_string(value: Decimal) -> str:
    if not value.is_finite() or not 0 < value < 10000000 or value.as_tuple().exponent < -2:
        raise ValueError("Positive THB amount with at most two decimal places required")
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def thai_integer(text: str) -> int:
    tokens = re.findall(WORD_TOKEN, text)
    if "".join(tokens) != text or not tokens:
        raise ValueError("Unsupported Thai number")
    if "ล้าน" in tokens:
        if tokens.count("ล้าน") != 1:
            raise ValueError("Repeated million unit")
        left, right = text.split("ล้าน")
        result = thai_integer(left) * 1000000 + (thai_integer(right) if right else 0)
        if result >= 10000000:
            raise ValueError("Amount outside bound")
        return result
    total, pending, last_unit = 0, None, 1000000
    for index, token in enumerate(tokens):
        if token in UNITS:
            unit = UNITS[token]
            if unit >= last_unit or (pending is None and unit != 10):
                raise ValueError("Unordered Thai units")
            total += (pending if pending is not None else 1) * unit
            pending, last_unit = None, unit
        else:
            value = 2 if token == "ยี่" else 1 if token == "เอ็ด" else WORD_DIGITS[token]
            if pending is not None or (token == "ยี่" and (index + 1 == len(tokens) or tokens[index+1] != "สิบ")):
                raise ValueError("Unordered Thai digits")
            if token == "เอ็ด" and (index != len(tokens)-1 or total == 0):
                raise ValueError("Unsupported terminal digit")
            if value == 0 and len(tokens) > 1:
                raise ValueError("Unsupported zero")
            pending = value
    return total + (pending or 0)


@dataclass(frozen=True)
class Amount:
    start: int
    end: int
    value: str | None


def amounts(text: str, excluded=()) -> list[Amount]:
    source = text.translate(DIGITS)
    occupied = list(excluded)
    found = []
    # Consume the entire numeric token, including malformed grouping/sign/unit.
    pattern = r"[+-]?\d[\d,.]*(?:[kK]|ล้าน|พัน)?"
    for m in re.finditer(pattern, source):
        if any(m.start() < b and m.end() > a for a, b in occupied):
            continue
        raw = m.group()
        valid = re.fullmatch(r"(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)(?:\.\d{1,2})?", raw)
        try:
            value = money_string(Decimal(raw.replace(",", ""))) if valid else None
        except ValueError:
            value = None
        if re.search(r"(?:[-+]|ลบ)\s*$", source[:m.start()]):
            value = None
        found.append(Amount(m.start(), m.end(), value))
        occupied.append((m.start(), m.end()))
    for m in re.finditer(rf"(?:{WORD_TOKEN})+(?:จุด(?:{'|'.join(WORD_DIGITS)})+)?", source):
        if any(m.start() < b and m.end() > a for a, b in occupied):
            continue
        try:
            parts = m.group().split("จุด")
            integer = thai_integer(parts[0])
            fraction = ""
            if len(parts) == 2:
                ds = re.findall("|".join(WORD_DIGITS), parts[1])
                if "".join(ds) != parts[1] or not 1 <= len(ds) <= 2:
                    raise ValueError("Unsupported fractional amount")
                fraction = "." + "".join(str(WORD_DIGITS[d]) for d in ds)
            value = money_string(Decimal(str(integer) + fraction))
        except ValueError:
            value = None
        if re.search(r"(?:[-+]|ลบ)\s*$", source[:m.start()]):
            value = None
        found.append(Amount(m.start(), m.end(), value))
    return sorted(found, key=lambda a: a.start)


@dataclass(frozen=True)
class DateEvidence:
    value: date | None
    spans: tuple[tuple[int, int], ...]
    explicit: bool = False
    issue: str | None = None


def dates(text: str, occurred_at: datetime) -> DateEvidence:
    if occurred_at.utcoffset() is None:
        raise ValueError("Aware date context required")
    today = occurred_at.astimezone(BANGKOK).date()
    source = text.translate(DIGITS)
    found, invalid = [], False
    def add(m, year, month, day):
        nonlocal invalid
        try:
            year = year - 543 if year >= 2400 else year
            if not 1900 <= year <= 2100:
                raise ValueError()
            value = date(year, month, day)
        except ValueError:
            value, invalid = None, True
        prefix = re.search(r'วันที่\s*$', source[:m.start()])
        found.append((prefix.start() if prefix else m.start(), m.end(), value))
    for m in re.finditer(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", source):
        add(m, *map(int, m.groups()))
    for m in re.finditer(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)", source):
        day, month, year = map(int, m.groups())
        add(m, year, month, day)
    for m in re.finditer(rf"(?<!\d)(\d{{1,2}})\s*({MONTH_PATTERN})(?:\s+(?:พ\.?ศ\.?\s*|ค\.?ศ\.?\s*)?(\d{{4}})(?!\d))?", source):
        day, month, year = m.groups()
        add(m, int(year) if year else today.year, MONTH_LOOKUP[month.replace(".", "")], int(day))
        if re.match(r"\s*\d", source[m.end():]) and not re.match(r"\s*\d[\d,.]*\s*บาท", source[m.end():]):
            invalid = True
    for m in re.finditer(r"เมื่อวานซืน|เมื่อวานนี้|เมื่อวาน|วานนี้|วันนี้|พรุ่งนี้|มะรืน(?:นี้)?", source):
        delta = -2 if m.group() == "เมื่อวานซืน" else -1 if m.group() in {"เมื่อวาน", "เมื่อวานนี้", "วานนี้"} else 0 if m.group() == "วันนี้" else 1 if m.group() == "พรุ่งนี้" else 2
        found.append((m.start(), m.end(), today + timedelta(days=delta)))
    mask = list(source)
    for a,b,_ in found:
        mask[a:b] = " " * (b-a)
    leftover = "".join(mask)
    # Do not default to today when an incomplete/unsupported date is present.
    unsupported = re.search(r"วันที่\s*\d+(?:[/.-]\d+)*|\d{1,2}/\d{1,2}|สัปดาห์|เดือนที่แล้ว|ปีที่แล้ว|วัน(?:จันทร์|อังคาร|พุธ|พฤหัส|ศุกร์|เสาร์|อาทิตย์)", leftover)
    if unsupported:
        found.append((unsupported.start(), unsupported.end(), None))
        invalid = True
    spans = tuple((a,b) for a,b,_ in found)
    values = {v for _,_,v in found if v is not None}
    if invalid or len(values) > 1:
        return DateEvidence(None, spans, True, "ambiguous_date")
    value = next(iter(values)) if values else today
    if value > today:
        return DateEvidence(None, spans, True, "future_expense_date")
    return DateEvidence(value, spans, bool(found))


def intent_guard(text: str) -> str | None:
    """Fail closed on explicit non-action, hypotheticals and instruction quoting.

    This is not a complete Thai semantic classifier. Unparsed model proposals
    therefore also need an explicit server-generated expense confirmation.
    """
    if re.search(r"(?:อย่า|ไม่ต้อง|ยังไม่(?:ต้อง)?|ห้าม|ไม่(?:ให้)?)(?:\s|เพิ่ง)*(?:บันทึก|จด|ลง|เพิ่ม|ทำรายการ|save|record)|(?:do\s+not|don't)\s+(?:save|record|add)", text, re.I):
        return "negated"
    if re.search(r"ถ้า|(?:^|\s)(?:สมมติ|ตัวอย่าง|ทดลอง|ทดสอบ)(?:ว่า|จ่าย|ซื้อ|\s|$)|ยังไง|อย่างไร|ควร|บันทึก.*ไหม|[?\"“”]|ignore\s+(?:rules|instructions)", text, re.I):
        return "non_action"
    return None


def read_request(text: str, occurred_at: datetime):
    compact = text.strip()
    evidence = dates(compact, occurred_at)
    if re.fullmatch(r"(?:เมื่อวาน(?:นี้)?|วานนี้|วันนี้|พรุ่งนี้|เมื่อวานซืน)(?:เป็น)?วันที่(?:เท่าไหร่|เท่าไร)(?:ครับ|ค่ะ|คะ|หน่อย)?", compact):
        # A future date question is read-only, unlike a future-dated expense.
        base = occurred_at.astimezone(BANGKOK).date()
        delta = -2 if compact.startswith("เมื่อวานซืน") else -1 if compact.startswith(("เมื่อวาน", "วานนี้")) else 1 if compact.startswith("พรุ่งนี้") else 0
        value = base + timedelta(days=delta)
        return PlannerNotice("date_answer", f"วันที่ {value.isoformat()} (เวลาไทย)")
    if re.fullmatch(r"(?:สรุป(?:รายจ่าย)?|ยอดรายจ่าย)(?:วันนี้|เมื่อวาน(?:นี้)?|วานนี้)?(?:หน่อย|ครับ|ค่ะ)?|summary", compact):
        if evidence.value is not None:
            return ToolRequest(name="spending_summary", arguments={"start_date": evidence.value, "end_date": evidence.value})
    return None


FILLER = r"(?:\s|[,;:|]|วันที่|เมื่อกี้|เพิ่ง|รายจ่าย|จ่าย|ซื้อ|บันทึก|รายการ|มียอด|ยอดเงิน|ยอด|จำนวน|เป็นเงิน|ราคา|ค่า|เงิน|บาท|ไป|เป็น|ให้|หน่อย|ครับ|ค่ะ|คะ|นะ)*"
PREFIX = r"^(?:(?:เมื่อกี้|เพิ่ง|รายจ่าย|จ่าย|ซื้อ|บันทึก|รายการ|ค่า|เงิน|ยอดเงิน|ยอด|จำนวน|เป็นเงิน|ราคา|มียอด)\s*)+"
SUFFIX = r"(?:\s*(?:บาท|ไป|ครับ|ค่ะ|คะ|นะ|หน่อย))+$"


def erase(text: str, spans) -> str:
    value = list(text)
    for a,b in spans:
        value[a:b] = " " * (b-a)
    return "".join(value)


def item_spans(text: str, catalog: ItemCatalog):
    found = []
    # Longest overlapping master names win; separate non-overlapping names remain.
    for key, canonical in {**catalog.items, **catalog.aliases}.items():
        for m in re.finditer(re.escape(key), text, re.I):
            a,b = m.span()
            if (a and text[a-1].isascii() and text[a-1].isalnum()) or (b < len(text) and text[b].isascii() and text[b].isalpha()):
                continue
            found.append((a,b,canonical))
    chosen = []
    for value in sorted(found, key=lambda v: (-(v[1]-v[0]), v[0])):
        if not any(value[0] < b and value[1] > a for a,b,_ in chosen):
            chosen.append(value)
    return sorted(chosen)


@dataclass(frozen=True)
class ExpenseDraft:
    entries: list[dict]
    missing: tuple[str, ...]
    context_date: str

    def request(self):
        if self.missing:
            raise ValueError("Incomplete expense source")
        return ToolRequest(name="add_transactions", arguments={"entries": self.entries})


@dataclass(frozen=True)
class ExpensePreview:
    request: ToolRequest


def candidate_context(text: str, occurred_at: datetime, catalog: ItemCatalog, source):
    names = [canonical for _,_,canonical in item_spans(text, catalog)]
    if source:
        for entry in source.entries:
            if entry["item"]:
                match = catalog.match(entry["item"])
                names.extend([match.canonical] if match.canonical else match.suggestions)
    else:
        ds = dates(text, occurred_at)
        ms = amounts(text, ds.spans)
        query = erase(text, (*ds.spans, *((v.start,v.end) for v in ms)))
        query = re.sub(PREFIX, "", query.strip())
        query = re.sub(SUFFIX, "", query).strip()
        names.extend(catalog.match(query).suggestions if query else ())
    # Retrieval is bounded local exact/fuzzy lookup, not a nearest-name verdict.
    result = {"catalog_candidates": list(dict.fromkeys(names))[:5]}
    if source:
        result["source_fields"] = source.entries
    return result


def expense_source(text: str, occurred_at: datetime, catalog: ItemCatalog) -> ExpenseDraft | None:
    if intent_guard(text) or len(text) > 2000 or "รายรับ" in text:
        return None
    lines = re.split(r"\n|\s+(?:กับ|และ)\s+", text)
    if not 1 <= len(lines) <= 5:
        return None
    entries, missing = [], set()
    for line in lines:
        evidence = dates(line, occurred_at)
        names = item_spans(line, catalog)
        if len(names) > 1:
            return None
        values = amounts(line, (*evidence.spans, *((a,b) for a,b,_ in names)))
        if len(values) > 1:
            return None
        spans = [*evidence.spans, *((a.start,a.end) for a in values)]
        if names:
            a,b,_ = names[0]
            if any(a < end and b > start for start,end in evidence.spans):
                return None  # An item that resembles a date needs disambiguation.
            item = line[a:b]  # Preserve raw spelling for the ordinary catalog port.
            spans.append((a,b))
        else:
            remainder = erase(line, spans).strip()
            item = re.sub(PREFIX, "", remainder).strip()
            item = re.sub(SUFFIX, "", item).strip()
            if item and (len(item) > 200 or re.search(r"[\d.,]|วันที่|เตือน|ประชุม|นัด|ตาราง|สรุป", item)):
                return None
            # A bounded unknown name is preserved, never invented/classified.
            if item:
                start = line.find(item)
                if start < 0:
                    return None
                spans.append((start,start+len(item)))
        if not re.fullmatch(FILLER, erase(line, spans)):
            return None
        amount = values[0].value if values else None
        if not item:
            missing.add("item")
        if amount is None:
            missing.add("amount")
        if evidence.value is None:
            missing.add("date")
        if not item and amount is None:
            return None
        entries.append({"item": item or None, "amount": amount, "kind": "expense",
                        "date": evidence.value.isoformat() if evidence.value else None, "note": ""})
    return ExpenseDraft(entries, tuple(sorted(missing)), occurred_at.astimezone(BANGKOK).date().isoformat())


def supported_expense_fields(entry, text: str, occurred_at: datetime) -> bool:
    evidence = dates(text, occurred_at)
    if evidence.value is None or entry.date != evidence.value:
        return False
    matches = list(re.finditer(re.escape(entry.item), text, re.I))
    if not matches:
        return False
    excluded = (*evidence.spans, *((m.start(),m.end()) for m in matches))
    values = amounts(text, excluded)
    return any(v.value is not None and Decimal(v.value) == Decimal(entry.amount) for v in values)
