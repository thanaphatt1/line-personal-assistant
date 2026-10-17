"""Separate, atomic fake provider ledgers for local crash/restart experiments.

The fake side effect and its receipt commit together. Real Sheets does not offer
this contract. Set reconciliation_available=False to model that missing proof.
"""

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .adapters import CalendarEvent, MockCalendar, MockSheets, Reconciliation, SheetLayout
from .contracts import Transaction
from .persistence import SQLiteStore, jsonable


def event_from(value):
    return CalendarEvent(**{**value, "start": datetime.fromisoformat(value["start"]),
                            "end": datetime.fromisoformat(value["end"])})


class DurableMockSheets(MockSheets):
    def __init__(self, layout: SheetLayout, path: Path, reconciliation_available: bool = True, phase_hook=None):
        super().__init__(layout)
        self.store = SQLiteStore(path)
        self.reconciliation_available = reconciliation_available
        self.phase_hook = phase_hook or (lambda phase, key: None)
        self._call("summary", datetime.min.date(), datetime.max.date())

    def _call(self, method, *args):
        with self.store.transaction() as connection:
            value = self.store.read(connection, "sheets")
            if value:
                if value["layout"] != self._layout():
                    raise ValueError("Existing mock sheet layout differs")
                self.entries = [Transaction.model_validate(entry) for entry in value["entries"]]
                self.cell_values, self.write_log = value["cell_values"], value["write_log"]
                self.effects.receipts = {key: (receipt[0], tuple(receipt[1]))
                                         for key, receipt in value["receipts"].items()}
            result = getattr(super(), method)(*args)
            self.store.write(connection, "sheets", {
                "layout": self._layout(), "entries": [entry.model_dump(mode="json") for entry in self.entries],
                "cell_values": self.cell_values, "write_log": self.write_log,
                "receipts": self.effects.receipts,
            })
            if method == "add":
                self.phase_hook("provider_staged", args[-1])
        if method == "add":
            self.phase_hook("provider_committed", args[-1])
        return result

    def _layout(self):
        return {"columns": dict(self.layout.columns), "formula_columns": list(self.layout.formula_columns),
                "date_mode": self.layout.date_mode}

    def add(self, entries, key):
        return self._call("add", entries, key)

    def summary(self, start, end):
        return self._call("summary", start, end)

    def reconcile(self, key, payload):
        if not self.reconciliation_available:
            return Reconciliation("unknown")
        return self._call("reconcile", key, payload)


class DurableMockCalendar(MockCalendar):
    def __init__(self, path: Path, reconciliation_available: bool = True, phase_hook=None):
        super().__init__()
        self.store = SQLiteStore(path)
        self.reconciliation_available = reconciliation_available
        self.phase_hook = phase_hook or (lambda phase, key: None)
        self._call("list", datetime.min.replace(tzinfo=timezone.utc), datetime.min.replace(tzinfo=timezone.utc))

    def _call(self, method, *args):
        with self.store.transaction() as connection:
            value = self.store.read(connection, "calendar")
            if value:
                self.events = {key: event_from(event) for key, event in value["events"].items()}
                self.effects.receipts = {key: (receipt[0], event_from(receipt[1]) if isinstance(receipt[1], dict) else receipt[1])
                                         for key, receipt in value["receipts"].items()}
            # MockCalendar mutation methods call self.get; use the loaded snapshot
            # directly during this provider transaction to avoid nested SQLite locks.
            self._in_transaction = True
            try:
                result = getattr(super(), method)(*args)
            finally:
                self._in_transaction = False
            self.store.write(connection, "calendar", jsonable({
                "events": {key: asdict(event) for key, event in self.events.items()},
                "receipts": {key: [digest, asdict(receipt) if isinstance(receipt, CalendarEvent) else receipt]
                             for key, (digest, receipt) in self.effects.receipts.items()},
            }))
            if method in {"create", "update", "delete"}:
                self.phase_hook("provider_staged", args[-1])
        if method in {"create", "update", "delete"}:
            self.phase_hook("provider_committed", args[-1])
        return result

    def create(self, args, key):
        return self._call("create", args, key)

    def get(self, event_id):
        if getattr(self, "_in_transaction", False):
            return super().get(event_id)
        return self._call("get", event_id)

    def list(self, start, end):
        return self._call("list", start, end)

    def update(self, event_id, args, version, key):
        return self._call("update", event_id, args, version, key)

    def delete(self, event_id, version, key):
        return self._call("delete", event_id, version, key)

    def reconcile(self, key, payload):
        if not self.reconciliation_available:
            return Reconciliation("unknown")
        return self._call("reconcile", key, payload)
