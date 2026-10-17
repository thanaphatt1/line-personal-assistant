"""Default-off history integration through existing REST adapters, mocks only.

Reuse the Firestore lease/CAS/acknowledgement protocol and the validated v05
history envelope. No production factory, credentials or live transport opt-in.
"""
import re

from .config import Settings
from .firestore_state import FirestoreState
from .history_context import HistoryState
from .history_persistence import history_snapshot, restore_history
from .sheets_api import SheetTarget, SheetsApi
from .transport import RestClient


class FirestoreHistoryState(FirestoreState, HistoryState):
    """History fields ride the existing owner snapshot and expiring lease."""

    def __init__(self, client, owner_id, *, collection_id="history_test_v06", clock=None,
                 lease_seconds=120, max_snapshot_bytes=800_000, compact_snapshots=False):
        if (type(client) is not RestClient or not client.mocked
                or not re.fullmatch(r"(?:mock|synthetic)-[A-Za-z0-9_-]+", owner_id)
                or not re.fullmatch(r"history_test_[A-Za-z0-9_-]+", collection_id)):
            raise ValueError("Explicit synthetic mocked history namespace required")
        super().__init__(client, owner_id, project_id="synthetic-project",
                         collection_id=collection_id, clock=clock, lease_seconds=lease_seconds,
                         max_snapshot_bytes=max_snapshot_bytes, compact_snapshots=compact_snapshots)

    def _snapshot_state(self):
        return history_snapshot(self)

    def _restore_state(self, snapshot):
        restore_history(self, snapshot, self.owner_id)


def history_adapter_assistant(*, firestore_client, sheets_client, target: SheetTarget,
                              port, settings=None, clock=None, enabled=False,
                              collection_id="history_test_v06", lease_seconds=120):
    """Inject synthetic HTTP transports into actual Firestore/Sheets adapters.

    A test must explicitly supply its approved mock ID column and layout.
    Matching inputs alone cannot produce a success or permit an append retry.
    """
    if enabled is not True:
        raise ValueError("History adapter integration defaults off")
    if (type(sheets_client) is not RestClient or not sheets_client.mocked
            or type(firestore_client) is not RestClient or not firestore_client.mocked
            or getattr(port, "mocked", False) is not True
            or type(target) is not SheetTarget or target.spreadsheet_id != "synthetic-sheet"
            or target.recovery != "approved_id_column" or target.row_allocation != "server_append_cells"):
        raise ValueError("Synthetic mocked identified adapters required")
    settings = settings or Settings()
    state = FirestoreHistoryState(firestore_client, settings.owner_id,
                                 collection_id=collection_id, clock=clock, lease_seconds=lease_seconds)
    sheets = SheetsApi(sheets_client, target, effect_guard=state.before_effect)
    from .history_first import HistoryFirstAssistant
    return HistoryFirstAssistant(settings, sheets, port=port, state=state, clock=clock,
                                 history_first_enabled=True)
