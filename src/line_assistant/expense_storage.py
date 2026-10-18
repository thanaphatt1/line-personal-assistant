"""Expense/catalog boundary. Provider coordinates never become tool arguments.

The historical ``sheets`` host attribute remains a compatibility name. Its
scope is a storage identity, not necessarily a spreadsheet. Sheets scopes keep
their original bytes so persisted reviews and receipts remain readable.
"""
from typing import Protocol, TypedDict
from .adapters import Reconciliation
from .contracts import Transaction
from .taxonomy import Taxonomy


class ExpenseEntry(TypedDict):
    date: str
    item: str
    amount: str
    note: str


class ClassifiedExpense(ExpenseEntry):
    transaction_id: str | None


class ExpenseStore(Protocol):
    @property
    def scope_id(self) -> str: ...
    def taxonomy(self) -> Taxonomy: ...
    def catalog(self): ...
    def saved_entries(self, start, end) -> list[ExpenseEntry]: ...
    def classification_entries(self) -> list[ClassifiedExpense]: ...
    def summary(self, start, end) -> dict: ...
    def add(self, entries: list[Transaction], key: str) -> tuple[str, ...]: ...
    def enroll_and_add(self, payload: dict, key: str) -> tuple[str, ...]: ...
    def reconcile(self, key: str, payload: dict) -> Reconciliation: ...
    def reconcile_enrollment(self, key: str, payload: dict) -> Reconciliation: ...


class SavedExpenseStore(Protocol):
    def records(self, start=None, end=None, *, ids=None) -> dict: ...
    def get(self, identifier: str) -> dict: ...
    def apply(self, payload: dict, key: str, *, bind_request) -> dict: ...
    def reconcile(self, key: str, payload: dict) -> Reconciliation: ...


class MappingStore(Protocol):
    def apply(self, payload: dict, key: str, *, bind) -> dict: ...
    def reconcile(self, key: str, payload: dict) -> Reconciliation: ...


def storage_scope(store) -> str:
    scope = getattr(store, 'scope_id', None)
    # Legacy synthetic adapters and persisted Sheets fixtures keep working.
    if scope is None:
        scope = getattr(getattr(store, 'target', None), 'spreadsheet_id', None)
    if not isinstance(scope, str) or not scope:
        raise ValueError('Explicit expense storage scope required')
    return scope


def mapping_editor(store, *, enabled=False, single_writer_reviewed=False):
    from .firestore_expenses import FirestoreExpenseStore, FirestoreMappingEditor
    if type(store) is FirestoreExpenseStore:
        return FirestoreMappingEditor(store, enabled=enabled)
    from .provisional_categories import MappingEditor
    return MappingEditor(store, enabled=enabled, single_writer_reviewed=single_writer_reviewed)
