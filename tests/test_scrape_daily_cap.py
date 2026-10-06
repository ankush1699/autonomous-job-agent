"""
tests/test_scrape_daily_cap.py

Covers the "top N best, most likely to hear back" behavior added to
scraper/scheduler.py::run_scrape_cycle():

1. _rank_key() — ATS-sourced (direct company application) and confirmed-H1B
   jobs win ties against equally-scored jobs without those signals.
2. Candidates beyond daily_cap are STILL marked seen and cached — the LLM
   cost is already spent, so a job that misses the cut today must not be
   re-scraped and re-billed tomorrow. In entries mode (on_new_entry given)
   they are written too, flagged cap_missed=True — a hidden waitlist, not a
   discard (user decision, Sep 2026); sheet mode still drops them.
3. Jobs below MIN_SCORE never become candidates at all, cap or no cap.

Run: pytest tests/test_scrape_daily_cap.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import scheduler


def test_rank_key_prefers_ats_and_sponsorship_on_ties():
    plain = {"score": 80, "platform": "linkedin", "sponsorship_signal": "neutral"}
    ats = {"score": 80, "platform": "greenhouse", "sponsorship_signal": "neutral"}
    sponsored = {"score": 80, "platform": "linkedin", "sponsorship_signal": "confirmed_h1b"}
    ats_and_sponsored = {"score": 80, "platform": "lever", "sponsorship_signal": "confirmed_h1b"}

    ranked = sorted([plain, ats, sponsored, ats_and_sponsored], key=scheduler._rank_key, reverse=True)
    assert ranked[0] is ats_and_sponsored
    assert ranked[-1] is plain


def test_rank_key_score_dominates_over_tie_break_signals():
    high_score_plain = {"score": 90, "platform": "linkedin", "sponsorship_signal": "neutral"}
    low_score_ats_sponsored = {"score": 76, "platform": "greenhouse", "sponsorship_signal": "confirmed_h1b"}
    ranked = sorted([high_score_plain, low_score_ats_sponsored], key=scheduler._rank_key, reverse=True)
    assert ranked[0] is high_score_plain  # score always wins over tie-break bonuses


def _job(i, score, platform="linkedin"):
    return {
        "title": f"SWE {i}", "company": f"Co{i}", "apply_link": f"http://x/{i}",
        "description": "x" * 300, "platform": platform,
    }


def _run_cycle_with_scores(scores, daily_cap=25, on_new_entry=None):
    raw_jobs = [_job(i, s) for i, s in enumerate(scores)]
    score_iter = iter(scores)

    def fake_score(job, profile):
        job["score"] = next(score_iter)
        job["sponsorship_signal"] = "neutral"

    with patch.object(scheduler, "fetch_jobs", return_value=raw_jobs), \
         patch.object(scheduler, "filter_jobs", return_value=(raw_jobs, [])), \
         patch.object(scheduler, "open_sheet_session", return_value=None), \
         patch.object(scheduler, "load_profile_cache", return_value="profile"), \
         patch.object(scheduler, "score_single_job", side_effect=fake_score), \
         patch.object(scheduler, "lookup_sponsorship", return_value="neutral"), \
         patch.object(scheduler.scraper_settings, "load", return_value={
             **scheduler.scraper_settings._env_defaults(), "daily_cap": daily_cap,
         }), \
         patch.object(scheduler, "mark_seen") as mock_mark_seen, \
         patch.object(scheduler, "save_job_to_cache") as mock_cache, \
         patch.object(scheduler, "send_notification"), \
         patch("time.sleep"):
        kept = []
        scheduler.run_scrape_cycle(on_new_entry=on_new_entry or kept.append)
    return kept, mock_mark_seen, mock_cache


def test_only_top_daily_cap_candidates_are_written():
    scores = [90, 85, 80, 76, 76]  # all >= 75, 5 candidates, cap at 3
    kept, mock_mark_seen, mock_cache = _run_cycle_with_scores(scores, daily_cap=3)

    written = [j for j in kept if not j.get("cap_missed")]
    waitlisted = [j for j in kept if j.get("cap_missed")]
    assert sorted(j["score"] for j in written) == [80, 85, 90]  # top 3 by score
    assert sorted(j["score"] for j in waitlisted) == [76, 76]   # the rest survive as a waitlist
    assert len(kept) == 5


def test_sheet_mode_still_discards_cap_missed_candidates():
    scores = [90, 85, 80, 76, 76]
    sheet = type("S", (), {"written": [], "write_job": lambda self, j: self.written.append(j), "sort": lambda self: None})()
    raw_jobs = [_job(i, s) for i, s in enumerate(scores)]
    score_iter = iter(scores)

    def fake_score(job, profile):
        job["score"] = next(score_iter); job["sponsorship_signal"] = "neutral"

    with patch.object(scheduler, "fetch_jobs", return_value=raw_jobs), \
         patch.object(scheduler, "filter_jobs", return_value=(raw_jobs, [])), \
         patch.object(scheduler, "open_sheet_session", return_value=sheet), \
         patch.object(scheduler, "load_profile_cache", return_value="profile"), \
         patch.object(scheduler, "score_single_job", side_effect=fake_score), \
         patch.object(scheduler, "lookup_sponsorship", return_value="neutral"), \
         patch.object(scheduler.scraper_settings, "load", return_value={
             **scheduler.scraper_settings._env_defaults(), "daily_cap": 3}), \
         patch.object(scheduler, "mark_seen"), patch.object(scheduler, "save_job_to_cache"), \
         patch.object(scheduler, "send_notification"), patch("time.sleep"):
        scheduler.run_scrape_cycle(on_new_entry=None)
    assert len(sheet.written) == 3
    assert not any(j.get("cap_missed") for j in sheet.written)


def test_discarded_candidates_still_marked_seen_and_cached():
    scores = [90, 85, 80, 76, 76]
    kept, mock_mark_seen, mock_cache = _run_cycle_with_scores(scores, daily_cap=3)

    # All 5 scored jobs (kept + discarded) must be marked seen and cached —
    # not just the 3 that made the cut — so the 2 discarded ones never get
    # re-scraped and re-billed tomorrow.
    assert mock_mark_seen.call_count == 5
    assert mock_cache.call_count == 5


def test_below_threshold_jobs_never_become_candidates_regardless_of_cap():
    scores = [90, 40, 30]  # only 1 passes MIN_SCORE (75 default)
    kept, mock_mark_seen, mock_cache = _run_cycle_with_scores(scores, daily_cap=25)

    assert len(kept) == 1
    assert kept[0]["score"] == 90
    # still marked seen/cached even though below threshold
    assert mock_mark_seen.call_count == 3
    assert mock_cache.call_count == 3


def test_fewer_candidates_than_cap_keeps_all_of_them():
    scores = [90, 85]
    kept, _, _ = _run_cycle_with_scores(scores, daily_cap=25)
    assert len(kept) == 2
