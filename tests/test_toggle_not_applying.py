"""
tests/test_toggle_not_applying.py

Covers server.toggle_not_applying() — marks an entry as "decided not to
apply" without deleting it, and the mutual-exclusion with toggle_applied()
(an entry can't be both "applied" and "not applying" at once).

Calls the FastAPI route function directly (bypassing HTTP/auth), matching
the pattern in test_delete_entry.py.

Run: pytest tests/test_toggle_not_applying.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("APP_PASSWORD", "test-only")

import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _make_entry(entry_id, **overrides):
    entry = {"id": entry_id, "company": "Co", "title": "SWE",
             "applied": False, "applied_at": None,
             "not_applying": False, "not_applying_at": None}
    entry.update(overrides)
    return entry


def test_toggle_not_applying_sets_flag_and_timestamp():
    server.ENTRIES.clear()
    server.ENTRIES["e1"] = _make_entry("e1")

    with patch("core.entries_store.save") as mock_save:
        result = server.toggle_not_applying("e1")

    assert result["not_applying"] is True
    assert result["not_applying_at"] is not None
    mock_save.assert_called_once_with(server.ENTRIES)


def test_toggle_not_applying_twice_undoes_it():
    server.ENTRIES.clear()
    server.ENTRIES["e1"] = _make_entry("e1")

    with patch("core.entries_store.save"):
        server.toggle_not_applying("e1")
        result = server.toggle_not_applying("e1")

    assert result["not_applying"] is False
    assert result["not_applying_at"] is None


def test_toggle_not_applying_unknown_entry_404s():
    try:
        server.toggle_not_applying("not-a-real-id")
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def test_marking_not_applying_clears_applied():
    server.ENTRIES.clear()
    server.ENTRIES["e1"] = _make_entry("e1", applied=True, applied_at="2026-08-12T00:00:00")

    with patch("core.entries_store.save"):
        result = server.toggle_not_applying("e1")

    assert result["not_applying"] is True
    assert result["applied"] is False
    assert result["applied_at"] is None


def test_marking_applied_clears_not_applying():
    server.ENTRIES.clear()
    server.ENTRIES["e1"] = _make_entry("e1", not_applying=True, not_applying_at="2026-08-12T00:00:00")

    with patch("core.entries_store.save"):
        result = server.toggle_applied("e1")

    assert result["applied"] is True
    assert result["not_applying"] is False
    assert result["not_applying_at"] is None
