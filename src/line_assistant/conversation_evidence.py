"""Source-only category choices. Model IDs never establish owner selection."""
import re
from .catalog import normalized
from .catalog import ItemCatalog
from .conversation_dates import resolve
from .language import FILLER, amounts, erase, expense_source
from .source_spans import finditer


def command_text(text):
    return re.split(r'(?:หมายเหตุ|\bnote)\s*:?', text, maxsplit=1, flags=re.I)[0].strip()


def edited_item(text, proposed):
    command = command_text(text)
    match = re.fullmatch(r'(?:แก้|เปลี่ยน)(?:ชื่อ(?:รายการ)?|รายการ)?\s*(?:เป็น|คือ)\s*(.+?)(?:ครับ|ค่ะ)?', command)
    if match and normalized(proposed) in normalized(match[1]):
        value = match[1].strip()
        if len(value) > 200: raise ValueError('Bounded full item name required')
        return value
    return proposed


def item_selection(text, item):
    return bool(re.fullmatch(r'(?:(?:หมายถึง|เลือก(?:รายการ)?|ใช้ชื่อ|ชื่อที่ถูกต้องคือ|รายการคือ|คือ)\s*)?' +
        re.escape(item) + r'\s*(?:ครับ|ค่ะ)?', command_text(text), re.I))


def grounded_item(text, proposed, at, taxonomy):
    """Keep meaningful modifiers; source grammar never chooses a catalog mapping."""
    command = command_text(text)
    names = finditer(command, proposed)
    if len(names) != 1: return proposed
    spans = resolve(command, at)[2]
    money = amounts(command, (*spans, names[0].span()))
    if len(money) != 1 or money[0].value is None: return proposed
    outside = erase(command, (*spans, (money[0].start, money[0].end), names[0].span()))
    if re.fullmatch(r'(?:(?:มี|กิน|ดื่ม)|' + FILLER + r')*', outside): return proposed
    source = expense_source(command, at, ItemCatalog([], {}))
    if source and len(source.entries) == 1:
        raw = source.entries[0]['item']
        if raw and normalized(proposed) in normalized(raw): return raw
    if taxonomy.catalog.match(proposed).status == 'exact':
        raise ValueError('Known item has unexplained source modifiers; clarify the full name')
    return proposed


def category_choice(text, taxonomy, *, allow_bare, item=''):
    command = command_text(text)
    if any(mark in command for mark in ('"', '“', '”', "'")):
        return [], False
    command = re.sub(r'(?:ครับ|ค่ะ)\s*$', '', command).strip()
    # A new-name decision and its category may be one owner answer. Retain a
    # full directed choice; arbitrary prose/quotes/negation never becomes one.
    command = re.sub(r'^(?:เป็น\s*)?รายการใหม่\s*[,;:]?\s*(?=(?:เพิ่ม|ใส่|จัด|add)\s*(?:ใน|in\b|into\b))','',command,flags=re.I)
    if re.match(r'^(?:ไม่|อย่า|ห้าม|not\b|never\b)', command, re.I) and re.search(r'หมวด(?!ย่อย)|\bcategory', command, re.I):
        return [], 'rejected'
    verb = r'(?:เลือก|เปลี่ยน|ใช้|เอา|เพิ่ม|ใส่|จัด|choose|use|change|add)'
    target = r'(?:รายการ(?:นี้|ใหม่)?|ชื่อใหม่|' + re.escape(item) + r')' if item else r'(?:รายการ(?:นี้|ใหม่)?|ชื่อใหม่)'
    directed = re.fullmatch(r'(?:(?:ช่วย|ขอ|กรุณา)\s*)?(?:' + verb + r'\s*(?:' + target +
        r')?\s*(?:ใน|เป็น|to|as)?\s*)?(?:หมวด(?:หมู่)?(?!ย่อย)|category)\s*[:=]?\s*(.+)', command, re.I)
    if not directed:
        directed = re.fullmatch(r'(?:(?:ช่วย|ขอ|กรุณา)\s*)?(?:เพิ่ม|ใส่|จัด|add)\s*(?:ใน|into\b|in\b)\s*(?:(?:หมวด(?:หมู่)?(?!ย่อย)|category)\s*)?(.+)',command,re.I)
    value = directed[1].strip() if directed else command if allow_bare else ''
    value = re.split(r'\s*(?:หมวดย่อย|subcategory)\s*[:=]?', value, maxsplit=1, flags=re.I)[0].strip()
    ids = taxonomy.category_matches(value) if value else []
    return ids, bool(directed or (allow_bare and ids))


def subcategory_choice(text, taxonomy, category):
    command = command_text(text)
    if any(mark in command for mark in ('"', '“', '”', "'")):
        return [], False
    command = re.sub(r'(?:ครับ|ค่ะ)\s*$', '', command).strip()
    if re.match(r'^(?:ไม่|อย่า|ห้าม|not\b|never\b)', command, re.I) and re.search(r'หมวดย่อย|\bsubcategory', command, re.I):
        return [], 'rejected'
    directed = re.search(r'(?:หมวดย่อย|subcategory)\s*[:=]?\s*(.+)$', command, re.I)
    if directed and not re.fullmatch(r'(?:(?:เลือก|ใช้|เปลี่ยน)\s*)?(?:หมวดย่อย|subcategory)\s*[:=]?\s*.+', command, re.I):
        # A combined explicit category+subcategory declaration is also a choice.
        if not re.match(r'^(?:หมวด(?!ย่อย)|เลือกหมวด|ใช้หมวด|เพิ่มในหมวด|เปลี่ยนหมวด|category)', command, re.I):
            return [], False
    value = directed[1].strip() if directed else command
    ids = [key for key, label in taxonomy.pairs.get(category, {}).items()
           if label and normalized(label) == normalized(value)]
    return ids, bool(directed or ids)
