"""Four-column Sheets REST adapter. No blind append retries or master writes."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from .adapters import Conflict, Reconciliation
from .catalog import ItemCatalog
from .contracts import AddTransactions
from .google_requests import expense_preview, identified_append_preview
from .sheet_metadata import TransactionIdMetadata, column_letters, transaction_id
from .sheet_dates import serial_to_date
from .transport import NotReady, OutcomeUnknown, RestClient, WriteBlocked


@dataclass(frozen=True)
class SheetTarget:
    spreadsheet_id: str = "placeholder-spreadsheet"
    expense_tab: str = "All Expenses"
    database_tab: str = "Database"
    expense_sheet_id: int = 101
    database_sheet_id: int = 102
    max_rows: int = 1000
    writes_enabled: bool = False
    # This is a setup precondition, not model-granted authorization.
    input_access_verified: bool = False
    recovery: str = "manual_review"
    transaction_id_metadata: TransactionIdMetadata | None = None
    row_allocation: str = "server_append_cells"

    def __post_init__(self):
        if self.max_rows < 2 or not self.spreadsheet_id:
            raise ValueError("Explicit bounded target required")
        if self.recovery not in {"manual_review", "approved_id_column"}:
            raise ValueError("Select a known reconciliation policy")
        if self.recovery == "approved_id_column":
            if not self.transaction_id_metadata or not self.transaction_id_metadata.schema_approved:
                raise ValueError("A separately approved non-input ID column is required")
        elif self.transaction_id_metadata is not None:
            raise ValueError("Manual review does not write an ID column")
        if self.row_allocation not in {"server_append_cells", "legacy_mock_update"}:
            raise ValueError("Unsupported row allocation")
        if self.recovery == "approved_id_column" and self.row_allocation != "server_append_cells":
            raise ValueError("Identified writes require server append allocation")


def tab_range(tab, cells):
    return f"'{tab.replace(chr(39), chr(39) * 2)}'!{cells}"


def normalized_row(row, size=4):
    return list(row) + [""] * max(0, size - len(row))


def same_inputs(actual, expected):
    actual = normalized_row(actual)
    if len(actual) != 4:
        return False
    for index in (0, 2):
        if type(actual[index]) not in {int, float}:
            return False
        try:
            if Decimal(str(actual[index])) != Decimal(str(expected[index])):
                return False
        except InvalidOperation:
            return False
    return actual[1] == expected[1] and actual[3] == expected[3]


class SheetsApi:
    def __init__(self, client: RestClient, target: SheetTarget, effect_guard=None):
        if client.service != "sheets":
            raise ValueError("Sheets transport required")
        self.client, self.target = client, target
        self.effect_guard = effect_guard or (lambda: None)
        self.base = f"/v4/spreadsheets/{quote(target.spreadsheet_id, safe='')}"

    @property
    def scope_id(self):
        return self.target.spreadsheet_id

    def values(self, tab, cells, *, formulas=False):
        response = self.client.request("GET", self.base + "/values/" + quote(tab_range(tab, cells), safe=""), params={
            "valueRenderOption": "FORMULA" if formulas else "UNFORMATTED_VALUE",
            "dateTimeRenderOption": "SERIAL_NUMBER",
        })
        rows = response.get("values", [])
        if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
            raise OutcomeUnknown("Invalid value range")
        return rows

    def metadata(self):
        fields = "spreadsheetId,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),protectedRanges)"
        params = {"fields": fields}
        metadata = self.target.transaction_id_metadata
        if metadata:
            params["fields"] = fields[:-1] + ",data(startColumn,columnMetadata(hiddenByUser)))"
            params["ranges"] = [tab_range(self.target.expense_tab, f"{metadata.column}1:{metadata.column}1"),
                                tab_range(self.target.database_tab, "A1:A1")]
        response = self.client.request("GET", self.base, params=params)
        if response.get("spreadsheetId") != self.target.spreadsheet_id:
            raise NotReady("Spreadsheet identity changed")
        tabs = {item["properties"]["title"]: item for item in response.get("sheets", [])}
        for name, identifier in ((self.target.expense_tab, self.target.expense_sheet_id),
                                 (self.target.database_tab, self.target.database_sheet_id)):
            if name not in tabs or tabs[name]["properties"]["sheetId"] != identifier:
                raise NotReady("Configured tab identity changed")
        if metadata and metadata.require_hidden:
            hidden = next((column.get("hiddenByUser", False)
                           for data in tabs[self.target.expense_tab].get("data", [])
                           for offset, column in enumerate(data.get("columnMetadata", []))
                           if data.get("startColumn", 0) + offset == metadata.zero_based_index), None)
            if hidden is not True:
                raise NotReady("Configured ID metadata column is not verified hidden")
        if metadata:
            grid = tabs[self.target.expense_tab]["properties"]["gridProperties"]
            if grid["rowCount"] > self.target.max_rows or grid["columnCount"] < metadata.index:
                raise NotReady("Full ID uniqueness scan is outside configured grid bounds")
        return tabs

    def catalog(self):
        return self.taxonomy().catalog

    def taxonomy(self):
        from .taxonomy import Taxonomy
        grid = self.metadata()[self.target.database_tab]["properties"]["gridProperties"]
        if grid["rowCount"] > self.target.max_rows or grid["columnCount"] < 3:
            raise NotReady("Complete Database lookup is outside configured grid bounds")
        rows = self.values(self.target.database_tab, f"A1:C{grid['rowCount']}")
        if not rows or rows[0] != ["Item", "Categories", "Sub-categories"]:
            raise NotReady("Database headers changed")
        return Taxonomy([normalized_row(row, 3)[:3] for row in rows[1:]
                         if row and isinstance(row[0], str) and row[0].strip()])

    transaction_id = staticmethod(transaction_id)

    def _rows(self):
        rows = self.values(self.target.expense_tab, f"A1:D{self.target.max_rows}")
        if not rows or rows[0] != ["Date", "List", "Cost", "Remark"]:
            raise NotReady("Four-column input headers changed")
        return rows

    def _prepare_write(self, args, key, *, approved_new=()):
        entries = args.entries
        if not self.target.writes_enabled or not self.target.input_access_verified:
            raise WriteBlocked("Sheet writes/protection preconditions are not enabled")
        if any(entry.kind != "expense" for entry in entries):
            raise NotReady("Income mapping is unresolved")
        tabs = self.metadata()
        expense = tabs[self.target.expense_tab]
        # Coarse fail-closed protection gate: do not guess exceptions/unprotected ranges.
        if any(not protection.get("warningOnly", False) for protection in expense.get("protectedRanges", [])):
            raise NotReady("Enforced range protections need an explicit access policy")
        catalog = self.catalog()
        if any(catalog.match(entry.item).canonical != entry.item and entry.item not in approved_new for entry in entries):
            raise Conflict("Canonical Database item changed; resolve it again")
        rows = self._rows()
        grid = expense["properties"]["gridProperties"]
        metadata = self.target.transaction_id_metadata
        if metadata:
            if grid["columnCount"] < metadata.index or grid["rowCount"] > self.target.max_rows:
                raise NotReady("Configured grid is outside bounded ID scan capacity")
            # Server append considers every column, so preflight capacity does too.
            all_rows = self.values(self.target.expense_tab, f"A1:{column_letters(grid['columnCount'])}{grid['rowCount']}", formulas=True)
            last = max((index + 1 for index, row in enumerate(all_rows) if any(value not in ("", None) for value in row)), default=1)
            if last + len(entries) > min(grid["rowCount"], self.target.max_rows):
                raise NotReady("No bounded append capacity; do not extend the grid intentionally")
            identified_rows = self._identified_rows()
            planned = {transaction_id(key, index) for index in range(len(entries))}
            if any(len(row) >= metadata.index and row[metadata.zero_based_index] in planned for row in identified_rows[1:]):
                raise Conflict("Action ID already appears; reconcile instead of appending")
            return None, identified_append_preview(args, self.target.expense_sheet_id, metadata, key)
        if self.target.row_allocation != "legacy_mock_update" or not self.client.mocked:
            raise NotReady("Unidentified client-selected allocation is available only in the legacy mock")
        # Append below all occupied A:D rows, never fill a gap or reuse a blank Item.
        occupied = [index + 1 for index, row in enumerate(rows) if any(value not in ("", None) for value in row)]
        first = max(occupied, default=1) + 1
        if first + len(entries) - 1 > min(self.target.max_rows, grid["rowCount"]):
            raise NotReady("No bounded input capacity; do not extend the live grid automatically")
        preview = expense_preview(args, first, self.target.expense_tab)
        # Refresh the candidate immediately before the write. This is not atomic
        # with Google; human/other-writer races remain a documented setup constraint.
        candidate = self.values(self.target.expense_tab, f"A{first}:D{first+len(entries)-1}", formulas=True)
        if any(value not in ("", None) for row in candidate for value in row):
            raise Conflict("Candidate inputs changed before write")
        return first, preview

    def add(self, entries, key):
        args = AddTransactions.model_validate({"entries": [entry.model_dump(mode="json") for entry in entries]})
        try:
            first, preview = self._prepare_write(args, key)
        except Conflict:
            raise
        except Exception:
            # Only read RPCs occurred; distinguish setup/read failure from an
            # ambiguous write. Do not freeze the user behind a nonexistent effect.
            raise WriteBlocked("Read-only write preconditions did not pass") from None
        self.effect_guard()
        if self.target.transaction_id_metadata:
            response = self.client.request("POST", self.base + ":batchUpdate", body=preview.body)
            if response.get("spreadsheetId") != self.target.spreadsheet_id:
                raise OutcomeUnknown("Append acknowledgement target changed")
            proof = self.reconcile(key, args.model_dump(mode="json"))
            if proof.outcome != "committed":
                raise OutcomeUnknown("Appended ID and payload readback remains unresolved")
            return proof.receipt
        response = self.client.request("POST", self.base + "/values:batchUpdate", body=preview.body)
        expected = preview.body["data"][0]["values"]
        replies = response.get("responses", [])
        actual = replies[0].get("updatedData", {}).get("values", []) if replies else []
        if (response.get("spreadsheetId") != self.target.spreadsheet_id or len(actual) != len(expected)
                or not all(same_inputs(row, wanted) for row, wanted in zip(actual, expected))):
            raise OutcomeUnknown("Written value acknowledgement does not match the action")
        return tuple(tab_range(self.target.expense_tab, f"A{row}:D{row}") for row in range(first, first + len(entries)))

    def enrollment_body(self, payload, key):
        """Read-only preconditions. Never creates a category or overwrites a row."""
        from .enrollment import Enrollment, batch_body
        from .catalog import normalized
        plan = Enrollment.model_validate(payload).checked()
        if not self.target.transaction_id_metadata:
            raise WriteBlocked('Enrollment requires identified expense recovery')
        tabs = self.metadata()
        database = tabs[self.target.database_tab]
        grid = database['properties']['gridProperties']
        if (grid['rowCount'] > self.target.max_rows or grid['columnCount'] != 26
                or any(not p.get('warningOnly', False) for p in database.get('protectedRanges', []))):
            raise WriteBlocked('Database capacity/protection must be reviewed')
        taxonomy = self.taxonomy()
        desired = []
        for row in plan.catalog:
            if (row.category, row.subcategory) not in {(r.category, r.subcategory) for r in taxonomy.rows}:
                raise Conflict('Category/subcategory pair no longer exists')
            found = [r for r in taxonomy.rows if normalized(r.item) == normalized(row.item)]
            if found:
                if len(found) != 1 or (found[0].item, found[0].category, found[0].subcategory) != (row.item, row.category, row.subcategory):
                    raise Conflict('Existing item has a different mapping')
            else: desired.append(row)
        all_rows = self.values(self.target.database_tab, f"A1:Z{grid['rowCount']}", formulas=True)
        last = max((i + 1 for i, row in enumerate(all_rows) if any(v not in ('', None) for v in row)), default=1)
        if last + len(desired) > grid['rowCount']: raise WriteBlocked('No Database append capacity')
        self._prepare_write(AddTransactions(entries=plan.entries), key, approved_new=[r.item for r in desired])
        return batch_body(payload, self.target, key, desired)

    def enroll_and_add(self, payload, key):
        try: body = self.enrollment_body(payload, key)
        except Conflict: raise
        except Exception: raise WriteBlocked('Enrollment read-only preconditions did not pass') from None
        self.effect_guard()
        response = self.client.request('POST', self.base + ':batchUpdate', body=body)
        if response.get('spreadsheetId') != self.target.spreadsheet_id:
            raise OutcomeUnknown('Atomic batch acknowledgement target changed')
        proof = self.reconcile_enrollment(key, payload)
        if proof.outcome != 'committed': raise OutcomeUnknown('Enrollment and expense readback unresolved')
        return proof.receipt

    def reconcile_enrollment(self, key, payload):
        from .enrollment import Enrollment
        from .catalog import normalized
        try:
            plan = Enrollment.model_validate(payload).checked()
            proof = self.reconcile(key, {'entries': payload['entries']})
            if proof.outcome != 'committed': return Reconciliation('unknown')
            taxonomy = self.taxonomy()
            for row in plan.catalog:
                found = [r for r in taxonomy.rows if normalized(r.item) == normalized(row.item)]
                if len(found) != 1 or (found[0].item, found[0].category, found[0].subcategory) != (row.item, row.category, row.subcategory):
                    return Reconciliation('unknown')
            return proof
        except Exception: return Reconciliation('unknown')

    def summary(self, start, end):
        total = sum((Decimal(row['amount']) for row in self.saved_entries(start, end)), Decimal('0'))
        return {"income": None, "income_supported": False, "expense": f"{total:.2f}"}

    def classification_entries(self):
        """Read every configured expense input, including legacy rows, once.

        Display pagination never limits the source read. Existing IDs participate
        in the snapshot fingerprint; this path cannot assign or change IDs.
        """
        import re
        grid = self.metadata()[self.target.expense_tab]['properties']['gridProperties']
        metadata = self.target.transaction_id_metadata
        width = metadata.index if metadata else 4
        if (type(grid.get('rowCount')) is not int or not 2 <= grid['rowCount'] <= self.target.max_rows
                or type(grid.get('columnCount')) is not int or grid['columnCount'] < width):
            raise NotReady('Complete classification read exceeds configured grid')
        column = metadata.column if metadata else 'D'
        rows = self.values(self.target.expense_tab, f'A1:{column}{grid["rowCount"]}', formulas=True)
        if (not rows or rows[0][:4] != ['Date', 'List', 'Cost', 'Remark'] or len(rows) > grid['rowCount']
                or metadata and (len(rows[0]) < width or rows[0][width-1] != metadata.header)):
            raise NotReady('Classification input/ID header drift')
        entries = []; seen = set()
        for raw in rows[1:]:
            if not any(v not in ('', None) for v in raw):continue
            row = normalized_row(raw, width)
            if metadata and all(v in ('',None) for v in row[:4]) and isinstance(row[width-1],str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',row[width-1]):
                identifier=row[width-1]
                if identifier in seen:raise Conflict('Duplicate existing transaction ID')
                seen.add(identifier);continue
            if (type(row[0]) not in {int, float} or type(row[2]) not in {int, float}
                    or not isinstance(row[1], str) or not 1 <= len(row[1]) <= 200
                    or not isinstance(row[3], str) or len(row[3]) > 500
                    or any(isinstance(v, str) and v.startswith('=') for v in row[:4])):
                raise NotReady('Classification inputs/formulas need manual review')
            try:
                if int(row[0]) != row[0]:raise ValueError()
                day = serial_to_date(int(row[0])); amount = Decimal(str(row[2]))
                if not amount.is_finite() or amount <= 0 or amount.quantize(Decimal('.01')) != amount:raise ValueError()
            except (ValueError, OverflowError, InvalidOperation):
                raise NotReady('Classification date/amount cannot be decoded') from None
            identifier = row[width-1] if metadata else ''
            if identifier and (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier)):
                raise NotReady('Invalid existing transaction ID')
            if identifier and identifier in seen:raise Conflict('Duplicate existing transaction ID')
            if identifier:seen.add(identifier)
            entries.append({'date':day.isoformat(), 'item':row[1], 'amount':f'{amount:.2f}',
                            'note':row[3], 'transaction_id':identifier or None})
        return entries

    def saved_entries(self, start, end):
        """Complete bounded read of the existing expense inputs; no append path."""
        from .read_cache import read
        # Validate the complete source once; each window still filters all rows.
        records=read(('expense_snapshot',id(self)),self._saved_snapshot)
        return [r for r in records if start.isoformat()<=r['date']<=end.isoformat()]

    def _saved_snapshot(self):
        grid = self.metadata()[self.target.expense_tab]["properties"]["gridProperties"]
        if (type(grid.get("rowCount")) is not int or not 1 <= grid["rowCount"] <= self.target.max_rows
                or type(grid.get("columnCount")) is not int or grid["columnCount"] < 4):
            raise NotReady("Complete expense read is outside configured grid bounds")
        rows = self._rows()
        if len(rows) > grid["rowCount"]:
            raise NotReady("Expense response exceeds reviewed grid")
        result = []
        for raw in rows[1:]:
            if not any(v not in ("", None) for v in raw):continue
            row = normalized_row(raw)
            if (len(row) != 4 or type(row[0]) not in {int, float} or type(row[2]) not in {int, float}
                    or not isinstance(row[1], str) or not 1 <= len(row[1]) <= 200
                    or not isinstance(row[3], str) or len(row[3]) > 500):
                raise NotReady("Expense inputs cannot be decoded completely")
            try:
                if int(row[0]) != row[0]:raise ValueError()
                day = serial_to_date(int(row[0]));amount = Decimal(str(row[2]))
                if not amount.is_finite() or amount <= 0 or amount.quantize(Decimal('.01')) != amount:raise ValueError()
            except (ValueError, OverflowError, InvalidOperation):
                raise NotReady("Expense date or amount cannot be decoded") from None
            result.append({'date': day.isoformat(), 'item': row[1], 'amount': f'{amount:.2f}', 'note': row[3]})
        return result

    def _identified_rows(self):
        metadata = self.target.transaction_id_metadata
        rows = self.values(self.target.expense_tab, f"A1:{metadata.column}{self.target.max_rows}")
        if (not rows or rows[0][:4] != ["Date", "List", "Cost", "Remark"]
                or len(rows[0]) < metadata.index or rows[0][metadata.zero_based_index] != metadata.header):
            raise NotReady("Exact input/transaction_id header or configured column drifted")
        return rows

    def reconcile(self, key, payload):
        if self.target.recovery == "manual_review":
            return Reconciliation("unknown")  # Matching A:D alone is not action proof.
        args = AddTransactions.model_validate(payload)
        metadata = self.target.transaction_id_metadata
        try:
            self.metadata()
            rows = self._identified_rows()  # One response pairs each ID with its inputs.
        except Exception:
            return Reconciliation("unknown")
        expected = expense_preview(args, 2, self.target.expense_tab).body["data"][0]["values"]
        refs = []
        for index, wanted in enumerate(expected):
            found = [row_index for row_index, row in enumerate(rows[1:], start=2)
                     if len(row) >= metadata.index and row[metadata.zero_based_index] == transaction_id(key, index)]
            if len(found) != 1 or not same_inputs(rows[found[0] - 1][:4], wanted):
                return Reconciliation("unknown")
            refs.append(tab_range(self.target.expense_tab, f"A{found[0]}:D{found[0]}"))
        # Never return not_committed: an earlier HTTP write may still be in flight.
        return Reconciliation("committed", tuple(refs))
