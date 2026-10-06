"""
tests/test_score_rubric_version.py

scraper/score.py keeps its own URL-keyed 48h score cache
(scored_jobs_cache.json) that sits IN FRONT of core.jd_cache. It has to
honor RUBRIC_VERSION the same way core.should_apply's cache read does,
otherwise a verdict scored under an older rubric within the last 48h is
served straight from this layer with no validation — and it must propagate
rubric_version onto the job dict, or every freshly-scraped entry shows the
"scored under an older rubric" badge in the UI.

Run: pytest tests/test_score_rubric_version.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import score as score_mod
from core.should_apply import RUBRIC_VERSION


def _job():
    return {"company": "Acme", "title": "SWE", "apply_link": "https://x/1", "description": "d" * 300}


def _verdict(version=RUBRIC_VERSION, score=88):
    return {
        "rubric_version": version, "score": score, "reasoning": "r", "jd_summary": "s",
        "seniority_level": "mid", "role_type": "fullstack-swe",
        "sub_scores": {"tech": 40, "seniority": 20, "role_type": 15, "sponsorship_bonus": 13},
        "tailoring_recommended": False, "tailoring_reasoning": "t",
    }


def test_fresh_score_propagates_rubric_version_and_caches_it():
    with patch.object(score_mod, "get_cached_score", return_value=None), \
         patch.object(score_mod, "cache_score") as mock_cache, \
         patch.object(score_mod, "score_jd", return_value=_verdict()), \
         patch.object(score_mod, "load_profile_cache", return_value="profile"):
        job = _job()
        score_mod.score_single_job(job)
    assert job["rubric_version"] == RUBRIC_VERSION
    assert job["score"] == 88
    saved = mock_cache.call_args.args[1]
    assert saved["rubric_version"] == RUBRIC_VERSION


def test_current_version_cache_hit_is_used_without_llm():
    with patch.object(score_mod, "get_cached_score", return_value=_verdict(score=77)), \
         patch.object(score_mod, "cache_score"), \
         patch.object(score_mod, "score_jd") as mock_llm, \
         patch.object(score_mod, "load_profile_cache", return_value="profile"):
        job = _job()
        score_mod.score_single_job(job)
    mock_llm.assert_not_called()
    assert job["score"] == 77
    assert job["rubric_version"] == RUBRIC_VERSION


def test_stale_version_cache_hit_is_treated_as_miss():
    """The second-cache poisoning path: a v1 score cached in the last 48h
    must NOT be served — it has to go back to the LLM under the current rubric."""
    with patch.object(score_mod, "get_cached_score", return_value=_verdict(version=RUBRIC_VERSION - 1, score=30)), \
         patch.object(score_mod, "cache_score"), \
         patch.object(score_mod, "score_jd", return_value=_verdict(score=88)) as mock_llm, \
         patch.object(score_mod, "load_profile_cache", return_value="profile"):
        job = _job()
        score_mod.score_single_job(job)
    mock_llm.assert_called_once()
    assert job["score"] == 88
    assert job["rubric_version"] == RUBRIC_VERSION


def test_unversioned_cache_hit_is_treated_as_miss():
    old = _verdict(score=30); del old["rubric_version"]
    with patch.object(score_mod, "get_cached_score", return_value=old), \
         patch.object(score_mod, "cache_score"), \
         patch.object(score_mod, "score_jd", return_value=_verdict(score=88)) as mock_llm, \
         patch.object(score_mod, "load_profile_cache", return_value="profile"):
        job = _job()
        score_mod.score_single_job(job)
    mock_llm.assert_called_once()
    assert job["score"] == 88


def test_scoring_error_leaves_rubric_version_unset():
    with patch.object(score_mod, "get_cached_score", return_value=None), \
         patch.object(score_mod, "score_jd", side_effect=RuntimeError("boom")), \
         patch.object(score_mod, "load_profile_cache", return_value="profile"):
        job = _job()
        score_mod.score_single_job(job)
    assert job["score"] == 0
    assert job["rubric_version"] is None
