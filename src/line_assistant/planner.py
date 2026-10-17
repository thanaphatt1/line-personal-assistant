"""Small deterministic Thai demo grammar in place of any paid model call."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import re
from typing import Protocol

from .adapters import BANGKOK
from .contracts import ToolRequest


@dataclass(frozen=True)
class CalendarClarification:
    title: str


@dataclass(frozen=True)
class PlannerNotice:
    status: str
    message: str


class Planner(Protocol):
    def plan(self, text: str, occurred_at: datetime) -> ToolRequest | CalendarClarification | PlannerNotice | None: ...


def local_datetime(value: str) -> datetime:
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value.strip()):
        raise ValueError("Specify both date and time; a date alone is insufficient")
    parsed = datetime.fromisoformat(value.strip())
    # This demo grammar explicitly interprets offset-free written times as Bangkok.
    return parsed.replace(tzinfo=BANGKOK) if parsed.tzinfo is None else parsed.astimezone(BANGKOK)


def day_window(occurred_at: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(occurred_at.astimezone(BANGKOK).date(), time.min, BANGKOK)
    return start, start + timedelta(days=1)


class RulePlanner:
    def plan(self, text: str, occurred_at: datetime) -> ToolRequest | CalendarClarification | None:
        text = text.strip()
        if text in {"วันนี้มีอะไร", "วันนี้มีอะไรบ้าง", "ตารางวันนี้", "today"}:
            start, end = day_window(occurred_at)
            return ToolRequest(name="list_events", arguments={"start": start, "end": end})
        if text in {"สรุปวันนี้", "summary"}:
            day = occurred_at.astimezone(BANGKOK).date()
            return ToolRequest(name="spending_summary", arguments={"start_date": day, "end_date": day})
        if text.startswith("ลบนัด "):
            return ToolRequest(name="delete_event", arguments={"event_ref": text[6:].strip()})
        if text.startswith("แก้นัด "):
            parts = [part.strip() for part in text[7:].split("|")]
            if len(parts) not in {2, 4}:
                raise ValueError("แก้นัด ล่าสุด | ชื่อใหม่ [| YYYY-MM-DD HH:MM | YYYY-MM-DD HH:MM]")
            arguments = {"event_ref": parts[0], "title": parts[1]}
            if len(parts) == 4:
                arguments.update(start=local_datetime(parts[2]), end=local_datetime(parts[3]))
            return ToolRequest(name="update_event", arguments=arguments)
        if text.startswith("นัด "):
            parts = [part.strip() for part in text[4:].split("|")]
            if len(parts) == 1:
                return CalendarClarification(parts[0])
            return self.calendar_request(parts)
        entries = []
        for line in text.splitlines():
            match = re.fullmatch(r"(?:(รายรับ|รายจ่าย)\s+)?(.+?)\s+([0-9]+(?:\.[0-9]+)?)\s*(?:บาท)?", line.strip())
            if not match:
                return None
            direction, item, amount = match.groups()
            entries.append({"item": item, "amount": amount,
                            "kind": "income" if direction == "รายรับ" else "expense",
                            "date": occurred_at.astimezone(BANGKOK).date()})
        if entries:
            return ToolRequest(name="add_transactions", arguments={"entries": entries})
        return None

    @staticmethod
    def calendar_request(parts: list[str]) -> ToolRequest:
        if len(parts) not in {2, 3, 4}:
            raise ValueError("นัด ชื่อ | YYYY-MM-DD HH:MM [| YYYY-MM-DD HH:MM | นาทีเตือน]")
        start = local_datetime(parts[1])
        end = local_datetime(parts[2]) if len(parts) >= 3 else start + timedelta(hours=1)
        minutes = int(parts[3]) if len(parts) == 4 else 30
        return ToolRequest(name="create_event", arguments={"title": parts[0], "start": start,
                                                           "end": end, "reminder_minutes": minutes})
