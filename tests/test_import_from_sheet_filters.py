"""
tests/test_import_from_sheet_filters.py

Regression test for scoping import-from-sheet: the first version imported
the ENTIRE sheet history unconditionally (199 rows in one click), which is
exactly the "too cluttered" problem the user then had to clean up. This
covers the min_score and date_from/date_to filters added afterward —
verifying rows below the score floor or outside the date window are
skipped, while rows with NO "Date Added" (older, pre-column rows) are
included rather than silently dropped, since "unknown" isn't "out of range".

Calls server.import_from_sheet() directly (bypassing HTTP/auth).

Run: pytest tests/test_import_from_sheet_filters.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("APP_PASSWORD", "test-only")

import server  # noqa: E402


def _row(company, score, date_added="", link=None):
    return {
        "Company": company, "Job Title": "SWE", "Score": score,
        "Apply Link": link or f"http://x/{company}",
        "Date Added": date_added, "Fit Reasoning": "", "JD Summary": "",
        "Output Folder": "", "Application Status": "",
    }


def test_min_score_filters_out_low_rows():
    server.ENTRIES.clear()
    rows = [_row("Low", 40), _row("High", 90)]
    with patch("scraper.sheets.read_all_rows", return_value=rows), \
         patch("scraper.seen_jobs.load_job_from_cache", return_value=None), \
         patch("core.entries_store.save"):
        result = server.import_from_sheet(server.ImportFromSheetRequest(min_score=75))

    assert result["imported_summary_only"] == 1
    assert result["skipped_below_threshold"] == 1
    companies = {e["company"] for e in server.ENTRIES.values()}
    assert companies == {"High"}


def test_date_range_filters_out_of_window_rows():
    server.ENTRIES.clear()
    rows = [
        _row("TooOld", 90, date_added="2026-01-01T00:00:00+00:00"),
        _row("InRange", 90, date_added="2026-08-05T00:00:00+00:00"),
        _row("TooNew", 90, date_added="2026-12-01T00:00:00+00:00"),
    ]
    with patch("scraper.sheets.read_all_rows", return_value=rows), \
         patch("scraper.seen_jobs.load_job_from_cache", return_value=None), \
         patch("core.entries_store.save"):
        result = server.import_from_sheet(
            server.ImportFromSheetRequest(date_from="2026-08-01", date_to="2026-08-10")
        )

    assert result["skipped_out_of_range"] == 2
    companies = {e["company"] for e in server.ENTRIES.values()}
    assert companies == {"InRange"}


def test_rows_with_no_date_added_are_included_not_dropped():
    """Rows written before the Date Added column existed have it blank —
    unknown date should be treated as in-range, not filtered out."""
    server.ENTRIES.clear()
    rows = [_row("NoDate", 90, date_added="")]
    with patch("scraper.sheets.read_all_rows", return_value=rows), \
         patch("scraper.seen_jobs.load_job_from_cache", return_value=None), \
         patch("core.entries_store.save"):
        result = server.import_from_sheet(
            server.ImportFromSheetRequest(date_from="2026-08-01", date_to="2026-08-10")
        )

    assert result["skipped_out_of_range"] == 0
    assert result["imported_summary_only"] == 1


def test_no_filters_imports_everything_not_already_tracked():
    server.ENTRIES.clear()
    rows = [_row("A", 10), _row("B", 99)]
    with patch("scraper.sheets.read_all_rows", return_value=rows), \
         patch("scraper.seen_jobs.load_job_from_cache", return_value=None), \
         patch("core.entries_store.save"):
        result = server.import_from_sheet(server.ImportFromSheetRequest())

    assert result["imported_summary_only"] == 2
    assert result["skipped_below_threshold"] == 0
    assert result["skipped_out_of_range"] == 0
