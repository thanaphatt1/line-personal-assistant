from importlib.resources import files
"""Opt-in durable, synthetic-only local runtime. No cloud/client SDK imports."""

import json
from pathlib import Path
from typing import Callable
from datetime import datetime

from .adapters import SheetLayout
from .catalog import ItemCatalog
from .config import Settings
from .durable_mocks import DurableMockCalendar, DurableMockSheets
from .persistence import SQLiteState
from .service import Assistant


def durable_mock_assistant(directory: Path, *, reconciliation_available: bool = True,
                           clock: Callable[[], datetime] | None = None,
                           phase_hook: Callable[[str, str], None] | None = None,
                           settings: Settings | None = None) -> Assistant:
    directory = Path(directory)
    settings = settings or Settings()
    state = SQLiteState(directory / "controller.sqlite3", settings.owner_id)
    fixture = json.loads(files("line_assistant").joinpath("data/mock_sheet.json").read_text(encoding="utf-8"))
    layout = SheetLayout(fixture["columns"], tuple(fixture["formula_columns"]),
                         fixture["schema_verified"], fixture["date_mode"])
    # This database deliberately differs from the controller's commit boundary.
    sheets = DurableMockSheets(layout, directory / "providers.sqlite3", reconciliation_available, phase_hook)
    calendar = DurableMockCalendar(directory / "providers.sqlite3", reconciliation_available, phase_hook)
    return Assistant(settings, sheets, calendar, ItemCatalog(fixture["database_items"], fixture["aliases"]),
                     state=state, clock=clock, phase_hook=phase_hook)
