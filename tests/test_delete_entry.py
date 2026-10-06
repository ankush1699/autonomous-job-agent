"""
tests/test_delete_entry.py

Covers server.delete_entry() — removes an entry from ENTRIES and persists
the change, 404s on an unknown id, and does not touch anything else (no
output-folder deletion — generated resume/CL files stay on disk even after
their tracking row is deleted).

Calls the FastAPI route function directly (bypassing HTTP/auth), matching
the pattern in test_score_entry_throttle.py.

Run: pytest tests/test_delete_entry.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("APP_PASSWORD", "test-only")

import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def test_delete_entry_removes_it_and_saves():
    server.ENTRIES["e1"] = {"id": "e1", "company": "Co", "title": "SWE",
                             "output_folder": "/some/real/folder", "files": ["a.pdf"]}
    with patch("core.entries_store.save") as mock_save:
        result = server.delete_entry("e1")

    assert result == {"deleted": "e1"}
    assert "e1" not in server.ENTRIES
    mock_save.assert_called_once_with(server.ENTRIES)


def test_delete_unknown_entry_404s():
    assert "not-a-real-id" not in server.ENTRIES
    try:
        server.delete_entry("not-a-real-id")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def _make_entry(score, score_status="scored", applied=False):
    return {"score": score, "score_status": score_status, "applied": applied}


def test_delete_below_threshold_only_removes_low_scored_unapplied_entries():
    server.ENTRIES.clear()
    server.ENTRIES["low"] = _make_entry(50)
    server.ENTRIES["high"] = _make_entry(90)
    server.ENTRIES["low_but_applied"] = _make_entry(40, applied=True)
    server.ENTRIES["still_scoring"] = _make_entry(None, score_status="scoring")
    server.ENTRIES["errored"] = _make_entry(None, score_status="error")

    with patch("core.entries_store.save") as mock_save:
        result = server.delete_below_threshold(server.BulkDeleteRequest(min_score=75, skip_applied=True))

    assert result["deleted_count"] == 1
    assert result["deleted_ids"] == ["low"]
    assert "low" not in server.ENTRIES
    assert set(server.ENTRIES) == {"high", "low_but_applied", "still_scoring", "errored"}
    mock_save.assert_called_once_with(server.ENTRIES)


def test_delete_below_threshold_can_include_applied_when_skip_applied_false():
    server.ENTRIES.clear()
    server.ENTRIES["low_applied"] = _make_entry(30, applied=True)

    with patch("core.entries_store.save"):
        result = server.delete_below_threshold(server.BulkDeleteRequest(min_score=75, skip_applied=False))

    assert result["deleted_count"] == 1
    assert "low_applied" not in server.ENTRIES


def test_delete_below_threshold_also_deletes_from_sheet_when_requested():
    server.ENTRIES.clear()
    server.ENTRIES["low"] = _make_entry(30)

    with patch("core.entries_store.save"), \
         patch("scraper.sheets.delete_rows_below_score", return_value={"deleted_count": 12, "kept_count": 4}) as mock_sheet:
        result = server.delete_below_threshold(
            server.BulkDeleteRequest(min_score=75, skip_applied=True, also_delete_from_sheet=True)
        )

    mock_sheet.assert_called_once_with(75, skip_applied=True)
    assert result["sheet_deleted_count"] == 12
    assert result["sheet_kept_count"] == 4


def test_delete_below_threshold_sheet_failure_does_not_lose_entries_result():
    """A sheet API failure shouldn't hide that entries.json cleanup already
    succeeded — the entries-side result must still come back correctly."""
    server.ENTRIES.clear()
    server.ENTRIES["low"] = _make_entry(30)

    with patch("core.entries_store.save"), \
         patch("scraper.sheets.delete_rows_below_score", side_effect=RuntimeError("sheet down")):
        result = server.delete_below_threshold(
            server.BulkDeleteRequest(min_score=75, skip_applied=True, also_delete_from_sheet=True)
        )

    assert result["deleted_count"] == 1
    assert "sheet down" in result["sheet_error"]


def test_delete_below_threshold_leaves_sheet_untouched_by_default():
    server.ENTRIES.clear()
    server.ENTRIES["low"] = _make_entry(30)

    with patch("core.entries_store.save"), \
         patch("scraper.sheets.delete_rows_below_score") as mock_sheet:
        result = server.delete_below_threshold(server.BulkDeleteRequest(min_score=75))

    mock_sheet.assert_not_called()
    assert "sheet_deleted_count" not in result
