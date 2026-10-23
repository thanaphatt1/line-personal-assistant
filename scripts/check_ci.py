"""Offline source/wheel/startup checks after installing requirements-ci.txt."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True

# Run in a fresh interpreter: the selected archive/wheel must supply every
# runtime module and the fixture, without inheriting the checkout's imports.
PROBE = """
from pathlib import Path
import json
import socket
import sys
import tempfile

sys.dont_write_bytecode = True
runtime, scripts = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(runtime), str(scripts)]
attempts = []
def deny(*args, **kwargs):
    attempts.append(1)
    raise AssertionError('CI startup cannot connect to external services')
socket.socket.connect = deny
socket.socket.connect_ex = deny

from fastapi.testclient import TestClient
from pydantic import SecretStr
from line_assistant.main import create_app
from line_assistant.local_runtime import durable_mock_assistant
from verify_history_signed_startup import run_checks

with TestClient(create_app()) as client:
    assert client.get('/health').json() == {'status': 'ok', 'mode': 'mock'}
    assert client.post('/webhooks/line', content=b'{"events":[]}',
                       headers={'x-line-signature': 'invalid'}).status_code == 401
with tempfile.TemporaryDirectory() as directory:
    assert durable_mock_assistant(Path(directory)).catalog.items

base = {
    'PILOT_MODE': 'production', 'PILOT_LAUNCH_AUTHORIZED': 'true',
    'PILOT_PROJECT_ID': 'ci-example-project', 'PILOT_REGION': 'asia-southeast3',
    'PILOT_SERVICE_ACCOUNT': 'assistant@ci-example-project.iam.gserviceaccount.com',
    'PILOT_SPREADSHEET_ID': 'synthetic-ci-workbook-0001',
    'PILOT_LEGACY_SPREADSHEET_ID': '',
    'PILOT_EXPENSE_SHEET_ID': '501', 'PILOT_DATABASE_SHEET_ID': '502',
    'PILOT_SHEETS_MAX_ROWS': '1001', 'PILOT_SHEET_SCHEMA_VERIFIED': 'true',
    'PILOT_LINE_OWNER_ID': 'U' + 'a' * 32,
    'PILOT_MODEL_MONTHLY_MICRO_USD': '500000',
    'PILOT_MODEL_MAX_CALLS_DAY': '100', 'PILOT_MODEL_MAX_CALLS_MONTH': '300',
    'PILOT_CONVERSATION_ENABLED': 'true', 'PILOT_STATE_CODEC_ENABLED': 'true',
    'PILOT_HISTORY_ENABLED': 'true', 'PILOT_HISTORY_STATE_ENABLED': 'true',
    'PILOT_CALENDAR_ID': 'synthetic-ci-calendar@group.calendar.google.com',
    'PILOT_MODEL_USAGE_GATES_ENABLED': 'false',
}
for backend in ('sheets','firestore'):
    for enabled,saved,read_all in ((False,False,False),(True,False,False),(True,True,False),(True,True,True)):
        flag = 'true' if enabled else 'false'
        result = run_checks({
            **base, 'PILOT_CALENDAR_ENABLED': flag, 'PILOT_EXPENSE_BACKEND': backend,
            'PILOT_CALENDAR_READ_ALL_ENABLED': 'true' if read_all else 'false',
            'PILOT_CALENDAR_READ_IDS': '["synthetic-reader@example.invalid"]' if enabled else '[]',
            'PILOT_CALENDAR_CONNECTIONS_ENABLED': 'true' if saved else 'false',
            'PILOT_FIRESTORE_EXPENSE_SCHEMA_VERIFIED': 'true',
            'PILOT_RESPONSE_REALIZATION_ENABLED': flag,
            'PILOT_SAVED_EXPENSE_EDITS_ENABLED': 'true' if saved else 'false',
            'PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED': 'true' if saved else 'false',
            'PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY': ('firestore_transaction' if backend=='firestore' else 'reviewed_single_writer') if saved else 'off',
            'PILOT_PROVISIONAL_CATEGORIES_ENABLED': 'true' if saved else 'false',
            'PILOT_PROVISIONAL_MAPPING_ENABLED': 'true' if saved else 'false',
            'PILOT_HISTORICAL_CATEGORIES_ENABLED': 'true' if saved else 'false',
            'PILOT_PREVIOUS_EXPENSE_PRICES_ENABLED': 'true' if saved else 'false',
            'PILOT_SAVED_EXPENSE_DELETE_ENABLED': 'true' if saved else 'false',
            'PILOT_CATALOG_ALIASES_ENABLED': 'true' if saved else 'false',
            'PILOT_RECORD_ACTIONS_ENABLED': 'true' if saved else 'false',
        }, secret_loader=lambda name: SecretStr('synthetic-' + name))
        assert result['status'] == 'passed' and result['provider_requests'] == 0

for table_backend in ('sheets','firestore'):
    binding = ({'backend':'sheets','spreadsheet_id':'synthetic-table-workbook-0001','tab':'People','sheet_id':701}
               if table_backend=='sheets' else {'backend':'firestore','collection_path':'people'})
    registry = [{'table':{'id':'people','label':'People','description':'Synthetic contacts',
                 'columns':[{'name':'name','label':'Name','type':'text','required':True}]},'storage':binding}]
    result=run_checks({**base,'PILOT_TABLE_REGISTRY_JSON':json.dumps(registry)},
                      secret_loader=lambda name:SecretStr('synthetic-'+name))
    assert result['status']=='passed' and result['provider_requests']==0

assert not attempts
import line_assistant
assert Path(line_assistant.__file__).resolve().is_relative_to(runtime.resolve())
print('mock, durable mock and signed bridge/candidate startup passed; zero external connections')
"""


def run(*args, cwd=ROOT):
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
    }
    subprocess.run([sys.executable, *map(str, args)], cwd=cwd, env=env,
                   check=True, timeout=120)


def main():
    with tempfile.TemporaryDirectory(prefix="assistant-ci-") as directory:
        work = Path(directory)
        bundle, wheels, installed = work / "bundle", work / "wheels", work / "installed"
        packager = ROOT / "scripts/prepare_calendar_source.py"
        run(packager, "--output", bundle)
        run(packager, "--verify", bundle)
        probe = work / "startup_probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        source = bundle / "source"
        run("-I", probe, source / "src", source / "scripts", cwd=work)

        run("-m", "pip", "wheel", "--no-index", "--no-cache-dir", "--no-deps",
            "--no-build-isolation", "--wheel-dir", wheels, ROOT)
        candidates = list(wheels.glob("*.whl"))
        if len(candidates) != 1:
            raise RuntimeError("Expected one wheel from the reviewed checkout")
        wheel = candidates[0]
        expected = {
            str(path.relative_to(ROOT / "src")): path
            for path in (ROOT / "src/line_assistant").rglob("*")
            if path.is_file() and (path.suffix == ".py" or path.name == "mock_sheet.json")
        }
        bundled = {
            str(path.relative_to(source / "src")): path
            for path in (source / "src/line_assistant").rglob("*") if path.is_file()
        }
        if bundled.keys() != expected.keys():
            raise RuntimeError("Source bundle runtime/resource closure differs from checkout")
        with zipfile.ZipFile(wheel) as archive:
            actual = {name for name in archive.namelist()
                      if name.startswith("line_assistant/") and not name.endswith("/")}
            if actual != expected.keys():
                raise RuntimeError("Wheel runtime/resource closure differs from source")
            for name, path in expected.items():
                if archive.read(name) != path.read_bytes() or bundled[name].read_bytes() != path.read_bytes():
                    raise RuntimeError("Packaged runtime/resource differs from source")
        run("-m", "pip", "install", "--no-index", "--no-cache-dir", "--no-deps",
            "--target", installed, wheel)
        run("-I", probe, installed, source / "scripts", cwd=work)
        run(packager, "--verify", bundle)
    print("Offline source, wheel and startup checks passed", flush=True)


if __name__ == "__main__":
    main()
