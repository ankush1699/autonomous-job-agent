"""
tests/test_run_history.py

Each scrape run is recorded as a funnel (found -> passed filters -> scored ->
passed the bar -> kept/waitlisted) for the Scraper tab's "Last runs" list.
OUTPUT_BASE_PATH is isolated per test by conftest.py.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import run_history as rh
from scraper import scheduler


def test_source_key_separates_company_pages_and_boards():
    assert rh.source_key({"platform": "linkedin"}) == "linkedin"
    assert rh.source_key({"platform": "linkedin", "via": "linkedin_company_page"}) == "linkedin_company_pages"
    assert rh.source_key({"platform": "greenhouse"}) == "company_boards"


def test_company_found_credits_watched_companies_by_name():
    jobs = [{"platform": "linkedin", "via": "linkedin_company_page", "company": "Amazon Web Services (AWS)"},
            {"platform": "greenhouse", "company": "Stripe"},
            {"platform": "linkedin", "company": "Amazon"}]          # broad search: not credited
    assert rh.company_found(jobs, ["Amazon", "Stripe", "Meta"]) == {"Amazon": 1, "Stripe": 1, "Meta": 0}


def test_finish_prices_only_real_model_calls_and_keeps_the_last_runs():
    for i in range(rh.MAX_RUNS + 3):
        rec = rh.new_record(kind="manual")
        rec["scored"].update(total=10, model_calls=4, from_cache=6)
        rh.finish(rec)
    runs = rh.load()
    assert len(runs) == rh.MAX_RUNS
    assert runs[-1]["est_cost_usd"] == round(4 * rh.COST_PER_LLM_SCORE_USD, 3)
    assert runs[-1]["status"] == "completed"


def _cycle(settings_overrides, raw_jobs, rejected=(), stages=("llm",), scores=(90,)):
    stage_iter, score_iter = iter(stages), iter(scores)

    def fake_score(job, profile):
        job["score"] = next(score_iter)
        job["score_stage"] = next(stage_iter)

    settings = {**scheduler.scraper_settings._env_defaults(), **settings_overrides}
    with patch.object(scheduler, "fetch_jobs", return_value=raw_jobs) as mock_fetch, \
         patch.object(scheduler, "filter_jobs", return_value=(raw_jobs, list(rejected))), \
         patch.object(scheduler, "open_sheet_session", return_value=None), \
         patch.object(scheduler, "load_profile_cache", return_value="profile"), \
         patch.object(scheduler, "score_single_job", side_effect=fake_score), \
         patch.object(scheduler, "lookup_sponsorship", return_value="neutral"), \
         patch.object(scheduler.scraper_settings, "load", return_value=settings), \
         patch.object(scheduler, "mark_seen"), patch.object(scheduler, "save_job_to_cache"), \
         patch.object(scheduler, "send_notification"), patch("time.sleep"):
        kept = []
        scheduler.run_scrape_cycle(on_new_entry=kept.append, overrides={"run_kind": "manual"})
    return mock_fetch, kept


def _job(i, platform="linkedin"):
    return {"title": f"SWE {i}", "company": f"Co{i}", "apply_link": f"http://x/{i}",
            "description": "x" * 300, "platform": platform}


def test_a_run_is_recorded_as_a_funnel():
    jobs = [_job(0), _job(1, "dice"), _job(2, "greenhouse")]
    rejected = [{**_job(9), "reject_reason": "already_seen"}, {**_job(8), "reject_reason": "too_senior_title"}]
    _cycle({"min_score": 80, "daily_cap": 1}, jobs, rejected, stages=("llm", "url_cache", "llm"), scores=(95, 85, 60))
    run = rh.last()
    assert run["kind"] == "manual" and run["status"] == "completed"
    assert run["found"] == {"linkedin": 1, "dice": 1, "company_boards": 1}
    assert run["rejected"] == {"already_seen": 1, "too_senior_title": 1}
    assert run["scored"]["total"] == 3 and run["scored"]["model_calls"] == 2 and run["scored"]["from_cache"] == 1
    assert run["passed_bar"] == 2 and run["kept"] == 1 and run["waitlisted"] == 1
    assert run["est_cost_usd"] == round(2 * rh.COST_PER_LLM_SCORE_USD, 3)


def test_source_switches_reach_fetch_jobs():
    off = {**scheduler.scraper_settings._DEFAULT_SOURCES, "company_boards": False, "linkedin_company_pages": False}
    mock_fetch, _ = _cycle({"sources": off}, [_job(0)])
    kw = mock_fetch.call_args.kwargs
    assert kw["include_ats"] is False and kw["linkedin_company_ids"] is None


def test_simple_mode_depth_reaches_the_company_pass():
    mock_fetch, _ = _cycle({"mode": "simple", "depth": "deep"}, [_job(0)])
    assert mock_fetch.call_args.kwargs["linkedin_company_results"] == \
        scheduler.scraper_settings.DEPTH_PRESETS["deep"]["linkedin_company_pages"]["per_role"]
