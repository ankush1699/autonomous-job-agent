"""
tests/test_ats_recency_filter.py

Covers scraper/ats_scrape.py's recency filtering — a real gap this fixes:
ATS boards (Greenhouse/Lever/Ashby/Workday) return their ENTIRE current
posting list with no server-side "only recent" param (unlike LinkedIn's
hours_old search param), so adding a new company used to score its whole
historical backlog in one shot (a real example: 171 keyword-matching
postings from one company on the very first cycle). hours_old is now
applied client-side per job, reusing the same setting LinkedIn already has.

Run: pytest tests/test_ats_recency_filter.py -v
"""
import datetime
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import ats_scrape


def _iso_hours_ago(hours: float) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return dt.isoformat().replace("+00:00", "Z")


def test_hours_since_iso_parses_recent_and_old_timestamps():
    recent = _iso_hours_ago(2)
    old = _iso_hours_ago(200)
    assert ats_scrape._hours_since_iso(recent) < 3
    assert ats_scrape._hours_since_iso(old) > 190


def test_hours_since_iso_none_for_missing_or_malformed():
    assert ats_scrape._hours_since_iso("") is None
    assert ats_scrape._hours_since_iso("not a date") is None


def test_hours_since_epoch_ms():
    two_hours_ago_ms = int((datetime.datetime.now(datetime.timezone.utc)
                             - datetime.timedelta(hours=2)).timestamp() * 1000)
    hours = ats_scrape._hours_since_epoch_ms(two_hours_ago_ms)
    assert 1.5 < hours < 2.5


def test_hours_since_epoch_ms_none_for_missing():
    assert ats_scrape._hours_since_epoch_ms(None) is None
    assert ats_scrape._hours_since_epoch_ms(0) is None


def test_hours_since_workday_relative_phrases():
    assert ats_scrape._hours_since_workday_relative("Posted Today") == 0.0
    assert ats_scrape._hours_since_workday_relative("Posted Yesterday") == 24.0
    assert ats_scrape._hours_since_workday_relative("Posted 3 Days Ago") == 72.0
    assert ats_scrape._hours_since_workday_relative("Posted 30+ Days Ago") == 720.0


def test_hours_since_workday_relative_unrecognized_returns_none():
    """An unparseable/unexpected Workday date phrase must not be treated as
    infinitely old — that would silently drop real postings."""
    assert ats_scrape._hours_since_workday_relative("") is None
    assert ats_scrape._hours_since_workday_relative("Some new format Workday ships later") is None


def test_passes_recency_no_filter_keeps_everything():
    assert ats_scrape._passes_recency(500, None) is True
    assert ats_scrape._passes_recency(None, None) is True


def test_passes_recency_filters_old_keeps_recent():
    assert ats_scrape._passes_recency(10, 32) is True
    assert ats_scrape._passes_recency(50, 32) is False


def test_passes_recency_unparseable_date_kept_not_dropped():
    """An unrecognized date format keeps the job rather than risk silently
    losing a real posting because we couldn't parse its timestamp."""
    assert ats_scrape._passes_recency(None, 32) is True


def test_fetch_greenhouse_filters_old_postings_with_hours_old():
    now_posting = {"title": "Software Engineer", "absolute_url": "https://x.com/1",
                    "updated_at": _iso_hours_ago(2), "location": {"name": "Remote"}, "content": "..."}
    old_posting = {"title": "Software Engineer II", "absolute_url": "https://x.com/2",
                   "updated_at": _iso_hours_ago(500), "location": {"name": "Remote"}, "content": "..."}
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"jobs": [now_posting, old_posting]}
    mock_resp.raise_for_status = MagicMock()

    with patch.object(ats_scrape.requests, "get", return_value=mock_resp):
        jobs = ats_scrape._fetch_greenhouse("Acme", "acme", ["Software Engineer"], hours_old=32)

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Software Engineer"


def test_fetch_greenhouse_no_hours_old_keeps_everything():
    now_posting = {"title": "Software Engineer", "absolute_url": "https://x.com/1",
                    "updated_at": _iso_hours_ago(2), "location": {"name": "Remote"}, "content": "..."}
    old_posting = {"title": "Software Engineer II", "absolute_url": "https://x.com/2",
                   "updated_at": _iso_hours_ago(500), "location": {"name": "Remote"}, "content": "..."}
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"jobs": [now_posting, old_posting]}
    mock_resp.raise_for_status = MagicMock()

    with patch.object(ats_scrape.requests, "get", return_value=mock_resp):
        jobs = ats_scrape._fetch_greenhouse("Acme", "acme", ["Software Engineer"])

    assert len(jobs) == 2  # backward compatible: hours_old=None means no filtering
