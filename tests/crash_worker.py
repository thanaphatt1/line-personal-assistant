"""Subprocess-only fault injection. os._exit skips all cleanup/finally handlers."""

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from line_assistant.local_runtime import durable_mock_assistant


directory, event_file, now, phase, available = sys.argv[1:]


def hook(current_phase, key):
    if phase == current_phase:
        os._exit(73)


assistant = durable_mock_assistant(Path(directory), clock=lambda: datetime.fromisoformat(now),
                                    phase_hook=hook, reconciliation_available=available == "yes")
result = assistant.handle(json.loads(Path(event_file).read_text()))
print(json.dumps(asdict(result), ensure_ascii=False))
