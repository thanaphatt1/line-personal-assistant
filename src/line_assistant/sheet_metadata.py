"""Server-owned receipt metadata, separate from the A:D transaction contract."""

from dataclasses import dataclass
import hashlib
import re


def column_index(column):
    if not isinstance(column, str) or not re.fullmatch(r"[A-Z]{1,3}", column):
        raise ValueError("A1 column letters required")
    result = 0
    for character in column:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def column_letters(index):
    if type(index) is not int or index < 1:
        raise ValueError("One-based column index required")
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


@dataclass(frozen=True)
class TransactionIdMetadata:
    column: str
    header: str = "transaction_id"
    schema_approved: bool = False
    require_hidden: bool = True

    def __post_init__(self):
        if (self.index <= 4 or not isinstance(self.header, str) or not self.header or len(self.header) > 100
                or type(self.schema_approved) is not bool or type(self.require_hidden) is not bool):
            raise ValueError("ID metadata must be a dedicated non-input column")

    @property
    def index(self):
        return column_index(self.column)  # one-based

    @property
    def zero_based_index(self):
        return self.index - 1


def transaction_id(key, index):
    return hashlib.sha256(f"{key}:{index}".encode()).hexdigest()
