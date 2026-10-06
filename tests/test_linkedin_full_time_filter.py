"""
tests/test_linkedin_full_time_filter.py

LinkedIn searches are scoped to full-time postings only (job_type="fulltime"
passed to jobspy, which maps it to JobType.FULL_TIME and sends LinkedIn's own
f_JT query param — same effect as clicking "Full-time" in LinkedIn's own
search UI). Fixed on for LinkedIn specifically, per explicit user choice
(Sep 2026) — not a setting, and Indeed must be unaffected.

Run: pytest tests/test_linkedin_full_time_filter.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd
from scraper import scrape


def test_linkedin_search_requests_full_time_only():
    with patch.object(scrape, "scrape_jobs", return_value=pd.DataFrame()) as mock_scrape:
        scrape._fetch_jobspy_platform(
            "linkedin", "Software Engineer", "United States", 20, False, set(),
        )
    assert mock_scrape.call_args.kwargs["job_type"] == "fulltime"


def test_indeed_search_is_not_scoped_to_full_time():
    with patch.object(scrape, "scrape_jobs", return_value=pd.DataFrame()) as mock_scrape:
        scrape._fetch_jobspy_platform(
            "indeed", "Software Engineer", "United States", 20, False, set(),
        )
    assert "job_type" not in mock_scrape.call_args.kwargs
