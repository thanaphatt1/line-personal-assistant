from copy import deepcopy

import pytest

from line_assistant.preflight import (
    EXPENSE_HEADERS, MASTER_HEADERS, MONTHLY_HEADERS, MONTHLY_INPUT_COLUMNS,
    SheetEvidence, analyze, cell_has_input, master_row_occupied, row_input_occupied,
)


@pytest.fixture
def metadata():
    # Synthetic evidence tests the analyzer; no actual private transaction values.
    return {"spreadsheet_id": "synthetic-sheet", "source": "connector_read_only",
            "expense": {"name": "All Expenses", "sheet_id": 123, "header_row": 1,
                        "headers": EXPENSE_HEADERS},
            "master": {"name": "Database", "sheet_id": 456, "header_row": 1,
                       "headers": MASTER_HEADERS},
            "expense_layout": "four_column_input",
            "locale": "en_US", "timezone": "America/Los_Angeles", "date_number_format": "yyyy-mm-dd",
            "formula_mode": "not_applicable", "formula_samples": [],
            "candidate": {"row": 10, "input_occupied": {"A": False, "B": False, "C": False, "D": False},
                          "formulas": {}, "protection_allows_input_write": True},
            "dropdown": {"kind": "range", "source_range": "=Database!$A$2:$A", "invalid_input": "warn",
                         "expands_with_master": True},
            "id_scheme_verified": True, "transaction_id_column": None,
            "uncertain_write_recovery": "manual_review"}


def monthly_metadata(metadata):
    metadata["expense_layout"] = "formula_derived_input"
    metadata["expense"]["name"] = "Synthetic monthly table"
    metadata["expense"]["headers"] = MONTHLY_HEADERS
    metadata["formula_mode"] = "per_row"
    metadata["formula_samples"] = [
        {"column": "C", "row": 9, "formula": '=IF(B9="","",XLOOKUP(B9,Database!A:A,Database!B:B,"Transportation"))'},
        {"column": "D", "row": 9, "formula": '=IF(B9="","",XLOOKUP(B9,Database!A:A,Database!C:C,""))'}]
    metadata["candidate"]["input_occupied"] = {"A": False, "B": False, "E": False, "F": False}
    metadata["candidate"]["formulas"] = {
        "C": '=IF(B10="","",XLOOKUP(B10,Database!A:A,Database!B:B,"Transportation"))',
        "D": '=IF(B10="","",XLOOKUP(B10,Database!A:A,Database!C:C,""))'}
    return metadata


def test_passing_metadata_never_enables_live_writes(metadata):
    result = analyze(SheetEvidence.model_validate(metadata))
    assert result.mechanical_checks_passed
    assert not result.live_writes_enabled
    assert result.proposed_input_ranges == ("'All Expenses'!A10:D10",)
    assert not any("Transportation" in warning for warning in result.warnings)
    assert any("uncertain" in warning for warning in result.warnings)
    assert any("warning" in warning for warning in result.warnings)
    assert any("Workbook timezone" in warning for warning in result.warnings)


def test_supplied_tab_names_are_not_verification():
    result = analyze(SheetEvidence(spreadsheet_id="target", expense={"name": "All Expenses"}, master={"name": "Database"}))
    assert not result.mechanical_checks_passed
    assert "live_evidence_unverified" in result.blocking_checks
    assert "expense_identity_incomplete" in result.blocking_checks
    assert "four_column_input_formula_status_unverified" in result.blocking_checks
    assert not result.proposed_input_ranges


@pytest.mark.parametrize("column", ["A", "B", "C", "D"])
def test_any_occupied_input_blocks_candidate(metadata, column):
    metadata["candidate"]["input_occupied"][column] = True
    result = analyze(SheetEvidence.model_validate(metadata))
    assert "candidate_input_occupied" in result.blocking_checks
    assert not result.proposed_input_ranges


def test_formatted_dropdown_does_not_make_normalized_input_row_occupied():
    row = {"B": {"dataValidation": {"condition": {"type": "ONE_OF_RANGE"}}},
           "C": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY"}}}}
    assert row_input_occupied(row) == {"A": False, "B": False, "C": False, "D": False}
    row["C"]["userEnteredValue"] = {"numberValue": 0}
    assert row_input_occupied(row)["C"]


def test_formula_columns_only_are_ignored_for_legacy_monthly_input_detection():
    row = {"B": {"dataValidation": {"condition": {"type": "ONE_OF_RANGE"}}},
           "C": {"userEnteredValue": {"formulaValue": '=IF(B10="","",1)'}},
           "D": {"userEnteredValue": {"formulaValue": '=IF(B10="","",2)'}}}
    assert row_input_occupied(row, MONTHLY_INPUT_COLUMNS) == {"A": False, "B": False, "E": False, "F": False}
    assert row_input_occupied(row)["C"]  # A formula in selected All Expenses input is occupied.


@pytest.mark.parametrize("entered", [{"formulaValue": '=""'}, {"numberValue": 0}, {"boolValue": False}, {"stringValue": " "}])
def test_existing_input_is_not_overwritten_even_when_display_looks_blank(entered):
    assert cell_has_input({"userEnteredValue": entered})


def test_missing_candidate_formula_blocks_legacy_per_row_append(metadata):
    monthly_metadata(metadata)
    metadata["candidate"]["formulas"].pop("D")
    assert "candidate_formula_coverage_missing" in analyze(SheetEvidence.model_validate(metadata)).blocking_checks


def test_array_spill_requires_verified_coverage(metadata):
    monthly_metadata(metadata)
    metadata["formula_mode"] = "array_spill"
    metadata["candidate"]["formulas"] = {}
    assert "spill_coverage_unverified" in analyze(SheetEvidence.model_validate(metadata)).blocking_checks
    metadata["spill_covers_candidate"] = True
    assert analyze(SheetEvidence.model_validate(metadata)).mechanical_checks_passed


def test_legacy_fallback_warning_is_not_applied_to_current_normalized_layout(metadata):
    result = analyze(SheetEvidence.model_validate(monthly_metadata(metadata)))
    assert any("Transportation" in warning for warning in result.warnings)
    assert result.proposed_input_ranges == ("'Synthetic monthly table'!A10:B10", "'Synthetic monthly table'!E10:F10")


@pytest.mark.parametrize("field,value,expected", [
    ("formula_mode", "unknown", "four_column_input_formula_status_unverified"),
    ("id_scheme_verified", False, "transaction_id_scheme_unverified"),
    ("date_number_format", None, "date_encoding_unverified"),
    ("transaction_id_column", "C", "transaction_id_overlaps_existing_columns"),
    ("source", "synthetic", "live_evidence_unverified"),
    ("uncertain_write_recovery", "unselected", "uncertain_write_recovery_unselected"),
])
def test_incomplete_or_unsafe_evidence_blocks(metadata, field, value, expected):
    metadata[field] = value
    result = analyze(SheetEvidence.model_validate(metadata))
    assert expected in result.blocking_checks
    assert not result.live_writes_enabled


def test_date_and_protection_uncertainty_are_not_treated_as_permission(metadata):
    metadata["candidate"]["protection_allows_input_write"] = None
    assert "candidate_protection_unverified_or_restricted" in analyze(SheetEvidence.model_validate(metadata)).blocking_checks


def test_schema_analyzer_does_not_mutate_supplied_evidence(metadata):
    before = deepcopy(metadata)
    analyze(SheetEvidence.model_validate(metadata))
    assert metadata == before


def test_master_blank_item_with_existing_category_or_subcategory_is_occupied():
    assert master_row_occupied({"B": {"userEnteredValue": {"stringValue": "synthetic category"}},
                                "C": {"userEnteredValue": {"stringValue": "synthetic subcategory"}}})
    assert not master_row_occupied({"B": {"dataValidation": {"strict": True}}})


def test_unknown_cell_types_fail_closed():
    with pytest.raises(ValueError):
        cell_has_input({"userEnteredValue": {"unsupported": "x"}})
