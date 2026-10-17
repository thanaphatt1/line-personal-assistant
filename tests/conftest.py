from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from line_assistant.adapters import MockCalendar, MockSheets, SheetLayout
from line_assistant.catalog import ItemCatalog
from line_assistant.config import Settings
from line_assistant.service import Assistant


class Harness:
    def __init__(self):
        fixture = json.loads((Path(__file__).parents[1] / "src/line_assistant/data/mock_sheet.json").read_text())
        self.now = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)
        self.sequence = 0
        self.service = Assistant(
            Settings(), MockSheets(SheetLayout(fixture["columns"], tuple(fixture["formula_columns"]),
                                             date_mode=fixture.get("date_mode", "iso_mock"))),
            MockCalendar(), ItemCatalog(fixture["database_items"], fixture["aliases"]),
            clock=lambda: self.now,
        )

    def event(self, text, event_id=None, stamp=None, owner="mock-owner", source_type="user"):
        self.sequence += 1
        event_id = event_id or f"event-{self.sequence}"
        return {"type": "message", "webhookEventId": event_id,
                "timestamp": stamp if stamp is not None else int(self.now.timestamp() * 1000) + self.sequence,
                "source": {"type": source_type, "userId": owner},
                "message": {"id": f"message-{event_id}", "type": "text", "text": text}}

    def send(self, text, **kwargs):
        return self.service.handle(self.event(text, **kwargs))


@pytest.fixture
def h():
    return Harness()


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """The public regression suite never calls paid or live services."""
    import socket
    def deny(*args, **kwargs):
        raise AssertionError("Offline tests cannot open external sockets")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
