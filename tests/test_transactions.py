from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from line_assistant.adapters import MockSheets, SheetLayout
from line_assistant.catalog import ItemCatalog
from line_assistant.contracts import AddTransactions, ToolRequest
from line_assistant.sheet_dates import date_to_serial


def test_known_expenses_use_only_observed_input_columns(h):
    result = h.send("bts 65\nseven 100.25")
    assert result.status == "recorded"
    assert result.data["items"] == ["BTS", "seven"]
    assert result.data["raw_items"] == ["bts", "seven"]
    assert h.service.sheets.write_log[0] == {"A1": date_to_serial(date(2026, 10, 11)), "B1": "BTS", "C1": 65.0, "D1": ""}
    assert all(cell[0] in {"A", "B", "C", "D"} for writes in h.service.sheets.write_log for cell in writes)
    assert h.send("สรุปวันนี้").data["expense"] == "165.25"


def test_legacy_monthly_fixture_preserves_its_classification_formulas(h):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/mock_monthly_sheet.json").read_text())
    h.service.sheets = MockSheets(SheetLayout(fixture["columns"], tuple(fixture["formula_columns"])))
    assert h.send("BTS 65").status == "recorded"
    assert h.service.sheets.cell_values["C1"] == "<existing-sheet-formula>"
    assert h.service.sheets.cell_values["D1"] == "<existing-sheet-formula>"
    assert h.service.sheets.write_log[0] == {"A1": "2026-10-11", "B1": "BTS", "E1": 65.0, "F1": ""}


def test_synthetic_confirmed_alias_resolves_without_persisting_any_alias(h):
    aliases = dict(h.service.catalog.aliases)
    assert h.send("บีทีเอส 65").data["items"] == ["BTS"]
    assert h.service.catalog.aliases == aliases


def test_whole_batch_waits_for_explicit_typo_choice(h):
    result = h.send("BTS 65\nsevne 100")
    assert result.status == "clarification"
    assert result.data["suggestions"] == ["seven"]
    assert not h.service.sheets.entries
    token = result.data["token"]
    assert h.send(f"ยืนยัน {token}").status == "clarification"
    assert not h.service.sheets.entries
    result = h.send(f"เลือก {token} seven")
    assert result.status == "recorded"
    assert len(h.service.sheets.entries) == 2
    assert result.data["raw_items"] == ["BTS", "sevne"]
    assert "sevne" not in h.service.catalog.aliases


def test_multiple_unresolved_items_get_new_tokens(h):
    first = h.send("กาแฟฟ 40\nsevne 100")
    second = h.send(f"เลือก {first.data['token']} กาแฟ")
    assert second.status == "clarification"
    assert first.data["token"] != second.data["token"]
    assert not h.service.sheets.entries
    assert h.send(f"เลือก {first.data['token']} seven").status == "clarification"
    assert h.send(f"เลือก {second.data['token']} seven").status == "recorded"
    assert len(h.service.sheets.entries) == 2


def test_new_item_never_uses_transportation_fallback_or_writes_master(h):
    items = dict(h.service.catalog.items)
    result = h.send("ร้านใหม่หน้าซอย 120")
    assert result.data["match"] == "unknown"
    assert not h.service.sheets.entries
    assert h.send(f"ใหม่ {result.data['token']}").status == "new_item_unconfigured"
    assert not h.service.sheets.entries
    assert h.service.catalog.items == items
    assert h.send("ยกเลิก").status == "cancelled"
    assert h.service.state.pending is None


def test_cancel_and_expired_confirmation_cannot_write(h):
    first = h.send("sevne 100")
    h.send("ยกเลิก")
    assert h.send(f"เลือก {first.data['token']} seven").status == "no_pending"
    second = h.send("sevne 100")
    h.now += timedelta(seconds=h.service.settings.pending_ttl_seconds + 1)
    assert h.send(f"เลือก {second.data['token']} seven").status == "no_pending"
    assert not h.service.sheets.entries


def test_income_and_mixed_batches_are_gated_before_any_write(h):
    assert h.send("รายรับ เงินเดือน 30000").status == "income_unconfigured"
    assert h.send("BTS 65\nรายรับ เงินเดือน 30000").status == "income_unconfigured"
    assert not h.service.sheets.entries


@pytest.mark.parametrize("amount", ["0", "-1", "1.001", "NaN", "Infinity", "10000000", 1.2, True])
def test_invalid_money_is_rejected(amount):
    with pytest.raises(ValueError):
        AddTransactions.model_validate({"entries": [{"item": "BTS", "amount": amount,
                                                    "kind": "expense", "date": "2026-10-11"}]})


@pytest.mark.parametrize("extra", [{"category": "Transportation"}, {"sub_category": "x"}, {"owner_id": "intruder"}, {"spreadsheet_id": "other"}])
def test_tool_cannot_supply_classification_or_authorization(extra):
    with pytest.raises(ValueError):
        AddTransactions.model_validate({"entries": [{"item": "BTS", "amount": "65",
                                                    "kind": "expense", "date": "2026-10-11", **extra}]})
    with pytest.raises(ValueError):
        ToolRequest.model_validate({"name": "add_transactions", "arguments": {}, **extra})


def test_formula_collisions_are_rejected():
    with pytest.raises(ValueError):
        SheetLayout({"date": "A", "item": "B", "amount": "C", "note": "F"}, ("C", "D"))


def test_internal_spaces_are_not_stripped():
    catalog = ItemCatalog(["a b"], {})
    assert catalog.match("ab").canonical is None
    assert catalog.match(" A B ").canonical == "a b"


def test_ambiguous_master_names_are_rejected():
    with pytest.raises(ValueError):
        ItemCatalog(["BTS", "bts"], {})


def test_pending_selection_revalidates_master(h):
    result = h.send("sevne 100")
    h.service.catalog.items.pop("seven")
    assert h.send(f"เลือก {result.data['token']} seven").status == "clarification"
    assert not h.service.sheets.entries


def test_reads_do_not_discard_pending_batch(h):
    result = h.send("sevne 100")
    assert h.send("สรุปวันนี้").status == "summary"
    assert h.service.state.pending.token == result.data["token"]
    assert h.send("BTS 50").status == "clarification"
    assert not h.service.sheets.entries


def test_alias_is_invalidated_if_master_item_removed(h):
    h.service.catalog.items.pop("bts")
    assert h.service.catalog.match("บีทีเอส").canonical is None
    assert h.send("บีทีเอส 65").status == "clarification"
    assert not h.service.sheets.entries


def test_layout_copies_input_mapping_and_cannot_mutate_formula_policy():
    columns = {"date": "A", "item": "B", "amount": "E", "note": "F"}
    layout = SheetLayout(columns, ("C", "D"))
    columns["amount"] = "C"
    assert layout.columns["amount"] == "E"
    with pytest.raises(TypeError):
        layout.columns["amount"] = "C"
