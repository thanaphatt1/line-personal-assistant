"""Small domain-tool contracts; no commit, approval or arbitrary success tool."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .model_api import object_schema


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class Search(Arguments):
    query: str = Field(max_length=200)
    offset: int = Field(ge=0, le=10000)


class Patch(Arguments):
    entry_id: str | None = Field(default=None, max_length=40)
    result_set_id: str | None = Field(default=None, max_length=40)
    item_id: str | None = Field(default=None, max_length=40)
    category_id: str | None = Field(default=None, max_length=40)
    subcategory_id: str | None = Field(default=None, max_length=40)
    item: str | None = Field(default=None, max_length=200)
    amount: str | None = Field(default=None, max_length=30, pattern=r'^\d+(?:\.\d{1,2})?$')
    note: str | None = Field(default=None, max_length=300)
    day: int | None = Field(default=None, ge=0, le=99)
    month: int | None = Field(default=None, ge=0, le=99)
    year: int | None = Field(default=None, ge=0, le=9999)
    date_source: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=200)
    subcategory: str | None = Field(default=None, max_length=200)


class Edit(Arguments):
    mode: Literal['create', 'update']
    entries: list[Patch] = Field(min_length=1, max_length=5)


class Empty(Arguments):
    pass


class Summary(Arguments):
    start_date: str = Field(max_length=30)
    end_date: str = Field(max_length=30)


MODELS = {'catalog_search': Search, 'category_search': Search, 'draft_edit': Edit,
          'preview_draft': Empty, 'summary_query': Summary}


def tools():
    text = lambda: {'type': ['string', 'null']}
    integer = lambda: {'type': ['integer', 'null']}
    patch = object_schema({
        **{k: text() for k in ('entry_id', 'result_set_id', 'item_id', 'category_id', 'subcategory_id')},
        **{k: text() for k in ('item', 'amount', 'note', 'date_source', 'category', 'subcategory')},
        **{k: integer() for k in ('day', 'month', 'year')}})
    patch['properties']['note']['description'] = 'Null preserves; full owner note field; empty only explicit clear, never item tags.'
    patch['properties']['entry_id']['description'] = 'Create null in source line order. Update exact context entry_ids, never a position.'
    patch['properties']['result_set_id']['description'] = 'Selected IDs require latest search result_set_id, never consent.'
    patch['properties']['amount']['description'] = 'Decimal string, e.g. 35.50; no currency. Null preserves.'
    patch['properties']['amount']['pattern'] = r'^\d+(?:\.\d{1,2})?$'
    definitions = [
        ('catalog_search', 'Search distinctive item words without payment verbs/ค่า. Use returned IDs; do not repeat a found search. Bounded page.',
         object_schema({'query': {'type': 'string'}, 'offset': {'type': 'integer'}})),
        ('category_search', 'Search existing categories/pairs by label/component. Reuse found IDs; not enrollment consent. Bounded page.',
         object_schema({'query': {'type': 'string'}, 'offset': {'type': 'integer'}})),
        ('draft_edit', 'Reversible draft. Null preserves. Current entry IDs for update; current date span. source_context grounds recovery. Returns runtime review. No consent.',
         object_schema({'mode': {'type': 'string', 'enum': ['create', 'update']},
                        'entries': {'type': 'array', 'items': patch}})),
        ('preview_draft', 'Finish with runtime-owned review or missing-field question; never writes.', object_schema({})),
        ('summary_query', 'Finish an explicit owner expense-summary request. Preserve any active draft.',
         object_schema({'start_date': {'type': 'string'}, 'end_date': {'type': 'string'}})),
    ]
    return [{'type': 'function', 'name': n, 'description': d, 'strict': True, 'parameters': s}
            for n, d, s in definitions]
