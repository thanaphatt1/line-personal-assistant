"""Mock providers and sparse write plans. No SDKs, HTTP calls, or credentials."""

from dataclasses import dataclass, replace
from datetime import date,datetime
from decimal import Decimal
import hashlib
import json
import re
from types import MappingProxyType
from typing import Callable, Protocol, TypeVar
from zoneinfo import ZoneInfo

from .contracts import CreateEvent, Transaction, UpdateEvent
from .sheet_dates import date_to_serial

BANGKOK = ZoneInfo("Asia/Bangkok")
T = TypeVar("T")


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class Reconciliation:
    # Absence is conclusive only in these atomic mock provider ledgers.
    outcome: str  # committed | not_committed | unknown
    receipt: object = None


class Effects:
    """Provider-side deduplication contract, in memory for this local mock only."""

    def __init__(self):
        self.receipts: dict[str, tuple[str, object]] = {}

    def once(self, key: str, payload: object, apply: Callable[[], T]) -> T:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        if key in self.receipts:
            previous, receipt = self.receipts[key]
            if previous != digest:
                raise Conflict("An idempotency key cannot be reused with another payload")
            return receipt
        receipt = apply()
        self.receipts[key] = (digest, receipt)
        return receipt

    def reconcile(self, key: str, payload: object) -> Reconciliation:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        if key not in self.receipts:
            return Reconciliation("not_committed")
        previous, receipt = self.receipts[key]
        if previous != digest:
            raise Conflict("Receipt payload differs from the frozen action")
        return Reconciliation("committed", receipt)


@dataclass(frozen=True)
class SheetLayout:
    columns: dict[str, str]
    formula_columns: tuple[str, ...]
    schema_verified: bool = False
    date_mode: str = "iso_mock"

    def __post_init__(self):
        object.__setattr__(self, "columns", MappingProxyType(dict(self.columns)))
        object.__setattr__(self, "formula_columns", tuple(self.formula_columns))
        if set(self.columns) != {"date", "item", "amount", "note"}:
            raise ValueError("Explicit mapping for all mock input fields is required")
        all_columns = list(self.columns.values()) + list(self.formula_columns)
        if any(not re.fullmatch(r"[A-Z]{1,3}", col) for col in all_columns):
            raise ValueError("Columns must be A1 column letters")
        if len(set(all_columns)) != len(all_columns):
            raise ValueError("Input cells must not overlap each other or formula columns")
        if self.date_mode not in {"iso_mock", "serial_date"}:
            raise ValueError("Unsupported date encoding")

    def cells(self, row: int, entry: Transaction) -> dict[str, object]:
        if row < 1:
            raise ValueError("Rows are one-based")
        values = {
            "date": date_to_serial(entry.date) if self.date_mode == "serial_date" else entry.date.isoformat(),
            "item": entry.item,
            "amount": float(Decimal(entry.amount)), "note": entry.note,
        }
        # Sparse input cells only: no row-wide blanks and no formula replacements.
        return {f"{self.columns[key]}{row}": value for key, value in values.items()}


class Sheets(Protocol):
    def add(self, entries: list[Transaction], key: str) -> tuple[str, ...]: ...
    def summary(self, start, end) -> dict[str, str]: ...
    def reconcile(self, key: str, payload: dict) -> Reconciliation: ...


class MockSheets:
    value_input_option = "RAW"

    def __init__(self, layout: SheetLayout):
        self.layout = layout
        self.effects = Effects()
        self.entries: list[Transaction] = []
        self.cell_values: dict[str, object] = {}
        self.write_log: list[dict[str, object]] = []

    def add(self, entries: list[Transaction], key: str) -> tuple[str, ...]:
        if any(entry.kind != "expense" for entry in entries):
            raise ValueError("Income requires an independent verified storage mapping")
        def apply():
            first_row = len(self.entries) + 1  # Synthetic rows; not a live append strategy.
            ids = []
            for offset, entry in enumerate(entries):
                row = first_row + offset
                for col in self.layout.formula_columns:
                    self.cell_values[f"{col}{row}"] = "<existing-sheet-formula>"
                writes = self.layout.cells(row, entry)
                self.cell_values.update(writes)
                self.write_log.append(writes)
                ids.append(f"mock-row-{row}")
            self.entries.extend(entries)
            return tuple(ids)
        return self.effects.once(key, [entry.model_dump(mode="json") for entry in entries], apply)

    def summary(self, start, end) -> dict[str, str]:
        totals = {"income": Decimal("0"), "expense": Decimal("0")}
        for entry in self.entries:
            if start <= entry.date <= end:
                totals[entry.kind] += Decimal(entry.amount)
        return {key: f"{value:.2f}" for key, value in totals.items()}

    def reconcile(self, key: str, payload: dict) -> Reconciliation:
        return self.effects.reconcile(key, payload["entries"])


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    title: str
    start: datetime|date
    end: datetime|date
    reminder_minutes: int | None
    version: int | str = 1  # Mock integer or a live API ETag, held by the server.
    native_reminders: dict | None = None

    def as_dict(self) -> dict:
        reminders = {"useDefault": True} if self.reminder_minutes is None else {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": self.reminder_minutes}],
        }
        if self.native_reminders is not None:
            reminders = self.native_reminders
        if type(self.start) is date:
            return {'id':self.id,'title':self.title,'start':self.start.isoformat(),'end':self.end.isoformat(),
                'all_day':True,'reminders':reminders,'version':self.version}
        return {
            "id": self.id, "title": self.title,
            "start": self.start.astimezone(BANGKOK).isoformat(),
            "end": self.end.astimezone(BANGKOK).isoformat(),
            "timezone": "Asia/Bangkok", "reminders": reminders, "version": self.version,
        }


class Calendar(Protocol):
    def create(self, args: CreateEvent, key: str) -> CalendarEvent: ...
    def get(self, event_id: str) -> CalendarEvent: ...
    def list(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...
    def update(self, event_id: str, args: UpdateEvent, version: int, key: str) -> CalendarEvent: ...
    def delete(self, event_id: str, version: int, key: str) -> str: ...
    def reconcile(self, key: str, payload: dict) -> Reconciliation: ...


class MockCalendar:
    calendar_id = "mock-calendar"  # Server-owned, never a tool argument.

    def __init__(self):
        self.events: dict[str, CalendarEvent] = {}
        self.effects = Effects()

    def create(self, args: CreateEvent, key: str) -> CalendarEvent:
        def apply():
            event_id = hashlib.sha256(key.encode()).hexdigest()
            event = CalendarEvent(event_id, args.title, args.start, args.end, args.reminder_minutes)
            self.events[event_id] = event
            return event
        return self.effects.once(key, args.model_dump(mode="json"), apply)

    def get(self, event_id: str) -> CalendarEvent:
        if event_id not in self.events:
            raise NotFound("Event is unavailable in the configured calendar")
        return self.events[event_id]

    def list(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return sorted(
            (event for event in self.events.values() if event.start < end and event.end > start),
            key=lambda event: (event.start, event.id),
        )

    def update(self, event_id: str, args: UpdateEvent, version: int, key: str) -> CalendarEvent:
        def apply():
            previous = self.get(event_id)
            if previous.version != version:
                raise Conflict("Event changed; review it again")
            event = replace(previous, title=args.title or previous.title,
                            start=args.start or previous.start, end=args.end or previous.end,
                            version=previous.version + 1)
            self.events[event_id] = event
            return event
        payload = {"id": event_id, "version": version, "patch": args.model_dump(mode="json")}
        return self.effects.once(key, payload, apply)

    def delete(self, event_id: str, version: int, key: str) -> str:
        def apply():
            if self.get(event_id).version != version:
                raise Conflict("Event changed since deletion was proposed")
            del self.events[event_id]
            return event_id
        return self.effects.once(key, {"id": event_id, "version": version}, apply)

    def reconcile(self, key: str, payload: dict) -> Reconciliation:
        return self.effects.reconcile(key, payload)
