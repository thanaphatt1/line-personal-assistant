"""Explicit check/preview/apply setup. Never imported by application startup."""
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import quote
import re

from .history_context import digest
from .taxonomy import Taxonomy
from .sheets_api import tab_range
from .transport import ApiError, NotReady, OutcomeUnknown, WriteBlocked
from .firestore_expenses import SCHEMA, fields, unfields, FirestoreExpenseStore


def checked_seed(seed):
    if not isinstance(seed, list) or len(seed) > 1000:
        raise ValueError('Bounded explicit catalog seed required')
    for row in seed:
        if not isinstance(row, dict) or set(row) != {'item', 'category', 'subcategory'}:
            raise ValueError('Seed rows require item/category/subcategory only')
    Taxonomy([[r[k] for k in ['item', 'category', 'subcategory']] for r in seed])
    return deepcopy(seed)


@dataclass(frozen=True)
class SheetsInitializationTarget:
    spreadsheet_id: str
    max_rows: int = 1001

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', self.spreadsheet_id) or type(self.max_rows) is not int or not 2 <= self.max_rows <= 1001:
            raise ValueError('Explicit existing spreadsheet and bounded grid required')


class SheetsInitializer:
    backend = 'sheets'

    def __init__(self, client, target):
        if client.service != 'sheets' or type(target) is not SheetsInitializationTarget:
            raise ValueError('Explicit Sheets initializer target required')
        self.client, self.target = client, target
        self.base = '/v4/spreadsheets/' + target.spreadsheet_id

    def preview(self, seed=None):
        seed = checked_seed(seed or [])
        metadata = self.client.request('GET', self.base, params={
            'fields': 'spreadsheetId,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),protectedRanges,merges)'})
        if metadata.get('spreadsheetId') != self.target.spreadsheet_id:
            raise NotReady('Initializer spreadsheet identity changed')
        tabs = {}
        identifiers = set()
        for sheet in metadata.get('sheets', []):
            p = sheet.get('properties', {})
            if p.get('title') in tabs or type(p.get('sheetId')) is not int or not 0 <= p['sheetId'] <= 2147483647 or p['sheetId'] in identifiers:
                raise NotReady('Ambiguous spreadsheet tab identities')
            tabs[p['title']] = sheet
            identifiers.add(p['sheetId'])
        requests, snapshot, ids = [], {'metadata': metadata, 'values': {}}, {}
        seed_ready = False
        definitions = [('All Expenses', ['Date', 'List', 'Cost', 'Remark', 'transaction_id'], 24),
                       ('Database', ['Item', 'Categories', 'Sub-categories'], 26)]
        for title, headers, columns in definitions:
            existing = tabs.get(title)
            if existing:
                props = existing['properties']; grid = props.get('gridProperties', {})
                if (type(grid.get('rowCount')) is not int or not 2 <= grid['rowCount'] <= self.target.max_rows
                        or grid.get('columnCount') != columns or existing.get('merges')
                        or any(not p.get('warningOnly', False) for p in existing.get('protectedRanges', []))):
                    raise NotReady('Existing target grid/protections/merges conflict; no automatic repair')
                identifier = props['sheetId']
                rows = self.client.request('GET', self.base + '/values/' + quote(tab_range(title, f'A1:{"X" if columns == 24 else "Z"}{grid["rowCount"]}'), safe=''),
                    params={'valueRenderOption': 'FORMULA', 'dateTimeRenderOption': 'SERIAL_NUMBER'}).get('values', [])
                if (not isinstance(rows, list) or len(rows) > grid['rowCount'] or any(not isinstance(r, list) or len(r) > columns for r in rows)):
                    raise NotReady('Complete initializer grid read is unproven')
                snapshot['values'][title] = rows
                header = rows[0] if rows else []
                if any(v not in ('', None) for v in header[len(headers):]):
                    raise NotReady('Unexpected occupied target headers')
                for i, wanted in enumerate(headers):
                    if i < len(header) and header[i] not in ('', None, wanted):
                        raise NotReady('Conflicting target header; existing values are preserved')
                populated = any(v not in ('', None) for row in rows[1:] for v in row)
                missing = [i for i in range(len(headers)) if i >= len(header) or header[i] in ('', None)]
                if populated and missing:
                    raise NotReady('Populated target has missing headers; manual review required')
            else:
                identifier = next(i for i in range(501, 10000) if i not in identifiers)
                identifiers.add(identifier); rows = []; missing = list(range(len(headers))); populated = False
                requests.append({'addSheet': {'properties': {'sheetId': identifier, 'title': title,
                    'gridProperties': {'rowCount': self.target.max_rows, 'columnCount': columns}}}})
            ids[title] = identifier
            for i in missing:
                requests.append({'updateCells': {'start': {'sheetId': identifier, 'rowIndex': 0, 'columnIndex': i},
                    'rows': [{'values': [{'userEnteredValue': {'stringValue': headers[i]}}]}], 'fields': 'userEnteredValue'}})
            if title == 'All Expenses':
                # Read hidden metadata explicitly: no credentials, protection or input changes.
                hidden = False
                if existing:
                    details = self.client.request('GET', self.base, params={
                        'fields': 'spreadsheetId,sheets(properties(sheetId),data(startColumn,columnMetadata(hiddenByUser)))',
                        'ranges': [tab_range(title, 'E1:E1')]})
                    if details.get('spreadsheetId') != self.target.spreadsheet_id:
                        raise NotReady('Initializer hidden-column target changed')
                    snapshot['hidden'] = details
                    hidden = any(s.get('properties', {}).get('sheetId') == identifier and any(
                        d.get('startColumn', 0) + i == 4 and c.get('hiddenByUser') is True
                        for d in s.get('data', []) for i, c in enumerate(d.get('columnMetadata', []))) for s in details.get('sheets', []))
                if not hidden:
                    if populated:
                        raise NotReady('Populated ID column is not hidden; explicit review required')
                    requests.append({'updateDimensionProperties': {'range': {'sheetId': identifier, 'dimension': 'COLUMNS', 'startIndex': 4, 'endIndex': 5},
                        'properties': {'hiddenByUser': True}, 'fields': 'hiddenByUser'}})
                if not existing:
                    for column, pattern, kind in [(0, 'yyyy-mm-dd', 'DATE'), (2, '0.00', 'NUMBER')]:
                        requests.append({'repeatCell': {'range': {'sheetId': identifier, 'startRowIndex': 1, 'endRowIndex': self.target.max_rows,
                            'startColumnIndex': column, 'endColumnIndex': column + 1},
                            'cell': {'userEnteredFormat': {'numberFormat': {'type': kind, 'pattern': pattern}}}, 'fields': 'userEnteredFormat.numberFormat'}})
            else:
                occupied = [r for r in rows[1:] if any(v not in ('', None) for v in r)]
                if seed:
                    wanted = [[r[k] for k in ['item', 'category', 'subcategory']] for r in seed]
                    if occupied and occupied != wanted:
                        raise NotReady('Existing catalog differs from seed; initializer does not merge or overwrite')
                    if not occupied:
                        capacity = existing['properties']['gridProperties']['rowCount'] if existing else self.target.max_rows
                        if len(seed) + 1 > capacity:
                            raise NotReady('Catalog seed exceeds grid capacity')
                        requests.append({'updateCells': {'start': {'sheetId': identifier, 'rowIndex': 1, 'columnIndex': 0},
                            'rows': [{'values': [{'userEnteredValue': {'stringValue': v}} for v in row]} for row in wanted], 'fields': 'userEnteredValue'}})
                    seed_ready = any(r['category'].strip() for r in seed)
                elif occupied:
                    tax = Taxonomy([(r + [''] * 3)[:3] for r in occupied])
                    seed_ready = bool(tax.categories)
        return {'format': 'expense-initialization-v1', 'backend': self.backend, 'target': self.target.spreadsheet_id,
                'max_rows': self.target.max_rows, 'seed': seed, 'snapshot_hash': digest(snapshot), 'requests': requests,
                'sheet_ids': ids, 'schema_ready': not requests, 'catalog_ready': seed_ready}

    def check(self):
        plan = self.preview()
        return {k: plan[k] for k in ['backend', 'target', 'schema_ready', 'catalog_ready', 'sheet_ids']}

    def apply(self, plan, *, exclusive_setup=False):
        if exclusive_setup is not True:
            raise WriteBlocked('Explicit exclusive setup window required; Sheets has no external CAS')
        if plan.get('format') != 'expense-initialization-v1' or plan.get('backend') != self.backend or plan.get('target') != self.target.spreadsheet_id or plan.get('max_rows') != self.target.max_rows:
            raise WriteBlocked('Initializer plan target changed')
        fresh = self.preview(plan.get('seed'))
        if not fresh['requests']:
            return {**self.check(), 'proof': 'already_ready', 'writes': 0}
        if fresh != plan:
            raise WriteBlocked('Initializer preview is stale or altered; preview again')
        try:
            response = self.client.request('POST', self.base + ':batchUpdate', body={'requests': fresh['requests']})
            if response.get('spreadsheetId') != self.target.spreadsheet_id:
                raise OutcomeUnknown('Initializer acknowledgement target changed')
        except OutcomeUnknown:
            after = self.preview(plan['seed'])
            if after['requests']:
                raise OutcomeUnknown('Initializer outcome unresolved; check again without retrying mutation') from None
            return {**self.check(), 'proof': 'observed_ready_after_unknown', 'writes': 1}
        after = self.preview(plan['seed'])
        if after['requests']:
            raise OutcomeUnknown('Initializer schema readback did not match')
        return {**self.check(), 'proof': 'ack_and_readback', 'writes': 1}


class FirestoreInitializer:
    """Create-only business dataset/catalog bootstrap; no workflow documents."""
    backend = 'firestore'

    def __init__(self, client, target):
        self.client, self.target = client, target
        if client.service != 'firestore':
            raise ValueError('Firestore setup transport required')

    def preview(self, seed=None):
        seed = checked_seed(seed or [])
        if len(seed) > self.target.max_records:
            raise NotReady('Catalog seed exceeds configured capacity')
        try:
            meta = self.client.request('GET', '/v1/' + self.target.parent)
        except ApiError as error:
            if error.status != 404:
                raise
            meta = None
        if meta is not None:
            value = unfields(meta)
            if (meta.get('name') != self.target.parent or set(value) != {'schema_version','owner_hash','dataset_id','revision','catalog_revision','expense_count','catalog_count'}
                    or value.get('owner_hash') != self.target.owner_hash or value.get('schema_version') != SCHEMA
                    or value.get('dataset_id') != self.target.dataset_id
                    or any(type(value[k]) is not int or value[k] < 0 for k in ['revision','catalog_revision','expense_count','catalog_count'])
                    or max(value['expense_count'],value['catalog_count']) > self.target.max_records):
                raise NotReady('Existing dataset schema/owner conflicts')
            rows=self.client.request('POST','/v1/'+self.target.parent+':runQuery',body={'structuredQuery':{
                'from':[{'collectionId':'catalog'}], 'limit':self.target.max_records+1}})
            if not isinstance(rows,list):raise NotReady('Existing catalog read is incomplete')
            catalog=[]
            for row in rows:
                if 'document' not in row:continue
                doc=row['document'];v=unfields(doc)
                if (set(v)!={'schema_version','owner_hash','item','category','subcategory'} or v['schema_version']!=SCHEMA
                        or v['owner_hash']!=self.target.owner_hash
                        or doc.get('name')!=self.target.parent+'/catalog/'+FirestoreExpenseStore.catalog_id(v['item'])):
                    raise NotReady('Existing catalog schema/identity conflicts')
                catalog.append([v[k] for k in ['item','category','subcategory']])
            if len(catalog)!=value['catalog_count']:raise NotReady('Existing catalog count is inconsistent')
            taxonomy=Taxonomy(catalog)
            if seed:
                if value.get('catalog_count') != len(seed):
                    raise NotReady('Existing dataset catalog differs from seed')
                for row in seed:
                    name = self.target.parent + '/catalog/' + FirestoreExpenseStore.catalog_id(row['item'])
                    existing = self.client.request('GET', '/v1/' + name)
                    if existing.get('name') != name or unfields(existing) != {'schema_version': SCHEMA, 'owner_hash': self.target.owner_hash, **row}:
                        raise NotReady('Existing catalog differs from seed; setup does not merge')
            return {'format': 'expense-initialization-v1', 'backend': self.backend, 'target': self.target.scope_id,
                    'seed': seed, 'snapshot_hash': digest(meta), 'writes': [], 'schema_ready': True,
                    'catalog_ready': bool(taxonomy.categories)}
        for collection in ['expenses', 'catalog', 'operations']:
            result = self.client.request('POST', '/v1/' + self.target.parent + ':runQuery', body={
                'structuredQuery': {'from': [{'collectionId': collection}], 'limit': 1}})
            if not isinstance(result, list) or any('document' in v for v in result):
                raise NotReady('Uninitialized dataset contains orphan documents; manual review required')
        value = {'schema_version': SCHEMA, 'owner_hash': self.target.owner_hash, 'dataset_id': self.target.dataset_id,
                 'revision': 0, 'catalog_revision': 0, 'expense_count': 0, 'catalog_count': len(seed)}
        writes = [FirestoreExpenseStore._update(self.target.parent, value, {'exists': False})]
        for row in seed:
            writes.append(FirestoreExpenseStore._update(self.target.parent + '/catalog/' + FirestoreExpenseStore.catalog_id(row['item']),
                {'schema_version': SCHEMA, 'owner_hash': self.target.owner_hash, **row}, {'exists': False}))
        if len(writes) > 100 or len(str(writes).encode()) > 200_000:
            raise NotReady('Bootstrap seed exceeds fixed transaction bound (99 catalog rows)')
        return {'format': 'expense-initialization-v1', 'backend': self.backend, 'target': self.target.scope_id,
                'seed': seed, 'snapshot_hash': digest(None), 'writes': writes, 'schema_ready': False,
                'catalog_ready': any(r['category'].strip() for r in seed)}

    def check(self):
        plan = self.preview()
        return {k: plan[k] for k in ['backend', 'target', 'schema_ready', 'catalog_ready']}

    def apply(self, plan, *, exclusive_setup=False):
        if exclusive_setup is not True or plan.get('target') != self.target.scope_id or plan.get('backend') != self.backend:
            raise WriteBlocked('Explicit matching dataset bootstrap plan required')
        fresh = self.preview(plan.get('seed'))
        if fresh['schema_ready']:
            return {**self.check(), 'proof': 'already_ready', 'writes': 0}
        if self.preview(plan.get('seed')) != plan:
            raise WriteBlocked('Dataset bootstrap preview is stale or altered')
        try:
            response = self.client.request('POST', '/v1/' + self.target.database + '/documents:commit', body={'writes': plan['writes']})
            if len(response.get('writeResults', [])) != len(plan['writes']) or not response.get('commitTime'):
                raise OutcomeUnknown('Dataset bootstrap acknowledgement incomplete')
        except OutcomeUnknown:
            raise OutcomeUnknown('Dataset bootstrap outcome unknown; check only, do not repeat a mutation') from None
        result = self.check()
        if not result['schema_ready']:
            raise OutcomeUnknown('Dataset bootstrap readback incomplete')
        return {**result, 'proof': 'ack_and_readback', 'writes': 1}
