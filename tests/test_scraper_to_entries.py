"""
tests/test_scraper_to_entries.py

Regression test for the scraper → Applications tab change: scored jobs used
to go to Google Sheets exclusively; the user asked for them to show up in
the same Applications tab as manually-added JDs instead. Covers two things:

1. scraper/scheduler.py: when on_new_entry is provided, it REPLACES the
   sheet write entirely — no Sheets session should even be opened.
2. server.py: _new_entry_from_scraped_job() builds the right entry shape
   from an already-scored job dict (apply_link, source, proceed threshold).

Run: pytest tests/test_scraper_to_entries.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import scheduler


def _job(i, score=80):
    return {
        "title": f"SWE {i}", "company": f"Co{i}", "apply_link": f"http://x/{i}",
        "description": "x" * 300, "score": score, "reasoning": "Good fit.",
    }


def test_on_new_entry_skips_sheet_session_entirely():
    raw_jobs = [_job(0)]
    entries_created = []

    def fake_score(job, profile):
        job["score"] = 80  # already set by _job, but mirrors real score_single_job mutation

    with patch.object(scheduler, "fetch_jobs", return_value=raw_jobs), \
         patch.object(scheduler, "filter_jobs", return_value=(raw_jobs, [])), \
         patch.object(scheduler, "open_sheet_session") as mock_open_sheet, \
         patch.object(scheduler, "load_profile_cache", return_value="profile"), \
         patch.object(scheduler, "score_single_job", side_effect=fake_score), \
         patch.object(scheduler, "lookup_sponsorship", return_value="neutral"), \
         patch.object(scheduler, "mark_seen"), \
         patch.object(scheduler, "save_job_to_cache"), \
         patch.object(scheduler, "send_notification"), \
         patch("time.sleep"):
        scheduler.run_scrape_cycle(on_new_entry=entries_created.append)

    mock_open_sheet.assert_not_called()
    assert len(entries_created) == 1
    assert entries_created[0]["company"] == "Co0"


def test_without_on_new_entry_falls_back_to_sheet():
    """Standalone/daemon use (no on_new_entry) must keep writing to the
    sheet — this is what makes the change backward compatible."""
    raw_jobs = [_job(0)]

    class _FakeSheet:
        def __init__(self):
            self.written = []
        def write_job(self, job):
            self.written.append(job)
        def sort(self):
            pass

    fake_sheet = _FakeSheet()

    with patch.object(scheduler, "fetch_jobs", return_value=raw_jobs), \
         patch.object(scheduler, "filter_jobs", return_value=(raw_jobs, [])), \
         patch.object(scheduler, "open_sheet_session", return_value=fake_sheet), \
         patch.object(scheduler, "load_profile_cache", return_value="profile"), \
         patch.object(scheduler, "score_single_job"), \
         patch.object(scheduler, "lookup_sponsorship", return_value="neutral"), \
         patch.object(scheduler, "mark_seen"), \
         patch.object(scheduler, "save_job_to_cache"), \
         patch.object(scheduler, "send_notification"), \
         patch("time.sleep"):
        scheduler.run_scrape_cycle()  # no on_new_entry

    assert len(fake_sheet.written) == 1


def test_new_entry_from_scraped_job_shape():
    os.environ.setdefault("APP_PASSWORD", "test-only")
    import server

    entry = server._new_entry_from_scraped_job(_job(0, score=80))
    assert entry["company"] == "Co0"
    assert entry["title"] == "SWE 0"
    assert entry["apply_link"] == "http://x/0"
    assert entry["source"] == "scraper"
    assert entry["score_status"] == "scored"
    assert entry["score"] == 80
    assert entry["proceed"] is True  # 80 >= should_apply's MIN_SCORE (75)
    assert entry["applied"] is False
    assert entry["applied_at"] is None

    below = server._new_entry_from_scraped_job(_job(1, score=40))
    assert below["proceed"] is False
