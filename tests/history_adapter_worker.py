"""Subprocess harness: actual adapters, persistent protocol fake, no sockets."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json, os, re, socket, sys

from line_assistant.config import Settings
from line_assistant.firestore_state import LeaseBusy, LeaseLost
from line_assistant.history_integration import history_adapter_assistant
from line_assistant.model_api import MODEL_ID
from line_assistant.sheets_api import SheetTarget
from line_assistant.sheet_metadata import TransactionIdMetadata
from line_assistant.transport import RestClient
from persistent_api_mock import PersistentApiMock


class ApiScriptPort:
    mocked = True

    def __init__(self):
        self.requests = []
        self.phase_hook = lambda phase, key: None

    def request(self, method, path, *, body):
        assert (method, path) == ('POST', '/v1/responses')
        self.requests.append(deepcopy(body))
        c = json.loads(body['input'][0]['content']); text = c['message']
        self.phase_hook('model_started', c['current_source_ref'])
        after_prepare = len(body['input']) > 1 and body['input'][1].get('name') == 'prepare_expenses'
        if text.startswith('เรื่องอื่น') or after_prepare:
            output = [{'type': 'message', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': 'กรุณาระบุข้อมูลเพิ่ม' if after_prepare else 'ตอบอีกหัวข้อได้'}]}]
        else:
            if c['available_drafts'] and len(body['input']) == 1:
                name, args = 'read_expense_drafts', {'draft_ref': c['available_drafts'][0]['draft_ref']}
            else:
                if len(body['input']) > 1:
                    d = json.loads(body['input'][2]['output'])['drafts'][0]
                    e = d['entries'][0]; ref, version = d['draft_ref'], d['draft_version']
                else:
                    e = {'entry_ref': None, 'item': 'BTS', 'amount': '228', 'date': None, 'note': ''}
                    ref, version = None, None
                entry = {k: e[k] for k in ['entry_ref', 'item', 'amount', 'date', 'note']}
                amount = re.search(r'(?:ยอด.*?เป็น|BTS)\s*(\d+)\s*บาท', text)
                if amount: entry['amount'] = amount[1]
                entry.update(date_text=None, category=None, subcategory=None, source_refs=[c['current_source_ref']])
                name, args = 'prepare_expenses', {'draft_ref': ref, 'draft_version': version, 'entries': [entry]}
            output = [{'type': 'function_call', 'status': 'completed', 'call_id': 'local_api',
                       'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}]
        return {'model': MODEL_ID, 'status': 'completed', 'usage': {'input_tokens': 800, 'output_tokens': 100},
                'output': output}


def mock_target(**kwargs):
    return SheetTarget(spreadsheet_id='synthetic-sheet', writes_enabled=True, input_access_verified=True,
                       recovery='approved_id_column', transaction_id_metadata=TransactionIdMetadata('E', schema_approved=True),
                       **kwargs)


def runtime(providers, clock, *, port=None, owner='mock-owner', **kwargs):
    return history_adapter_assistant(firestore_client=RestClient('firestore', providers.transport()),
                                    sheets_client=RestClient('sheets', providers.transport()), target=mock_target(),
                                    port=port or ApiScriptPort(), settings=Settings(owner_id=owner),
                                    clock=clock, enabled=True, **kwargs)


def main():
    def deny(*a, **k): raise AssertionError('Adapter worker cannot use network')
    socket.socket.connect = deny; socket.socket.connect_ex = deny
    directory, raw, phase = sys.argv[1:4]
    v = json.loads(Path(raw).read_text()); now = datetime.fromisoformat(v['now'])
    p = PersistentApiMock(directory); assistant = runtime(p, lambda: now)
    request = assistant.sheets.client.request

    def fault(method, path, **kwargs):
        append = method == 'POST' and path.endswith(':batchUpdate')
        if append and phase == 'before_append': os._exit(73)
        value = request(method, path, **kwargs)
        if append and phase == 'after_append': os._exit(73)
        return value

    def hook(current, key):
        if current == phase: os._exit(73)

    assistant.sheets.client.request = fault; assistant.phase_hook = hook
    assistant.port.phase_hook = hook
    try:
        result = assistant.handle(v['event'])
    except (LeaseBusy, LeaseLost):
        sys.exit(75)
    print(json.dumps({**asdict(result), 'model_requests': len(assistant.port.requests)}, ensure_ascii=False))


if __name__ == '__main__': main()
