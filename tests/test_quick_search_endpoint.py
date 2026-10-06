"""
tests/test_quick_search_endpoint.py

POST /api/quick-search: request validation and the overrides dict it builds
for the background scrape thread. Threading itself isn't exercised (mocked
out), matching this file's untested-elsewhere precedent for /api/scrape.

Run: pytest tests/test_quick_search_endpoint.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi import HTTPException

import server


def test_top_n_out_of_range_is_rejected():
    with patch.object(server.threading, "Thread") as mock_thread:
        with pytest.raises(HTTPException) as exc_info:
            server.trigger_quick_search(server.QuickSearchRequest(top_n=0))
        assert exc_info.value.status_code == 422
        with pytest.raises(HTTPException):
            server.trigger_quick_search(server.QuickSearchRequest(top_n=51))
    mock_thread.assert_not_called()


def test_default_top_n_is_ten():
    assert server.QuickSearchRequest().top_n == 10


def test_valid_request_builds_correct_overrides_and_starts_a_job():
    with patch.object(server.threading, "Thread") as mock_thread:
        result = server.trigger_quick_search(server.QuickSearchRequest(
            location="Chicago, IL", hours_old=24, keywords="Backend Engineer", top_n=5,
        ))
    assert result["status"] == "running"
    assert "job_id" in result
    assert server.SCRAPE_RUNS[result["job_id"]]["quick_search"] is True

    call_args, call_kwargs = mock_thread.call_args
    _, overrides = call_kwargs["args"]
    assert overrides == {
        "location": "Chicago, IL", "hours_old": 24,
        "keywords": "Backend Engineer", "daily_cap": 5,
    }


def test_all_fields_optional_omitted_fields_pass_through_as_none():
    with patch.object(server.threading, "Thread") as mock_thread:
        server.trigger_quick_search(server.QuickSearchRequest())
    _, overrides = mock_thread.call_args.kwargs["args"]
    assert overrides == {"location": None, "hours_old": None, "keywords": None, "daily_cap": 10}
