"""Explicit existing-resource setup CLI. Reads a short-lived token from stdin.

No dotenv, ADC, keychain, OAuth, sharing, spreadsheet creation or startup hook.
Keep seed/plan files outside the public checkout when they contain private data.
"""
import argparse
import json
from pathlib import Path
import sys

import httpx

from line_assistant.expense_initialization import SheetsInitializer, SheetsInitializationTarget, FirestoreInitializer
from line_assistant.firestore_expenses import FirestoreExpenseTarget
from line_assistant.history_context import digest
from line_assistant.transport import ApiError, NotReady, OutcomeUnknown


class SetupClient:
    def __init__(self, service, token, *, transport=None):
        if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(not 33 <= ord(c) <= 126 for c in token):
            raise ValueError('Explicit short-lived access token required on stdin')
        self.service = service
        self.client = httpx.Client(base_url='https://' + ('sheets.googleapis.com' if service == 'sheets' else 'firestore.googleapis.com'),
            headers={'Authorization': 'Bearer ' + token}, transport=transport, timeout=12, follow_redirects=False, trust_env=False)

    def request(self, method, path, *, body=None, params=None):
        if not ((self.service == 'firestore' and path.startswith('/v1/projects/'))
                or (self.service == 'sheets' and path.startswith('/v4/spreadsheets/'))):
            raise ValueError('Fixed setup API path required')
        try:
            response = self.client.request(method, path, json=body, params=params)
        except httpx.TransportError:
            raise OutcomeUnknown('Setup acknowledgement unavailable') from None
        if not response.is_success:
            raise ApiError(response.status_code)
        try:
            return response.json()
        except ValueError:
            raise NotReady('Invalid setup acknowledgement') from None

    def close(self):
        self.client.headers.clear()
        self.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preview', 'check', 'apply'])
    parser.add_argument('--backend', choices=['sheets', 'firestore'], default='sheets')
    parser.add_argument('--spreadsheet-id')
    parser.add_argument('--max-rows', type=int, default=1001)
    parser.add_argument('--project-id')
    parser.add_argument('--owner-id')
    parser.add_argument('--dataset-id', default='default')
    parser.add_argument('--max-records', type=int, default=1000)
    parser.add_argument('--seed', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--plan-sha256')
    parser.add_argument('--exclusive-setup', action='store_true')
    args = parser.parse_args()
    if args.operation == 'apply' and (not args.plan or not args.plan_sha256 or not args.exclusive_setup):
        parser.error('apply requires a reviewed --plan, --plan-sha256 and --exclusive-setup')
    if args.operation != 'preview' and args.seed:
        parser.error('--seed is accepted only when generating a preview')
    target = (SheetsInitializationTarget(args.spreadsheet_id, args.max_rows) if args.backend == 'sheets'
              else FirestoreExpenseTarget(args.project_id, args.owner_id, args.dataset_id, max_records=args.max_records))
    token = sys.stdin.read(8194).strip()
    client = SetupClient(args.backend, token)
    token = None
    try:
        initializer = SheetsInitializer(client, target) if args.backend == 'sheets' else FirestoreInitializer(client, target)
        if args.operation == 'preview':
            result = initializer.preview(json.loads(args.seed.read_text()) if args.seed else [])
            result = {'plan': result, 'plan_sha256': digest(result)}
        elif args.operation == 'check':
            result = initializer.check()
        else:
            saved = json.loads(args.plan.read_text()); plan = saved.get('plan', saved)
            if digest(plan) != args.plan_sha256:
                raise ValueError('Reviewed plan hash changed')
            result = initializer.apply(plan, exclusive_setup=args.exclusive_setup)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never print provider prose, token, seed contents or arbitrary exceptions.
        print(json.dumps({'status': 'failed', 'category': type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
