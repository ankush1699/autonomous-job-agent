"""
tests/test_cancel_generate.py

Covers the "Stop" button's backend: server.cancel_generate() and
_generate_entry()'s honoring of a cancellation that happened while it was
running. The pipeline call itself can't be force-killed mid-flight (it's one
opaque synchronous LangGraph invocation with real LLM calls), so "stop"
means: flip apply_status away from "running" immediately, and make sure the
background thread — when it eventually finishes — doesn't clobber that with
a late "completed" or "error".

Run: pytest tests/test_cancel_generate.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("APP_PASSWORD", "test-only")

import server  # noqa: E402


def test_cancel_generate_flips_status_and_saves():
    server.ENTRIES["e1"] = {"id": "e1", "company": "Co", "title": "SWE", "apply_status": "running"}
    with patch("core.entries_store.save") as mock_save:
        result = server.cancel_generate("e1")

    assert result["apply_status"] == "cancelled"
    assert "Cancelled" in result["apply_error"]
    mock_save.assert_called_once_with(server.ENTRIES)


def test_cancel_generate_404s_on_unknown_entry():
    try:
        server.cancel_generate("not-a-real-id")
        assert False, "expected HTTPException"
    except server.HTTPException as e:
        assert e.status_code == 404


def test_cancel_generate_409s_when_nothing_running():
    server.ENTRIES["e2"] = {"id": "e2", "company": "Co", "title": "SWE", "apply_status": "idle"}
    try:
        server.cancel_generate("e2")
        assert False, "expected HTTPException"
    except server.HTTPException as e:
        assert e.status_code == 409


def test_generate_entry_discards_success_result_after_cancellation():
    """If the entry was cancelled while the (unkillable) background thread
    was still running, a LATE successful result must not overwrite the
    'cancelled' status the user already saw and moved on from."""
    server.ENTRIES["e3"] = {"id": "e3", "company": "Co", "title": "SWE", "jd": "x", "apply_status": "cancelled"}
    with patch("modes.manual.run", return_value={"output_folder": "/tmp/x", "proceed": True}), \
         patch("core.entries_store.save"):
        server._generate_entry("e3", False)

    assert server.ENTRIES["e3"]["apply_status"] == "cancelled"  # untouched


def test_generate_entry_discards_error_result_after_cancellation():
    server.ENTRIES["e4"] = {"id": "e4", "company": "Co", "title": "SWE", "apply_status": "cancelled"}
    with patch("modes.manual.run", side_effect=RuntimeError("boom")), \
         patch("core.entries_store.save"):
        server._generate_entry("e4", False)

    assert server.ENTRIES["e4"]["apply_status"] == "cancelled"  # untouched, not "error"


def test_generate_entry_completes_normally_when_not_cancelled():
    server.ENTRIES["e5"] = {"id": "e5", "company": "Co", "title": "SWE", "jd": "x", "apply_status": "running"}
    with patch("modes.manual.run", return_value={"output_folder": "", "proceed": True}), \
         patch("core.entries_store.save"):
        server._generate_entry("e5", False)

    assert server.ENTRIES["e5"]["apply_status"] == "completed"
