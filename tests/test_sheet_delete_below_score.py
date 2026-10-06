"""
tests/test_sheet_delete_below_score.py

Covers scraper/sheets.py::delete_rows_below_score() — the sheet-side half
of "delete everything below 75 from everywhere" (entries.json is covered by
test_delete_entry.py's delete_below_threshold tests). Implemented as
read-all -> filter -> clear body -> rewrite, so this mocks a fake worksheet
object exposing get_all_values/batch_clear/update and asserts only the
correct rows survive the rewrite.

Run: pytest tests/test_sheet_delete_below_score.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import sheets


_HEADER = [
    "Score", "Company", "Job Title", "Platform", "Location", "Remote",
    "Date Posted", "JD Summary", "Fit Reasoning", "Apply Link",
    "Output Folder", "Job Type", "Resume Status", "Notes", "Salary", "Application Status",
    "Date Added",
]


def _row(score, company, app_status="—"):
    return [str(score), company, "SWE", "linkedin", "", "No", "", "", "", f"http://x/{company}",
            "", "", "pending", "", "", app_status, ""]


def _make_fake_ws(data_rows):
    ws = MagicMock()
    ws.get_all_values.return_value = [_HEADER] + data_rows
    return ws


def test_keeps_rows_at_or_above_threshold_and_deletes_the_rest():
    rows = [_row(90, "High"), _row(40, "Low"), _row(75, "Exactly")]
    ws = _make_fake_ws(rows)

    with patch.object(sheets, "_get_client"), \
         patch.object(sheets, "_open_spreadsheet"), \
         patch.object(sheets, "_get_or_create_tab", return_value=ws):
        result = sheets.delete_rows_below_score(75)

    assert result == {"deleted_count": 1, "kept_count": 2}
    ws.batch_clear.assert_called_once()
    written = ws.update.call_args[0][1]
    kept_companies = {r[1] for r in written}
    assert kept_companies == {"High", "Exactly"}


def test_applied_row_survives_even_below_threshold_by_default():
    rows = [_row(20, "AppliedLow", app_status="Applied"), _row(20, "NotApplied")]
    ws = _make_fake_ws(rows)

    with patch.object(sheets, "_get_client"), \
         patch.object(sheets, "_open_spreadsheet"), \
         patch.object(sheets, "_get_or_create_tab", return_value=ws):
        result = sheets.delete_rows_below_score(75, skip_applied=True)

    assert result["deleted_count"] == 1
    written = ws.update.call_args[0][1]
    assert {r[1] for r in written} == {"AppliedLow"}


def test_skip_applied_false_deletes_low_scored_rows_regardless_of_status():
    rows = [_row(20, "AppliedLow", app_status="Applied")]
    ws = _make_fake_ws(rows)

    with patch.object(sheets, "_get_client"), \
         patch.object(sheets, "_open_spreadsheet"), \
         patch.object(sheets, "_get_or_create_tab", return_value=ws):
        result = sheets.delete_rows_below_score(75, skip_applied=False)

    assert result["deleted_count"] == 1
    assert result["kept_count"] == 0


def test_nothing_to_delete_does_not_touch_the_sheet():
    rows = [_row(90, "High")]
    ws = _make_fake_ws(rows)

    with patch.object(sheets, "_get_client"), \
         patch.object(sheets, "_open_spreadsheet"), \
         patch.object(sheets, "_get_or_create_tab", return_value=ws):
        result = sheets.delete_rows_below_score(75)

    assert result == {"deleted_count": 0, "kept_count": 1}
    ws.batch_clear.assert_not_called()
    ws.update.assert_not_called()


def test_empty_sheet_is_a_noop():
    ws = _make_fake_ws([])
    with patch.object(sheets, "_get_client"), \
         patch.object(sheets, "_open_spreadsheet"), \
         patch.object(sheets, "_get_or_create_tab", return_value=ws):
        result = sheets.delete_rows_below_score(75)
    assert result == {"deleted_count": 0, "kept_count": 0}
