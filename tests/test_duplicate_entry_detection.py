"""
tests/test_duplicate_entry_detection.py

Covers server._find_duplicate_entry() and its use in create_entry(): a
manually-pasted JD must not create a second Applications-tab row if it
matches an existing entry (by JD content hash or apply_link) regardless of
that entry's source (manual/scraper) or current status (scoring, scored,
applied, errored).

Calls the FastAPI route function directly (bypassing HTTP/auth), matching
the pattern in test_delete_entry.py.

Run: pytest tests/test_duplicate_entry_detection.py -v
"""
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("APP_PASSWORD", "test-only")

import server  # noqa: E402
from core import jd_cache  # noqa: E402


def _make_entry(entry_id, jd_text="", apply_link="", **overrides):
    entry = {
        "id": entry_id, "company": "Co", "title": "SWE", "jd": jd_text,
        "jd_hash": jd_cache.jd_hash(jd_text) if jd_text else None,
        "apply_link": apply_link, "source": "manual",
        "apply_status": "idle", "score_status": "scored",
    }
    entry.update(overrides)
    return entry


def test_find_duplicate_by_jd_hash():
    server.ENTRIES.clear()
    server.ENTRIES["e1"] = _make_entry("e1", jd_text="We are hiring a great engineer.")

    dup = server._find_duplicate_entry("We are hiring a great engineer.")
    assert dup is not None
    assert dup["id"] == "e1"


def test_find_duplicate_by_apply_link_even_if_jd_text_differs():
    server.ENTRIES.clear()
    server.ENTRIES["e1"] = _make_entry("e1", jd_text="Original scraped text (truncated)...",
                                        apply_link="https://boards.example.com/jobs/123")

    dup = server._find_duplicate_entry("Full manually pasted JD text, different wording.",
                                        "https://boards.example.com/jobs/123")
    assert dup is not None
    assert dup["id"] == "e1"


def test_no_duplicate_when_neither_matches():
    server.ENTRIES.clear()
    server.ENTRIES["e1"] = _make_entry("e1", jd_text="Some JD.", apply_link="https://a.example.com")

    dup = server._find_duplicate_entry("A totally different JD.", "https://b.example.com")
    assert dup is None


def test_duplicate_detected_regardless_of_entry_status():
    server.ENTRIES.clear()
    server.ENTRIES["e1"] = _make_entry("e1", jd_text="Same JD text here.",
                                        score_status="error", apply_status="applied")

    dup = server._find_duplicate_entry("Same JD text here.")
    assert dup is not None
    assert dup["id"] == "e1"


def test_create_entry_returns_existing_entry_when_duplicate():
    server.ENTRIES.clear()
    server.ENTRIES["e1"] = _make_entry("e1", jd_text="Duplicate JD content.", apply_status="applied")

    with patch("core.entries_store.save") as mock_save, \
         patch.object(threading.Thread, "start") as mock_thread_start:
        result = server.create_entry(
            server.EntryCreateRequest(company="Co", title="SWE", jd="Duplicate JD content.")
        )

    assert result["duplicate"] is True
    assert result["id"] == "e1"
    assert len(server.ENTRIES) == 1
    mock_save.assert_not_called()
    mock_thread_start.assert_not_called()


def test_create_entry_creates_new_entry_when_no_duplicate():
    server.ENTRIES.clear()

    with patch("core.entries_store.save") as mock_save, \
         patch.object(threading.Thread, "start") as mock_thread_start:
        result = server.create_entry(
            server.EntryCreateRequest(company="Co", title="SWE", jd="Brand new JD text.")
        )

    assert "duplicate" not in result
    assert result["jd_hash"] == jd_cache.jd_hash("Brand new JD text.")
    assert len(server.ENTRIES) == 1
    mock_save.assert_called_once_with(server.ENTRIES)
    mock_thread_start.assert_called_once()


def test_on_new_entry_flags_scraper_job_matching_existing_manual_entry():
    """
    A scrape-cycle repost of an already-known JD (e.g. same text, new job
    board ID) must NOT be silently dropped — it's still created, just
    flagged with possible_duplicate/duplicate_note so the user can eyeball
    it instead of the scraper deciding for them.
    """
    server.ENTRIES.clear()
    server.ENTRIES["manual1"] = _make_entry("manual1", jd_text="Manually pasted JD.",
                                             apply_link="https://boards.example.com/jobs/999",
                                             company="Co", title="SWE", applied=True)

    duplicate = server._find_duplicate_entry("Manually pasted JD.", "https://boards.example.com/jobs/different-id")
    assert duplicate is not None
    assert duplicate["id"] == "manual1"

    note = server._duplicate_note(duplicate)
    assert "Co" in note and "SWE" in note
    assert "applied" in note.lower()


def test_duplicate_note_reflects_non_applied_status():
    entry = _make_entry("e2", jd_text="x", company="Acme", title="Role",
                         applied=False, apply_status="idle", created_at="2026-08-12T00:00:00")
    note = server._duplicate_note(entry)
    assert "Acme" in note
    assert "idle" in note
