from unittest.mock import MagicMock, patch

import pytest

from src.tools import sheets


@pytest.fixture
def fake_service():
    svc = MagicMock()
    with patch.object(sheets, "_service", return_value=svc):
        yield svc


def test_read_range(fake_service):
    fake_service.spreadsheets().values().get().execute.return_value = {
        "values": [["a", "b"], ["c", "d"]],
        "range": "Sheet1!A1:B2",
    }
    result = sheets.read_range(spreadsheet_id="SID", range="Sheet1!A1:B2")
    fake_service.spreadsheets().values().get.assert_called_with(
        spreadsheetId="SID", range="Sheet1!A1:B2", valueRenderOption="UNFORMATTED_VALUE",
    )
    assert result["values"] == [["a", "b"], ["c", "d"]]
    assert result["_meta"]["range_read"] == "Sheet1!A1:B2"
    assert result["_meta"]["row_count"] == 2
    assert result["_meta"]["empty_reason"] is None


def test_read_range_empty_flags_no_data(fake_service):
    fake_service.spreadsheets().values().get().execute.return_value = {}
    result = sheets.read_range("SID", "Sheet1!A1")
    assert result["values"] == []
    assert result["_meta"]["empty_reason"] == "no_data"
    assert result["_meta"]["row_count"] == 0


def test_find_in_spreadsheet_without_labels(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Год факт"}}],
    }
    fake_service.spreadsheets().values().get().execute.return_value = {
        "values": [
            ["", "Янв", "Фев", "Год"],
            ["Выручка", 100, 200, 300],
            ["Чистая прибыль", 10, 20, 30],
        ],
    }
    result = sheets.find_in_spreadsheet("SID", "20")
    assert "matches" in result and "_meta" in result
    # Three 20s appear: Фев row 2 ("200" contains "20"), Год row 2 ("300" → "00"?), Фев row 3 (20).
    # We only assert the labels were NOT added.
    for m in result["matches"]:
        assert "row_label" not in m
        assert "col_label" not in m


def test_find_in_spreadsheet_with_labels_attaches_row_and_col(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Год факт"}}],
    }
    fake_service.spreadsheets().values().get().execute.return_value = {
        "values": [
            ["", "Янв", "Фев", "Год"],
            ["Выручка", 100, 200, 300],
            ["Чистая прибыль", 10, 20, 3087967],
        ],
    }
    result = sheets.find_in_spreadsheet("SID", "3087967", with_labels=True)
    assert result["_meta"]["with_labels"] is True
    assert result["_meta"]["match_count"] == 1
    m = result["matches"][0]
    assert m["row_label"] == "Чистая прибыль"
    assert m["col_label"] == "Год"


def test_find_in_spreadsheet_no_matches_flags_empty_reason(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "S"}}],
    }
    fake_service.spreadsheets().values().get().execute.return_value = {"values": [["a", "b"]]}
    result = sheets.find_in_spreadsheet("SID", "xyz")
    assert result["matches"] == []
    assert result["_meta"]["empty_reason"] == "no_matches"


def test_query_flags_10k_truncation(fake_service):
    # 10000 rows back from the temp sheet → truncated.
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 42}}}],
    }
    fake_service.spreadsheets().get().execute.return_value = {"properties": {"locale": "en_US"}}
    fake_service.spreadsheets().values().update().execute.return_value = {}
    fake_service.spreadsheets().values().get().execute.return_value = {
        "values": [["x"]] * 10000,
    }
    result = sheets.query("SID", "Sheet1!A:B", "SELECT A")
    assert result["_meta"]["truncated"] is True
    assert "10000" in result["_meta"]["truncation_reason"]


def test_iter_rows_meta_truncated_when_full_chunk(fake_service):
    rows = [["x"]] * 200
    fake_service.spreadsheets().values().get().execute.return_value = {
        "values": rows,
        "range": "'S'!A2:ZZ201",
    }
    result = sheets.iter_rows("SID", "S", offset=0, chunk_size=200)
    assert result["has_more"] is True
    assert result["_meta"]["truncated"] is True
    assert "chunk_size=200" in result["_meta"]["truncation_reason"]


# ---- Phase 1: provenance tools ----

def test_read_range_formatted_passes_correct_option(fake_service):
    fake_service.spreadsheets().values().get().execute.return_value = {
        "values": [["3 087 967 ₽"]],
        "range": "'Год факт'!B45",
    }
    result = sheets.read_range("SID", "'Год факт'!B45", formatted=True)
    # Check that valueRenderOption was passed
    call_kwargs = fake_service.spreadsheets().values().get.call_args.kwargs
    assert call_kwargs.get("valueRenderOption") == "FORMATTED_VALUE"
    assert result["values"] == [["3 087 967 ₽"]]
    assert result["_meta"]["value_mode"] == "formatted"


def test_read_range_default_uses_unformatted(fake_service):
    fake_service.spreadsheets().values().get().execute.return_value = {
        "values": [[3087967]],
        "range": "'Год факт'!B45",
    }
    result = sheets.read_range("SID", "'Год факт'!B45")
    call_kwargs = fake_service.spreadsheets().values().get.call_args.kwargs
    # Google's API default is FORMATTED_VALUE (returns strings) — we override
    # to UNFORMATTED_VALUE so numbers come back as numbers.
    assert call_kwargs.get("valueRenderOption") == "UNFORMATTED_VALUE"
    assert result["_meta"]["value_mode"] == "raw"


def test_batch_read_passes_all_ranges(fake_service):
    fake_service.spreadsheets().values().batchGet().execute.return_value = {
        "valueRanges": [
            {"range": "Sheet1!A1", "values": [["a"]]},
            {"range": "Sheet2!B5", "values": [["b"]]},
        ],
    }
    result = sheets.batch_read("SID", ["Sheet1!A1", "Sheet2!B5"])
    call_kwargs = fake_service.spreadsheets().values().batchGet.call_args.kwargs
    assert call_kwargs["ranges"] == ["Sheet1!A1", "Sheet2!B5"]
    assert call_kwargs.get("valueRenderOption") == "UNFORMATTED_VALUE"
    assert len(result["per_range"]) == 2
    assert result["per_range"][0]["values"] == [["a"]]
    assert result["_meta"]["requested_count"] == 2
    assert result["_meta"]["returned_count"] == 2


def test_batch_read_empty_list_short_circuits(fake_service):
    result = sheets.batch_read("SID", [])
    assert result["per_range"] == []
    assert result["_meta"]["empty_reason"] == "no_ranges"
    fake_service.spreadsheets().values().batchGet.assert_not_called()


def test_list_named_ranges_resolves_sheet_names(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Год факт"}},
            {"properties": {"sheetId": 7, "title": "Год план"}},
        ],
        "namedRanges": [
            {
                "namedRangeId": "nr_1",
                "name": "Чистая_прибыль_Год",
                "range": {"sheetId": 0, "startRowIndex": 44, "endRowIndex": 45,
                          "startColumnIndex": 1, "endColumnIndex": 2},
            },
        ],
    }
    result = sheets.list_named_ranges("SID")
    assert result["_meta"]["count"] == 1
    nr = result["named_ranges"][0]
    assert nr["name"] == "Чистая_прибыль_Год"
    assert nr["sheet"] == "Год факт"
    assert nr["range"] == "'Год факт'!B45:B45"


def test_list_named_ranges_empty_flags_reason(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {"sheets": []}
    result = sheets.list_named_ranges("SID")
    assert result["named_ranges"] == []
    assert result["_meta"]["empty_reason"] == "no_named_ranges"


def test_read_named_range_passes_name_as_range(fake_service):
    fake_service.spreadsheets().values().get().execute.return_value = {
        "values": [[3_087_967]],
        "range": "'Год факт'!B45",
    }
    result = sheets.read_named_range("SID", "Чистая_прибыль_Год")
    call_kwargs = fake_service.spreadsheets().values().get.call_args.kwargs
    assert call_kwargs["range"] == "Чистая_прибыль_Год"
    assert result["values"] == [[3_087_967]]
    assert result["_meta"]["name"] == "Чистая_прибыль_Год"
    assert result["_meta"]["range_read"] == "'Год факт'!B45"


def test_create_named_range_builds_correct_batch_update(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 42, "title": "Год факт"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addNamedRange": {"namedRange": {
            "namedRangeId": "nr_new",
            "name": "Test",
            "range": {"sheetId": 42, "startRowIndex": 44, "endRowIndex": 45,
                      "startColumnIndex": 1, "endColumnIndex": 2},
        }}}],
    }
    result = sheets.create_named_range("SID", "Test", "Год факт!B45")
    # Inspect batchUpdate request
    bu_call = fake_service.spreadsheets().batchUpdate.call_args
    req = bu_call.kwargs["body"]["requests"][0]["addNamedRange"]["namedRange"]
    assert req["name"] == "Test"
    assert req["range"]["sheetId"] == 42
    assert req["range"]["startRowIndex"] == 44
    assert req["range"]["endRowIndex"] == 45
    assert req["range"]["startColumnIndex"] == 1
    assert req["range"]["endColumnIndex"] == 2
    assert result["named_range_id"] == "nr_new"
    assert result["ok"] is True


def test_create_named_range_rejects_unknown_sheet(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {"sheets": []}
    import pytest
    with pytest.raises(ValueError, match="not found"):
        sheets.create_named_range("SID", "Test", "NoSuchSheet!A1")


def test_duplicate_sheet_calls_duplicate_request(fake_service):
    """Passing numeric sheetId directly (skips resolution)."""
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"duplicateSheet": {"properties": {
            "sheetId": 99, "title": "Год факт 2026", "index": 3,
        }}}],
    }
    result = sheets.duplicate_sheet("SID", source_sheet=42, new_name="Год факт 2026")
    bu_call = fake_service.spreadsheets().batchUpdate.call_args
    req = bu_call.kwargs["body"]["requests"][0]["duplicateSheet"]
    assert req["sourceSheetId"] == 42
    assert req["newSheetName"] == "Год факт 2026"
    assert result["new_sheet_id"] == 99


def test_duplicate_sheet_resolves_name_to_id(fake_service):
    """Passing tab title triggers metadata resolution to numeric id."""
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 7, "title": "Год факт"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"duplicateSheet": {"properties": {"sheetId": 99, "title": "x"}}}],
    }
    sheets.duplicate_sheet("SID", source_sheet="Год факт", new_name="x")
    bu_call = fake_service.spreadsheets().batchUpdate.call_args
    req = bu_call.kwargs["body"]["requests"][0]["duplicateSheet"]
    assert req["sourceSheetId"] == 7  # resolved from title


def test_copy_sheet_to_calls_copy_to(fake_service):
    """Passing numeric sheetId directly."""
    fake_service.spreadsheets().sheets().copyTo().execute.return_value = {
        "sheetId": 88, "title": "Copy of Год факт",
    }
    result = sheets.copy_sheet_to("SRC_ID", source_sheet=42, dest_spreadsheet_id="DEST_ID")
    call_kwargs = fake_service.spreadsheets().sheets().copyTo.call_args.kwargs
    assert call_kwargs["spreadsheetId"] == "SRC_ID"
    assert call_kwargs["sheetId"] == 42
    assert call_kwargs["body"] == {"destinationSpreadsheetId": "DEST_ID"}
    assert result["copied_sheet_id"] == 88


def test_copy_sheet_to_resolves_name_to_id(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 11, "title": "Год факт"}}],
    }
    fake_service.spreadsheets().sheets().copyTo().execute.return_value = {"sheetId": 88}
    sheets.copy_sheet_to("SRC_ID", source_sheet="Год факт", dest_spreadsheet_id="DEST_ID")
    call_kwargs = fake_service.spreadsheets().sheets().copyTo.call_args.kwargs
    assert call_kwargs["sheetId"] == 11


# ---- Phase 2: formatting / charts / pivots / validation ----

def test_set_format_preset_currency(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {}
    result = sheets.set_format("SID", "Sheet1!B2", preset="currency_rub_int")
    assert result["ok"]
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    req = body["requests"][0]["repeatCell"]
    assert req["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"
    assert "₽" in req["cell"]["userEnteredFormat"]["numberFormat"]["pattern"]


def test_set_format_requires_something_to_apply(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    with pytest.raises(ValueError, match="nothing to apply"):
        sheets.set_format("SID", "Sheet1!A1")


def test_freeze_sends_correct_grid_properties(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 7, "title": "Год факт"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {}
    sheets.freeze("SID", "Год факт", rows=1, cols=2)
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    props = body["requests"][0]["updateSheetProperties"]["properties"]
    assert props["sheetId"] == 7
    assert props["gridProperties"]["frozenRowCount"] == 1
    assert props["gridProperties"]["frozenColumnCount"] == 2


def test_merge_cells_default_type(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {}
    sheets.merge_cells("SID", "Sheet1!A1:C1")
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    req = body["requests"][0]["mergeCells"]
    assert req["mergeType"] == "MERGE_ALL"
    assert req["range"]["sheetId"] == 0


def test_data_validation_dropdown_builds_one_of_list(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {}
    sheets.set_data_validation("SID", "Sheet1!B2:B10", kind="dropdown", values=["A", "B"])
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    rule = body["requests"][0]["setDataValidation"]["rule"]
    assert rule["condition"]["type"] == "ONE_OF_LIST"
    assert [v["userEnteredValue"] for v in rule["condition"]["values"]] == ["A", "B"]


def test_data_validation_rejects_unknown_kind(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    with pytest.raises(ValueError, match="unknown validation"):
        sheets.set_data_validation("SID", "Sheet1!A1", kind="unicorn")


def test_conditional_format_negatives_red_sets_number_less(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {}
    result = sheets.set_conditional_format("SID", "Sheet1!A1:A10", condition="negatives_red")
    assert result["ok"]
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    rule = body["requests"][0]["addConditionalFormatRule"]["rule"]
    assert rule["booleanRule"]["condition"]["type"] == "NUMBER_LESS"
    assert rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"] == "0"


def test_create_chart_pie_builds_pie_spec(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addChart": {"chart": {"chartId": 99}}}],
    }
    result = sheets.create_chart(
        "SID", "Sheet1", chart_type="pie", title="X",
        domain_range="Sheet1!A1:A3", series_ranges=["Sheet1!B1:B3"],
    )
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    spec = body["requests"][0]["addChart"]["chart"]["spec"]
    assert "pieChart" in spec
    assert result["chart_id"] == 99


def test_create_chart_rejects_unknown_type(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    with pytest.raises(ValueError, match="unknown chart_type"):
        sheets.create_chart(
            "SID", "Sheet1", chart_type="3d_holo", title="x",
            domain_range="Sheet1!A1", series_ranges=["Sheet1!B1"],
        )


def test_create_pivot_resolves_headers_to_offsets(fake_service):
    # First call: get sheet metadata for source resolution + header read
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sales"}}],
    }
    # values.get returns header row
    fake_service.spreadsheets().values().get().execute.return_value = {
        "values": [["Brand", "Date", "Revenue"]],
    }
    # batchUpdate returns add-sheet reply (for new pivot tab) then update-cells (no reply needed)
    fake_service.spreadsheets().batchUpdate().execute.side_effect = [
        {"replies": [{"addSheet": {"properties": {"sheetId": 42}}}]},
        {},
    ]
    result = sheets.create_pivot(
        "SID",
        source_range="Sales!A1:C100",
        rows=["Brand"],
        values=[{"column": "Revenue", "aggregate": "SUM"}],
    )
    assert result["ok"]
    assert result["dest_sheet_id"] == 42


# ---- Phase 3: collaboration ----

def test_add_protected_range_strict(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Год факт"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addProtectedRange": {"protectedRange": {"protectedRangeId": 777}}}],
    }
    result = sheets.add_protected_range(
        "SID", "Год факт!B45:B45", description="freeze net profit",
        editors=["egor.titt@gmail.com"],
    )
    assert result["protected_range_id"] == 777
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    pr = body["requests"][0]["addProtectedRange"]["protectedRange"]
    assert pr["warningOnly"] is False
    assert pr["editors"]["users"] == ["egor.titt@gmail.com"]
    assert pr["description"] == "freeze net profit"


def test_add_protected_range_warning_only_drops_editors(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "S"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addProtectedRange": {"protectedRange": {"protectedRangeId": 1}}}],
    }
    sheets.add_protected_range("SID", "S!A1", warning_only=True, editors=["x@y.com"])
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    pr = body["requests"][0]["addProtectedRange"]["protectedRange"]
    assert pr["warningOnly"] is True
    # Editors not enforced for warning_only
    assert "editors" not in pr


def test_list_protected_ranges_resolves_sheet_names(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [
            {
                "properties": {"sheetId": 0, "title": "Год факт"},
                "protectedRanges": [{
                    "protectedRangeId": 5,
                    "description": "lock",
                    "warningOnly": False,
                    "range": {"sheetId": 0, "startRowIndex": 44, "endRowIndex": 45,
                              "startColumnIndex": 1, "endColumnIndex": 2},
                    "editors": {"users": ["x@y.com"]},
                }],
            },
        ],
    }
    result = sheets.list_protected_ranges("SID")
    assert result["_meta"]["count"] == 1
    pr = result["protected_ranges"][0]
    assert pr["protected_range_id"] == 5
    assert pr["sheet"] == "Год факт"
    assert pr["editors"] == ["x@y.com"]


def test_list_protected_ranges_empty_flag(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
    }
    result = sheets.list_protected_ranges("SID")
    assert result["protected_ranges"] == []
    assert result["_meta"]["empty_reason"] == "no_protected_ranges"


def test_remove_protected_range_sends_delete(fake_service):
    fake_service.spreadsheets().batchUpdate().execute.return_value = {}
    sheets.remove_protected_range("SID", 555)
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    assert body["requests"][0]["deleteProtectedRange"]["protectedRangeId"] == 555


def test_set_cell_note_uses_repeat_cell(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"sheetId": 0, "title": "S"}}],
    }
    fake_service.spreadsheets().batchUpdate().execute.return_value = {}
    result = sheets.set_cell_note("SID", "S!B45", "проверь с бухгалтером")
    assert result["ok"]
    body = fake_service.spreadsheets().batchUpdate.call_args.kwargs["body"]
    rc = body["requests"][0]["repeatCell"]
    assert rc["fields"] == "note"
    assert rc["cell"]["note"] == "проверь с бухгалтером"


def test_get_cell_notes_flattens_grid(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"data": [{"rowData": [
            {"values": [{"note": "a"}, {}]},
            {"values": [{}, {"note": "b"}]},
        ]}]}],
    }
    result = sheets.get_cell_notes("SID", "S!A1:B2")
    assert result["notes"] == [["a", None], [None, "b"]]
    assert result["_meta"]["non_empty_count"] == 2


def test_get_cell_notes_empty_flag(fake_service):
    fake_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"data": [{"rowData": [
            {"values": [{}, {}]},
        ]}]}],
    }
    result = sheets.get_cell_notes("SID", "S!A1:B1")
    assert result["_meta"]["empty_reason"] == "no_notes"


def test_write_range(fake_service):
    fake_service.spreadsheets().values().update().execute.return_value = {"updatedCells": 4}
    result = sheets.write_range("SID", "Sheet1!A1:B2", [[1, 2], [3, 4]])
    fake_service.spreadsheets().values().update.assert_called_with(
        spreadsheetId="SID",
        range="Sheet1!A1:B2",
        valueInputOption="USER_ENTERED",
        body={"values": [[1, 2], [3, 4]]},
    )
    assert result == {"updatedCells": 4}


def test_append_rows(fake_service):
    fake_service.spreadsheets().values().append().execute.return_value = {"updates": {"updatedRows": 2}}
    sheets.append_rows("SID", "Sheet1!A1", [["x"], ["y"]])
    fake_service.spreadsheets().values().append.assert_called_with(
        spreadsheetId="SID",
        range="Sheet1!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [["x"], ["y"]]},
    )


def test_clear_range(fake_service):
    fake_service.spreadsheets().values().clear().execute.return_value = {}
    sheets.clear_range("SID", "Sheet1!A:Z")
    fake_service.spreadsheets().values().clear.assert_called_with(
        spreadsheetId="SID", range="Sheet1!A:Z", body={}
    )


def test_create_spreadsheet(fake_service):
    fake_service.spreadsheets().create().execute.return_value = {"spreadsheetId": "NEW", "spreadsheetUrl": "..."}
    result = sheets.create_spreadsheet(title="My Report")
    fake_service.spreadsheets().create.assert_called_with(
        body={"properties": {"title": "My Report"}},
        fields="spreadsheetId,spreadsheetUrl,properties.title",
    )
    assert result == {"spreadsheetId": "NEW", "spreadsheetUrl": "..."}


def test_add_sheet(fake_service):
    fake_service.spreadsheets().batchUpdate().execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 99, "title": "T"}}}]
    }
    result = sheets.add_sheet("SID", "T")
    fake_service.spreadsheets().batchUpdate.assert_called_with(
        spreadsheetId="SID",
        body={"requests": [{"addSheet": {"properties": {"title": "T"}}}]},
    )
    assert result == {"sheetId": 99, "title": "T"}


# ---------- bug_010 lockdown: fuzzy label match is one-directional ----------

def test_fuzzy_label_match_needle_in_label():
    """Query 'Прибыль' matches a named range called 'Чистая прибыль' — the
    label contains the needle, so the user's search term is a sub-phrase of
    a more specific metric."""
    from src.tools.sheets import _fuzzy_label_match
    assert _fuzzy_label_match("Чистая прибыль", "Прибыль") is True


def test_fuzzy_label_match_label_in_needle_does_not_match():
    """REGRESSION: previously bidirectional. Query 'Чистая прибыль' must NOT
    match a named range called just 'Прибыль' — they're different metrics.
    Returning a less-specific range for a more-specific query silently
    answered with the wrong number."""
    from src.tools.sheets import _fuzzy_label_match
    assert _fuzzy_label_match("Прибыль", "Чистая прибыль") is False


def test_fuzzy_label_match_underscore_normalization():
    """Named-range convention `Chistaya_Pribyl` should match the human label."""
    from src.tools.sheets import _fuzzy_label_match
    assert _fuzzy_label_match("Chistaya_Pribyl", "chistaya pribyl") is True


def test_fuzzy_label_match_empty_inputs():
    from src.tools.sheets import _fuzzy_label_match
    assert _fuzzy_label_match("", "x") is False
    assert _fuzzy_label_match("x", "") is False
    assert _fuzzy_label_match("", "") is False


# ---------- bug_017 lockdown: sheet names with spaces/cyrillic must be quoted ----------

def test_quote_sheet_name_basic():
    from src.tools.sheets import _quote_sheet_name
    assert _quote_sheet_name("Sheet1") == "'Sheet1'"
    assert _quote_sheet_name("Год факт") == "'Год факт'"


def test_quote_sheet_name_doubles_internal_quotes():
    """Google's A1 spec: embedded ' becomes ''."""
    from src.tools.sheets import _quote_sheet_name
    assert _quote_sheet_name("Lena's data") == "'Lena''s data'"


def test_find_in_spreadsheet_quotes_sheet_with_space():
    """REGRESSION: unquoted sheet names break `f"{sheet}!A1"` when the name
    has a space or non-Latin characters. The Sheets API parses 'Год факт!A1'
    as a malformed range."""
    from unittest.mock import patch, MagicMock
    from src.tools import sheets

    fake = MagicMock()
    fake.spreadsheets().get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Год факт"}}],
    }
    fake.spreadsheets().values().get.return_value.execute.return_value = {
        "values": [["foo"]],
    }
    with patch.object(sheets, "_service", return_value=fake):
        sheets.find_in_spreadsheet("SID", "foo")

    # The values().get range argument must contain the quoted name.
    calls = fake.spreadsheets().values().get.call_args_list
    ranges = [c.kwargs.get("range", "") for c in calls if "range" in c.kwargs]
    assert any("'Год факт'" in r for r in ranges), ranges


def test_last_data_row_quotes_sheet_with_space():
    """REGRESSION: `last_data_row(sheet='Год факт', column='B')` previously
    built `Год факт!B1:B` — invalid A1 syntax."""
    from unittest.mock import patch, MagicMock
    from src.tools import sheets

    fake = MagicMock()
    fake.spreadsheets().values().get.return_value.execute.return_value = {
        "values": [["hdr"], ["row1"], ["row2"]],
    }
    with patch.object(sheets, "_service", return_value=fake):
        sheets.last_data_row("SID", sheet="Год факт", column="B")

    used = fake.spreadsheets().values().get.call_args.kwargs.get("range", "")
    assert used.startswith("'Год факт'!"), used
