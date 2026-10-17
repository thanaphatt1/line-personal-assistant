"""Owner field evidence for optional notes; contents are literal data.

Use the existing note/หมายเหตุ field boundary, not word guesses or an item-tag
blacklist. Old drafts are never cleaned from their content alone.
"""
from dataclasses import dataclass
import re
from .source_spans import finditer


@dataclass(frozen=True)
class NoteError(ValueError):
    code: str


LABEL = re.compile(r'(?:(?<!\S)|(?<=แก้)|(?<=เพิ่ม)|(?<=ใส่)|(?<=เปลี่ยน))'
                   r'(?P<label>หมายเหตุ|note\b)\s*(?P<colon>:?)\s*', re.I)
CLEAR = re.compile(r'(?:(?:ช่วย|กรุณา|please)\s*)?'
                   r'(?:(?:ลบ|ล้าง)\s*หมายเหตุ(?:เดิม)?|(?:clear|remove|delete)\s+(?:the\s+)?note)'
                   r'\s*(?:ครับ|ค่ะ|please)?', re.I)
EDIT = re.compile(r'(?:แก้|เปลี่ยน|set|change)\s*$', re.I)


def section(source, item):
    """Find a field label outside the complete source-backed item name.

    A marker mentioned inside a known item is not a note field. The first field
    boundary owns the complete trailing value, including later labels/quotes.
    """
    spans = [m.span() for m in finditer(source,item)] if item else []
    for match in LABEL.finditer(source):
        if any(match.start('label') < end and match.end('label') > start for start, end in spans):
            continue
        value = source[match.end():].strip()
        if EDIT.search(source[:match.start('label')]):
            value = re.sub(r'^(?:เป็น|คือ|to\b|as\b)\s*', '', value, count=1, flags=re.I)
        if len(value) > 300:
            raise NoteError('note_too_long')
        return value, bool(match['colon'])
    return None


def apply_note(proposed, previous, source, item, *, creating):
    """Null preserves; nonempty supplies; empty clears only with owner evidence.

    A create starts blank. Ignore an unsupported optional proposal, preserving
    valid required fields and reporting a warning. A changed existing note needs
    evidence or clarification. Repeated unchanged values need no new provenance.
    """
    if proposed is None:
        return previous, None
    evidence = section(source, item)
    if proposed == '':
        explicit_blank = evidence == ('', True)
        if CLEAR.fullmatch(source.strip()) or explicit_blank:
            return '', None
        if previous == '' and evidence is None:
            return '', None
        raise NoteError('note_clear_not_requested')
    if evidence and evidence[0]:
        # Source is authoritative for the complete explicitly supplied value.
        # This also restores meaningful prefixes that a model drops from a note.
        value = evidence[0]
        return value, 'note_source_restored' if proposed != value else None
    if proposed == previous and evidence is None:
        return previous, None
    if creating:
        return previous, 'note_ignored_no_owner_source'
    raise NoteError('note_source_required')
