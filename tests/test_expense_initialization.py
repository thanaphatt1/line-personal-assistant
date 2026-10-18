"""Setup never overwrites conflicts or runs from startup; no real resources."""
from copy import deepcopy
from urllib.parse import unquote

import pytest

from line_assistant.expense_initialization import SheetsInitializer, SheetsInitializationTarget, FirestoreInitializer
from line_assistant.firestore_expenses import FirestoreExpenseTarget
from line_assistant.transport import NotReady, OutcomeUnknown, WriteBlocked
from test_expense_storage import BusinessPort, CATALOG
from firestore_expense_fake import BusinessFirestore


class SetupSheets:
    service = 'sheets'
    spreadsheet_id = 'synthetic-empty-workbook-0001'

    def __init__(self):
        self.tabs = {'Sheet1': {'properties': {'sheetId': 0, 'title': 'Sheet1', 'gridProperties': {'rowCount': 1001, 'columnCount': 26}},
                                'rows': [], 'hidden': False}}
        self.posts = 0
        self.lost_ack = False

    def request(self, method, path, *, body=None, params=None):
        if method == 'GET' and '/values/' not in path:
            sheets = []
            for title, sheet in self.tabs.items():
                value = {k: deepcopy(v) for k, v in sheet.items() if k not in {'rows', 'hidden'}}
                if 'columnMetadata' in params['fields']:
                    value['data'] = [{'startColumn': 4, 'columnMetadata': [{'hiddenByUser': sheet['hidden']}]}]
                sheets.append(value)
            return {'spreadsheetId': self.spreadsheet_id, 'sheets': sheets}
        if method == 'GET':
            title = unquote(path.split('/values/')[1]).split('!')[0][1:-1].replace("''", "'")
            return {'values': deepcopy(self.tabs[title]['rows'])}
        assert method == 'POST' and path.endswith(':batchUpdate')
        proposed = deepcopy(self.tabs)
        for request in body['requests']:
            if 'addSheet' in request:
                p = request['addSheet']['properties']; assert p['title'] not in proposed
                proposed[p['title']] = {'properties': deepcopy(p), 'rows': [], 'hidden': False}
                continue
            name, value = next(iter(request.items()))
            identifier = value.get('start', value.get('range', {}))['sheetId']
            sheet = next(s for s in proposed.values() if s['properties']['sheetId'] == identifier)
            if name == 'updateCells':
                start = value['start']
                for index, row in enumerate(value['rows'], start['rowIndex']):
                    while len(sheet['rows']) <= index:sheet['rows'].append([])
                    for column, cell in enumerate(row['values'], start['columnIndex']):
                        while len(sheet['rows'][index]) <= column:sheet['rows'][index].append('')
                        sheet['rows'][index][column] = cell['userEnteredValue']['stringValue']
            elif name == 'updateDimensionProperties':sheet['hidden'] = value['properties']['hiddenByUser']
            elif name != 'repeatCell':raise AssertionError(name)
        self.tabs = proposed; self.posts += 1
        if self.lost_ack:raise OutcomeUnknown('Synthetic setup acknowledgement lost')
        return {'spreadsheetId': self.spreadsheet_id}


def setup():
    api = SetupSheets()
    return api, SheetsInitializer(api, SheetsInitializationTarget(api.spreadsheet_id))


def test_preview_is_read_only_apply_adds_exact_schema_seed_and_repeated_apply_is_noop():
    api, initializer = setup(); before = deepcopy(api.tabs)
    plan = initializer.preview(CATALOG)
    assert api.tabs == before and api.posts == 0 and not plan['schema_ready']
    assert plan['sheet_ids']['All Expenses'] != plan['sheet_ids']['Database']
    result = initializer.apply(plan, exclusive_setup=True)
    assert result['schema_ready'] and result['catalog_ready'] and api.posts == 1
    assert api.tabs['Sheet1'] == before['Sheet1']
    assert api.tabs['All Expenses']['rows'][0] == ['Date', 'List', 'Cost', 'Remark', 'transaction_id']
    assert api.tabs['All Expenses']['hidden']
    assert initializer.apply(plan, exclusive_setup=True)['writes'] == 0 and api.posts == 1


def test_schema_without_business_seed_reports_catalog_setup_still_required():
    api, initializer = setup(); initializer.apply(initializer.preview(), exclusive_setup=True)
    assert initializer.check()['schema_ready'] and not initializer.check()['catalog_ready']


@pytest.mark.parametrize('damage', ['header', 'data_with_missing_header', 'formula', 'grid', 'protection', 'merge', 'extra_header'])
def test_conflicts_are_preserved_with_zero_mutations(damage):
    api, initializer = setup(); initializer.apply(initializer.preview(), exclusive_setup=True)
    sheet = api.tabs['All Expenses']
    if damage == 'header':sheet['rows'][0][0] = 'Wrong'
    elif damage == 'data_with_missing_header':sheet['rows'][0][0] = ''; sheet['rows'].append([123, 'private-placeholder', 1])
    elif damage == 'formula':sheet['rows'][0][0] = '=IF(1,"","")'
    elif damage == 'grid':sheet['properties']['gridProperties']['rowCount'] = 1002
    elif damage == 'protection':sheet['protectedRanges'] = [{'warningOnly': False}]
    elif damage == 'merge':sheet['merges'] = [{'startRowIndex': 0}]
    else:sheet['rows'][0].append('Extra occupied header')
    before = deepcopy(api.tabs); count = api.posts
    with pytest.raises(NotReady):initializer.preview()
    assert api.tabs == before and api.posts == count


def test_partial_correct_headers_are_filled_only_when_target_has_no_data():
    api, initializer = setup(); initializer.apply(initializer.preview(), exclusive_setup=True)
    api.tabs['Database']['rows'][0] = ['Item', '', 'Sub-categories']
    plan = initializer.preview(); assert len(plan['requests']) == 1
    initializer.apply(plan, exclusive_setup=True)
    assert api.tabs['Database']['rows'][0] == ['Item', 'Categories', 'Sub-categories']


def test_stale_preview_tampered_request_and_missing_exclusive_window_never_write():
    api, initializer = setup(); plan = initializer.preview()
    with pytest.raises(WriteBlocked):initializer.apply(plan)
    changed = deepcopy(plan); changed['requests'][0]['addSheet']['properties']['title'] = 'wrong'
    with pytest.raises(WriteBlocked):initializer.apply(changed, exclusive_setup=True)
    api.tabs['Unrelated'] = deepcopy(api.tabs['Sheet1']); api.tabs['Unrelated']['properties'].update(sheetId=1, title='Unrelated')
    with pytest.raises(WriteBlocked):initializer.apply(plan, exclusive_setup=True)
    assert api.posts == 0


def test_lost_ack_observes_result_without_second_mutation():
    api, initializer = setup(); api.lost_ack = True
    assert initializer.apply(initializer.preview(CATALOG), exclusive_setup=True)['proof'] == 'observed_ready_after_unknown'
    assert api.posts == 1


def test_foreign_target_seed_duplicates_and_oversized_setup_are_rejected():
    api, initializer = setup(); plan = initializer.preview(); plan['target'] = 'foreign-workbook-000000'
    with pytest.raises(WriteBlocked):initializer.apply(plan, exclusive_setup=True)
    with pytest.raises(ValueError):initializer.preview([CATALOG[0], CATALOG[0]])
    with pytest.raises(ValueError):SheetsInitializationTarget('../x')
    assert api.posts == 0


def test_firestore_bootstrap_is_create_only_separate_from_state_and_idempotent():
    fake = BusinessFirestore(); target = FirestoreExpenseTarget('synthetic-project', 'synthetic-owner')
    initializer = FirestoreInitializer(BusinessPort(fake), target)
    plan = initializer.preview(CATALOG)
    assert not fake.documents and not fake.commits
    assert initializer.apply(plan, exclusive_setup=True)['schema_ready']
    assert initializer.apply(plan, exclusive_setup=True)['writes'] == 0 and fake.commits == 1
    assert len(fake.documents) == 3 and all('/assistant_expense_owners/' in k for k in fake.documents)
    with pytest.raises(NotReady):initializer.preview([{'item': 'Different', 'category': 'Other', 'subcategory': ''}])


def test_firestore_orphans_and_stale_plan_fail_without_bootstrap_writes():
    fake = BusinessFirestore(); target = FirestoreExpenseTarget('synthetic-project', 'synthetic-owner')
    initializer = FirestoreInitializer(BusinessPort(fake), target); plan = initializer.preview()
    fake.put(target.parent + '/expenses/orphan', {'schema_version': 1})
    with pytest.raises(NotReady):initializer.apply(plan, exclusive_setup=True)
    assert fake.commits == 0


def test_firestore_existing_metadata_and_blank_categories_do_not_imply_catalog_readiness():
    fake=BusinessFirestore();target=FirestoreExpenseTarget('synthetic-project','synthetic-owner')
    seed=[{'item':'Unassigned','category':'','subcategory':''}]
    initializer=FirestoreInitializer(BusinessPort(fake),target)
    result=initializer.apply(initializer.preview(seed),exclusive_setup=True)
    assert result['schema_ready'] and not result['catalog_ready']
    from line_assistant.firestore_expenses import unfields
    value=unfields(fake.documents[target.parent]);value['unexpected']='manual change';fake.put(target.parent,value)
    with pytest.raises(NotReady):initializer.check()


def test_initializer_never_extends_a_smaller_existing_grid_for_seed():
    api,initializer=setup();initializer.apply(initializer.preview(),exclusive_setup=True)
    api.tabs['Database']['properties']['gridProperties']['rowCount']=2
    count=api.posts
    with pytest.raises(NotReady):initializer.preview(CATALOG)
    assert api.posts==count
