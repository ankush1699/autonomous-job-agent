"""
tests/test_scrape_progress.py

Regression test for a real gap: a scrape cycle can run 20-30+ minutes with
zero visibility — the web UI just showed "running" for the whole duration,
indistinguishable from actually being stuck. run_scrape_cycle() now accepts
an on_progress(**fields) callback invoked at each phase boundary and after
every job is scored, which server.py uses to drive a real progress bar.

Mocks every scraper dependency (network calls, Google Sheets, Telegram) so
this runs with no external calls — verifies only the on_progress contract:
phase transitions happen in order and job counts are correct.

Run: pytest tests/test_scrape_progress.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import scheduler


def _job(i):
    return {
        "title": f"SWE {i}", "company": f"Co{i}", "apply_link": f"http://x/{i}",
        "description": "x" * 300,
    }


def test_on_progress_reports_phases_in_order_and_final_counts():
    raw_jobs = [_job(i) for i in range(3)]
    calls = []

    def on_progress(**fields):
        calls.append(fields)

    def fake_score(job, profile):
        job["score"] = 50  # below default threshold — no notify path exercised

    with patch.object(scheduler, "fetch_jobs", return_value=raw_jobs), \
         patch.object(scheduler, "filter_jobs", return_value=(raw_jobs, [])), \
         patch.object(scheduler, "open_sheet_session", return_value=None), \
         patch.object(scheduler, "load_profile_cache", return_value="profile"), \
         patch.object(scheduler, "score_single_job", side_effect=fake_score), \
         patch.object(scheduler, "lookup_sponsorship", return_value="neutral"), \
         patch.object(scheduler, "mark_seen"), \
         patch.object(scheduler, "save_job_to_cache"), \
         patch.object(scheduler, "send_notification"), \
         patch("time.sleep"):
        scheduler.run_scrape_cycle(on_progress=on_progress)

    phases = [c["phase"] for c in calls]
    assert phases[0] == "scraping"
    assert "filtering" in phases
    assert phases.count("scoring") >= 3  # once at start + once per job
    assert phases[-1] == "done"

    final = calls[-1]
    assert final["jobs_total"] == 3
    assert final["jobs_scored"] == 3
    assert final["high_match_count"] == 0


def test_on_progress_reports_done_with_zero_when_nothing_passes_filter():
    calls = []

    with patch.object(scheduler, "fetch_jobs", return_value=[_job(0)]), \
         patch.object(scheduler, "filter_jobs", return_value=([], [_job(0)])):
        scheduler.run_scrape_cycle(on_progress=lambda **f: calls.append(f))

    assert calls[-1]["phase"] == "done"
    assert calls[-1]["jobs_total"] == 0


def test_defaults_to_noop_when_on_progress_omitted():
    """Standalone/CLI/daemon callers that don't pass on_progress must be
    unaffected — this is what makes the parameter backward compatible."""
    with patch.object(scheduler, "fetch_jobs", return_value=[]), \
         patch.object(scheduler, "filter_jobs", return_value=([], [])):
        scheduler.run_scrape_cycle()  # must not raise
