from pathlib import Path

from src.tools.excel import parse_xlsx


FIXTURE = Path(__file__).parent / "fixtures" / "sample.xlsx"


def test_parses_all_sheets():
    result = parse_xlsx(str(FIXTURE))
    assert set(result.keys()) == {"Sales", "Costs"}


def test_sales_sheet_rows():
    result = parse_xlsx(str(FIXTURE))
    assert result["Sales"] == [
        {"date": "2026-01-01", "product": "A", "amount": 100},
        {"date": "2026-01-02", "product": "B", "amount": 250},
    ]


def test_costs_sheet_rows():
    result = parse_xlsx(str(FIXTURE))
    assert result["Costs"] == [{"category": "rent", "value": 1000}]


def test_parse_single_sheet():
    result = parse_xlsx(str(FIXTURE), sheet="Sales")
    assert isinstance(result, list)
    assert len(result) == 2


def test_short_row_fills_missing_columns_with_none(tmp_path):
    """A row shorter than the header should produce a dict with None for missing columns."""
    from openpyxl import Workbook

    path = tmp_path / "short.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["a", "b", "c"])
    ws.append([1, 2])
    wb.save(str(path))

    result = parse_xlsx(str(path), sheet="S")
    assert result == [{"a": 1, "b": 2, "c": None}]
