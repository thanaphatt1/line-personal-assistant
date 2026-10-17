"""Read schema-only local configuration; never enable transport or write access."""

import json
from pathlib import Path

from .sheet_metadata import TransactionIdMetadata
from .sheets_api import SheetTarget


def load_disabled_sheet_target(path: Path) -> SheetTarget:
    value = json.loads(Path(path).read_text())
    if value.get("live_transport_enabled") is not False or value.get("writes_enabled") is not False:
        raise ValueError("Confirmed schema configuration must remain disabled")
    if value.get("input_columns") != {"date": "A", "item": "B", "amount": "C", "note": "D"}:
        raise ValueError("A:D input contract changed")
    if value.get("row_allocation") != "server_append_cells":
        raise ValueError("Confirmed target requires server append allocation")
    metadata = TransactionIdMetadata(**value["transaction_id_metadata"])
    if metadata != TransactionIdMetadata("E", "transaction_id", True, True):
        raise ValueError("Parent-confirmed hidden E transaction_id metadata changed")
    if value.get("metadata_zero_based_index") != metadata.zero_based_index:
        raise ValueError("Confirmed metadata column/index disagree")
    return SheetTarget(spreadsheet_id=value["spreadsheet_id"], expense_tab=value["expense_tab"],
                       database_tab=value["database_tab"], expense_sheet_id=value["expense_sheet_id"],
                       database_sheet_id=value["database_sheet_id"], max_rows=value["max_rows"],
                       recovery="approved_id_column", transaction_id_metadata=metadata,
                       row_allocation="server_append_cells", writes_enabled=False, input_access_verified=False)
