"""
tests/test_quick_search_overrides.py

Quick Search (web UI, Scraper tab): a one-off location/recency/title-scoped
run that must NOT touch the user's saved scraper_settings.json, unlike the
regular "Run" button which persists the on-screen form before firing.
Implemented as an `overrides` dict threaded through
scraper/scheduler.py::run_scrape_cycle() -> modes/scraper.py::run_once() ->
server.py::_run_scrape_cycle()/trigger_quick_search(), applied in-memory only.

Run: pytest tests/test_quick_search_overrides.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import scheduler


def _job(i, score=90):
    return {"title": f"SWE {i}", "company": f"Co{i}", "apply_link": f"http://x/{i}",
            "description": "x" * 300, "platform": "linkedin"}


def _run_with_overrides(overrides, scores=(90,)):
    raw_jobs = [_job(i) for i in range(len(scores))]
    score_iter = iter(scores)

    def fake_score(job, profile):
        job["score"] = next(score_iter)
        job["sponsorship_signal"] = "neutral"

    with patch.object(scheduler, "fetch_jobs", return_value=raw_jobs) as mock_fetch, \
         patch.object(scheduler, "filter_jobs", return_value=(raw_jobs, [])), \
         patch.object(scheduler, "open_sheet_session", return_value=None), \
         patch.object(scheduler, "load_profile_cache", return_value="profile"), \
         patch.object(scheduler, "score_single_job", side_effect=fake_score), \
         patch.object(scheduler, "lookup_sponsorship", return_value="neutral"), \
         patch.object(scheduler.scraper_settings, "load", return_value={
             **scheduler.scraper_settings._env_defaults(), "location": "United States",
             "hours_old": 96, "daily_cap": 25,
         }), \
         patch.object(scheduler.scraper_settings, "save") as mock_save, \
         patch.object(scheduler, "mark_seen"), patch.object(scheduler, "save_job_to_cache"), \
         patch.object(scheduler, "send_notification"), patch("time.sleep"):
        kept = []
        scheduler.run_scrape_cycle(on_new_entry=kept.append, overrides=overrides)
    return kept, mock_fetch, mock_save


def test_overrides_never_write_to_saved_settings():
    _, _, mock_save = _run_with_overrides({"location": "Chicago, IL", "hours_old": 24, "daily_cap": 10})
    mock_save.assert_not_called()


def test_location_and_hours_old_overrides_reach_fetch_jobs():
    _, mock_fetch, _ = _run_with_overrides({"location": "Chicago, IL", "hours_old": 24})
    call_kwargs = mock_fetch.call_args.kwargs
    assert call_kwargs["location"] == "Chicago, IL"
    assert call_kwargs["hours_old"] == 24


def test_keywords_override_applies_to_every_platform_and_ats():
    _, mock_fetch, _ = _run_with_overrides({"keywords": "Backend Engineer"})
    call_kwargs = mock_fetch.call_args.kwargs
    assert call_kwargs["ats_keywords"] == "Backend Engineer"
    for platform in ("linkedin", "indeed", "dice", "google_jobs"):
        assert call_kwargs["platform_keywords"][platform] == "Backend Engineer"
    # Depth is the quick-search constant, not whatever the user's saved
    # per-platform counts happen to be (which could be much higher/lower).
    assert call_kwargs["platform_counts"]["indeed"] == scheduler._QUICK_SEARCH_RESULTS_PER_PLATFORM


def test_daily_cap_override_gives_top_n_aligned_ranking():
    # Same waitlist behavior as a normal cycle (test_scrape_daily_cap.py):
    # cap-missed candidates are still written, flagged cap_missed=True, not
    # dropped — "top N" means the top_n/daily_cap override controls how many
    # land as real (non-waitlisted) matches, using the exact same ranking.
    scores = [95, 90, 85, 80, 76]
    kept, _, _ = _run_with_overrides({"daily_cap": 3}, scores=scores)
    written = [j for j in kept if not j.get("cap_missed")]
    waitlisted = [j for j in kept if j.get("cap_missed")]
    assert sorted(j["score"] for j in written) == [85, 90, 95]
    assert sorted(j["score"] for j in waitlisted) == [76, 80]


def test_no_overrides_behaves_exactly_as_a_normal_cycle():
    kept, mock_fetch, mock_save = _run_with_overrides({})
    call_kwargs = mock_fetch.call_args.kwargs
    assert call_kwargs["location"] == "United States"
    assert call_kwargs["hours_old"] == 96
    mock_save.assert_not_called()  # run_scrape_cycle itself never saves, override or not


def test_omitted_overrides_param_defaults_to_no_op():
    # Calling without `overrides` at all (every existing caller) must behave
    # identically to passing an empty dict.
    with patch.object(scheduler, "fetch_jobs", return_value=[]) as mock_fetch, \
         patch.object(scheduler, "filter_jobs", return_value=([], [])), \
         patch.object(scheduler.scraper_settings, "load", return_value={
             **scheduler.scraper_settings._env_defaults(), "location": "United States", "hours_old": 96,
         }):
        scheduler.run_scrape_cycle(on_new_entry=lambda j: None)
    assert mock_fetch.call_args.kwargs["location"] == "United States"
