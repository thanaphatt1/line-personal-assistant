"""Test-only durable protocol simulator; each HTTP RPC commits independently."""

from pathlib import Path

from line_assistant.mock_api import MockApiServices
from line_assistant.persistence import SQLiteStore


class PersistentApiMock(MockApiServices):
    def __init__(self, directory):
        super().__init__(id_column="E")
        self.store = SQLiteStore(Path(directory) / "simulator.sqlite3")
        with self.store.transaction() as connection:
            saved = self.store.read(connection, "api")
            if saved:
                self.restore(saved)
            else:
                self.store.write(connection, "api", self.snapshot())

    def snapshot(self):
        return {"cells": [[tab, row, col, value] for (tab, row, col), value in self.cells.items()],
                "documents": self.documents, "version": self.version, "sheet_writes": self.sheet_writes,
                "row_count": self.row_count, "column_count": self.column_count,
                "database_row_count": self.database_row_count,
                "hidden_columns": list(self.hidden_columns), "line_replies": self.line_replies}

    def restore(self, saved):
        self.cells = {(tab, row, col): value for tab, row, col, value in saved["cells"]}
        self.documents, self.version, self.sheet_writes = saved["documents"], saved["version"], saved["sheet_writes"]
        self.row_count, self.column_count = saved["row_count"], saved["column_count"]
        self.database_row_count = saved["database_row_count"]
        self.hidden_columns, self.line_replies = set(saved["hidden_columns"]), saved["line_replies"]

    def handle(self, request):
        error = None
        with self.store.transaction() as connection:
            self.restore(self.store.read(connection, "api"))
            try:
                response = super().handle(request)
            except Exception as caught:
                error = caught
            self.store.write(connection, "api", self.snapshot())
        if error:
            raise error  # A lost acknowledgement does not roll back the fake effect.
        return response
