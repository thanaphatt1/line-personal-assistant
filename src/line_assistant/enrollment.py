"""Frozen desired mapping plus identified expense append in one Sheets batch."""
from pydantic import BaseModel, ConfigDict, Field
from .contracts import Transaction, AddTransactions
from .google_requests import identified_append_preview
from .catalog import normalized

class Mapping(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    item: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=200)
    subcategory: str = Field(max_length=200)

class Enrollment(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    entries: list[Transaction] = Field(min_length=1, max_length=5)
    catalog: list[Mapping] = Field(min_length=1, max_length=5)

    def checked(self):
        AddTransactions(entries=self.entries)
        keys = [normalized(v.item) for v in self.catalog]
        if len(keys) != len(set(keys)) or any(v.item not in {e.item for e in self.entries} for v in self.catalog):
            raise ValueError('Each new mapping must uniquely belong to this expense')
        return self

def batch_body(payload, target, key, catalog_to_append):
    plan = Enrollment.model_validate(payload).checked()
    body = identified_append_preview(AddTransactions(entries=plan.entries), target.expense_sheet_id,
                                     target.transaction_id_metadata, key).body
    if catalog_to_append:
        body['requests'].insert(0, {'appendCells': {'sheetId': target.database_sheet_id,
            'rows': [{'values': [{'userEnteredValue': {'stringValue': value}}
                                for value in (row.item, row.category, row.subcategory)]} for row in catalog_to_append],
            'fields': 'userEnteredValue'}})
    return body
