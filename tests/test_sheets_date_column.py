"""
tests/test_sheets_date_column.py

Covers the "Date Added" column added to scraper/sheets.py (column Q) and
server.py's use of it on re-import — rows written by a scrape cycle get a
real timestamp, and _new_entry_from_scraped_job() uses a caller-supplied
created_at (from that column) instead of always defaulting to "now", so
backfilled historical jobs don't all misleadingly show up as "added today"
in the Applications tab's date filter.

Run: pytest tests/test_sheets_date_column.py -v
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper.sheets import _job_to_row, _HEADERS, _COL_DATE_ADDED


def test_headers_and_job_to_row_have_seventeen_columns_aligned():
    assert len(_HEADERS) == 17
    assert _HEADERS[-1] == "Date Added"
    row = _job_to_row({"company": "Co", "title": "SWE"})
    assert len(row) == 17
    assert _COL_DATE_ADDED == 17


def test_job_to_row_date_added_is_a_parseable_iso_timestamp():
    row = _job_to_row({"company": "Co", "title": "SWE"})
    date_added = row[_COL_DATE_ADDED - 1]  # 1-indexed column -> 0-indexed list
    parsed = datetime.datetime.fromisoformat(date_added)
    assert parsed.tzinfo is not None  # UTC-aware, not naive


def test_new_entry_from_scraped_job_uses_supplied_created_at():
    os.environ.setdefault("APP_PASSWORD", "test-only")
    import server

    job = {"company": "Co", "title": "SWE", "score": 80}
    historical = "2026-07-01T00:00:00+00:00"
    entry = server._new_entry_from_scraped_job(job, created_at=historical)
    assert entry["created_at"] == historical

    entry_default = server._new_entry_from_scraped_job(job)
    assert entry_default["created_at"] != historical  # falls back to _now()
