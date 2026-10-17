"""Real process fault injection around identified append RPCs, no live transport."""

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from line_assistant.api_sandbox import api_sandbox
from line_assistant.firestore_state import LeaseBusy, LeaseLost
from persistent_api_mock import PersistentApiMock


directory, event_path, now, phase = sys.argv[1:]
assistant, _, _ = api_sandbox(services=PersistentApiMock(directory), clock=lambda: datetime.fromisoformat(now),
                              approved_mock_id_column=True)
request = assistant.sheets.client.request


def crashed_request(method, path, **kwargs):
    is_append = method == "POST" and path.endswith(":batchUpdate")
    if is_append and phase == "before_append":
        os._exit(73)
    result = request(method, path, **kwargs)
    if is_append and phase == "after_append":
        os._exit(73)  # Fake provider transaction has already committed.
    return result


def hook(current, key):
    if current == phase:
        os._exit(73)


assistant.sheets.client.request = crashed_request
assistant.phase_hook = hook
try:
    result = assistant.handle(json.loads(Path(event_path).read_text()))
except (LeaseBusy, LeaseLost):
    sys.exit(75)
print(json.dumps(asdict(result), ensure_ascii=False))
