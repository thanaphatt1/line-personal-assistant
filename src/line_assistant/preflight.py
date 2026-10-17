"""Offline analysis of minimal read-only Sheet evidence; never writes or connects."""

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, StrictBool

from .contracts import Contract, Text

EXPENSE_HEADERS = {"A": "Date", "B": "List", "C": "Cost", "D": "Remark"}
MONTHLY_HEADERS = {"A": "Date", "B": "List", "C": "Category", "D": "Sub-category", "E": "Cost", "F": "Remark"}
MASTER_HEADERS = {"A": "Item", "B": "Categories", "C": "Sub-categories"}
INPUT_COLUMNS = ("A", "B", "C", "D")
MONTHLY_INPUT_COLUMNS = ("A", "B", "E", "F")


class TabEvidence(Contract):
    name: Text | None = None
    sheet_id: int | None = Field(default=None, strict=True, ge=0)
    header_row: int | None = Field(default=None, strict=True, ge=1)
    headers: dict[str, str] = Field(default_factory=dict)


class FormulaSample(Contract):
    column: Literal["C", "D"]
    row: int = Field(strict=True, ge=1)
    formula: str = Field(max_length=10000)


class CandidateEvidence(Contract):
    row: int | None = Field(default=None, strict=True, ge=1)
    # Booleans reveal occupied inputs without retaining transaction text/amounts.
    input_occupied: dict[str, StrictBool | None] = Field(default_factory=dict)
    formulas: dict[str, str] = Field(default_factory=dict)
    protection_allows_input_write: StrictBool | None = None


class DropdownEvidence(Contract):
    kind: Literal["range", "list", "unknown"] = "unknown"
    source_range: str | None = None
    invalid_input: Literal["reject", "warn", "unknown"] = "unknown"
    expands_with_master: StrictBool | None = None


class SheetEvidence(Contract):
    spreadsheet_id: Text
    source: Literal["user_supplied", "synthetic", "connector_read_only", "browser_read_only"] = "user_supplied"
    expense: TabEvidence = Field(default_factory=TabEvidence)
    master: TabEvidence = Field(default_factory=TabEvidence)
    expense_layout: Literal["four_column_input", "formula_derived_input"] = "four_column_input"
    locale: str | None = None
    timezone: str | None = None
    date_number_format: str | None = None
    formula_mode: Literal["unknown", "not_applicable", "per_row", "array_spill"] = "unknown"
    formula_samples: list[FormulaSample] = Field(default_factory=list, max_length=12)
    spill_covers_candidate: StrictBool | None = None
    candidate: CandidateEvidence = Field(default_factory=CandidateEvidence)
    dropdown: DropdownEvidence = Field(default_factory=DropdownEvidence)
    transaction_id_column: str | None = None
    id_scheme_verified: StrictBool = False
    uncertain_write_recovery: Literal["unselected", "manual_review", "hidden_id"] = "unselected"


@dataclass(frozen=True)
class PreflightReport:
    mechanical_checks_passed: bool
    live_writes_enabled: bool
    blocking_checks: tuple[str, ...]
    warnings: tuple[str, ...]
    proposed_input_ranges: tuple[str, ...]


def analyze(evidence: SheetEvidence) -> PreflightReport:
    """Evidence quality gates only. Passing these cannot grant write permission."""
    blocked, warnings = [], []
    four_column = evidence.expense_layout == "four_column_input"
    input_columns = INPUT_COLUMNS if four_column else MONTHLY_INPUT_COLUMNS
    expected_expense_headers = EXPENSE_HEADERS if four_column else MONTHLY_HEADERS
    if evidence.source not in {"connector_read_only", "browser_read_only"}:
        blocked.append("live_evidence_unverified")
    for role, tab, expected in (("expense", evidence.expense, expected_expense_headers),
                                ("master", evidence.master, MASTER_HEADERS)):
        if tab.name is None or tab.sheet_id is None or tab.header_row is None:
            blocked.append(f"{role}_identity_incomplete")
        if any(tab.headers.get(column) != header for column, header in expected.items()):
            blocked.append(f"{role}_headers_unverified_or_changed")
    if not evidence.locale or not evidence.timezone or not evidence.date_number_format:
        blocked.append("date_encoding_unverified")
    if evidence.timezone and evidence.timezone != "Asia/Bangkok":
        warnings.append("Workbook timezone differs from Bangkok: encode the chosen date as an integer serial; do not change workbook settings")
    candidate = evidence.candidate
    if candidate.row is None or (evidence.expense.header_row is not None and candidate.row <= evidence.expense.header_row):
        blocked.append("candidate_row_unverified")
    if any(candidate.input_occupied.get(col) is None for col in input_columns):
        blocked.append("candidate_inputs_unverified")
    elif any(candidate.input_occupied[col] for col in input_columns):
        blocked.append("candidate_input_occupied")
    if candidate.protection_allows_input_write is not True:
        blocked.append("candidate_protection_unverified_or_restricted")
    if four_column:
        if evidence.formula_mode != "not_applicable":
            blocked.append("four_column_input_formula_status_unverified")
        if any(candidate.formulas.get(col) for col in input_columns):
            blocked.append("candidate_input_contains_formula")
    elif evidence.formula_mode == "unknown" or evidence.formula_mode == "not_applicable":
        blocked.append("formula_mode_unknown")
    elif evidence.formula_mode == "per_row":
        if any(not candidate.formulas.get(col, "").startswith("=") for col in ("C", "D")):
            blocked.append("candidate_formula_coverage_missing")
        sampled = {sample.column for sample in evidence.formula_samples if sample.formula.startswith("=")
                   and sample.row != candidate.row}
        if sampled != {"C", "D"}:
            blocked.append("populated_formula_samples_incomplete")
    elif evidence.spill_covers_candidate is not True:
        blocked.append("spill_coverage_unverified")
    dropdown = evidence.dropdown
    if dropdown.kind == "unknown" or dropdown.invalid_input == "unknown":
        blocked.append("dropdown_validation_unverified")
    if dropdown.kind == "range" and not dropdown.source_range:
        blocked.append("dropdown_range_unverified")
    if dropdown.expands_with_master is not True:
        warnings.append("Master additions may fall outside the verified dropdown source")
    if dropdown.invalid_input == "reject":
        warnings.append("Unknown names would be rejected by List validation; do not probe by writing")
    if dropdown.invalid_input == "warn":
        warnings.append("List permits invalid inputs with a warning; membership still needs server validation")
    formulas = [sample.formula for sample in evidence.formula_samples]
    formulas += list(candidate.formulas.values())
    if any('"transportation"' in formula.casefold() and ("xlookup(" in formula.casefold() or "iferror(" in formula.casefold())
           for formula in formulas):
        warnings.append("Transportation is a formula fallback, not proof of an unknown item's classification")
    if not evidence.id_scheme_verified:
        blocked.append("transaction_id_scheme_unverified")
    elif evidence.transaction_id_column is None:
        warnings.append("No Sheet transaction ID: uncertain writes require review, never blind retry")
        if evidence.uncertain_write_recovery != "manual_review":
            blocked.append("uncertain_write_recovery_unselected")
    elif evidence.transaction_id_column in (set(input_columns) | (set() if four_column else {"C", "D"})):
        blocked.append("transaction_id_overlaps_existing_columns")
    elif evidence.uncertain_write_recovery != "hidden_id":
        blocked.append("uncertain_write_recovery_inconsistent")
    ranges = ()
    if not blocked:
        quoted_tab = "'" + evidence.expense.name.replace("'", "''") + "'"
        row = candidate.row
        ranges = (f"{quoted_tab}!A{row}:D{row}",) if four_column else (f"{quoted_tab}!A{row}:B{row}", f"{quoted_tab}!E{row}:F{row}")
    return PreflightReport(not blocked, False, tuple(blocked), tuple(warnings), ranges)


def cell_has_input(cell: dict) -> bool:
    """Inspect CellData userEnteredValue only; formatting/dropdowns are not data."""
    entered = cell.get("userEnteredValue")
    if entered is None or entered == {}:
        return False
    if not isinstance(entered, dict) or len(entered) != 1:
        raise ValueError("Unexpected CellData input value")
    key, value = next(iter(entered.items()))
    if key in {"stringValue", "formulaValue"} and isinstance(value, str):
        return value != ""  # A formula evaluating to empty still occupies an input.
    if key == "numberValue" and type(value) in {float, int}:
        return True  # Zero still occupies a cell.
    if key == "boolValue" and type(value) is bool:
        return True
    raise ValueError("Unexpected CellData input value")


def row_input_occupied(cells: dict[str, dict], columns: tuple[str, ...] = INPUT_COLUMNS) -> dict[str, bool]:
    """For one fully inspected input row; absent CellData denotes an empty cell."""
    return {column: cell_has_input(cells.get(column, {})) for column in columns}


def master_row_occupied(cells: dict[str, dict]) -> bool:
    """A blank Item does not permit overwriting an existing category/subcategory."""
    return any(row_input_occupied(cells, ("A", "B", "C")).values())
