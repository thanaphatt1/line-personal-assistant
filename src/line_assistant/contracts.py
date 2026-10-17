"""Server-validated tool contracts. Tool arguments never contain authorization."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Money = Annotated[str, StringConstraints(pattern=r"^(?:0|[1-9]\d{0,6})(?:\.\d{1,2})?$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Transaction(Contract):
    item: Text
    amount: Money  # Decimal string in THB; no float rounding at the tool boundary.
    kind: Literal["income", "expense"]
    date: date
    note: Annotated[str, StringConstraints(max_length=500)] = ""

    @model_validator(mode="after")
    def positive_amount(self):
        if Decimal(self.amount) <= 0:
            raise ValueError("amount must be positive")
        return self


class AddTransactions(Contract):
    entries: list[Transaction] = Field(min_length=1, max_length=20)


class SpendingSummary(Contract):
    start_date: date
    end_date: date  # Inclusive calendar date, local to Asia/Bangkok.

    @model_validator(mode="after")
    def ordered(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if (self.end_date - self.start_date).days > 366:
            raise ValueError("Summary window is limited to 367 dates")
        return self


def valid_interval(start: datetime, end: datetime):
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Explicit timezone offsets are required")
    if end <= start:
        raise ValueError("end must be after start")


class CreateEvent(Contract):
    title: Text
    start: datetime
    end: datetime
    reminder_minutes: Annotated[int, Field(strict=True, ge=0, le=40320)] | None = None

    @model_validator(mode="after")
    def ordered(self):
        valid_interval(self.start, self.end)
        if (self.end - self.start).total_seconds() > 7 * 86400:
            raise ValueError("Mock events are limited to seven days")
        return self


class ListEvents(Contract):
    start: datetime
    end: datetime  # Exclusive; events overlapping this interval are returned.

    @model_validator(mode="after")
    def ordered(self):
        valid_interval(self.start, self.end)
        if (self.end - self.start).total_seconds() > 31 * 86400:
            raise ValueError("Agenda query window is limited to 31 days")
        return self


class UpdateEvent(Contract):
    event_ref: Text  # Server resolves 'last'; calendar ID is never model-controlled.
    title: Text | None = None
    start: datetime | None = None
    end: datetime | None = None

    @model_validator(mode="after")
    def patch(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Supply both start and end when changing times")
        if self.start is not None:
            valid_interval(self.start, self.end)
            if (self.end - self.start).total_seconds() > 7 * 86400:
                raise ValueError("Mock events are limited to seven days")
        if self.title is None and self.start is None:
            raise ValueError("Supply at least one change")
        return self


class DeleteEvent(Contract):
    event_ref: Text  # Always proposes deletion; no 'confirmed' argument exists.


TOOL_MODELS = {
    "add_transactions": AddTransactions,
    "spending_summary": SpendingSummary,
    "create_event": CreateEvent,
    "list_events": ListEvents,
    "update_event": UpdateEvent,
    "delete_event": DeleteEvent,
}


class ToolRequest(Contract):
    name: Literal[
        "add_transactions", "spending_summary", "create_event", "list_events",
        "update_event", "delete_event",
    ]
    arguments: dict

    def validated_arguments(self) -> Contract:
        return TOOL_MODELS[self.name].model_validate(self.arguments)


def tool_schemas() -> dict:
    """Generic JSON Schemas; deliberately not tied to an unverified model API."""
    return {name: model.model_json_schema() for name, model in TOOL_MODELS.items()}
