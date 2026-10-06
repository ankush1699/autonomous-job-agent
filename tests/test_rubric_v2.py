"""
tests/test_rubric_v2.py

Covers the Sep 2026 scoring overhaul — every item here maps to a measured
failure against real cached verdicts (44% role_type/score mismatch, 34%
seniority mismatch, 67% sponsorship above its own rubric max, and a poisoned
cache entry that "Retry" could never clear):

  * Anthropic-first cheap tier (core/llm.py)
  * label/score consistency guard with tolerance sets, not strict equality
  * cache-READ validation + RUBRIC_VERSION invalidation (core/should_apply.py)
  * sponsorship as a pure signal; external H-1B evidence lifts it
  * scraper filter threading: Dice location/recency/remote, Google Jobs
    recency/remote, ATS location

Run: pytest tests/test_rubric_v2.py -v
"""
import os
import sys
import time
import calendar
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import llm as llm_mod
from core import should_apply as sa
from scraper import scrape, ats_scrape


# ---------------------------------------------------------------------------
# Provider order
# ---------------------------------------------------------------------------

def _clear_provider_cache():
    llm_mod.cheap_provider.cache_clear()


def test_cheap_tier_prefers_anthropic_when_key_present():
    _clear_provider_cache()
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x", "GROQ_API_KEY": "y", "LLM_CHEAP_PROVIDER": ""}), \
         patch.object(llm_mod, "_ollama_running", return_value=False):
        assert llm_mod.cheap_provider() == "anthropic"
    _clear_provider_cache()


def test_cheap_tier_falls_to_groq_without_anthropic_key():
    _clear_provider_cache()
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env.update({"GROQ_API_KEY": "y", "LLM_CHEAP_PROVIDER": ""})
    with patch.dict(os.environ, env, clear=True), \
         patch.object(llm_mod, "_ollama_running", return_value=False):
        assert llm_mod.cheap_provider() == "groq"
    _clear_provider_cache()


def test_next_provider_after_anthropic_is_groq():
    with patch.dict(os.environ, {"GROQ_API_KEY": "y"}), \
         patch.object(llm_mod, "_ollama_running", return_value=False):
        assert llm_mod.next_cheap_provider("anthropic") == "groq"
        assert llm_mod.next_cheap_provider("groq") is None  # ollama not running → nothing left


def test_explicit_override_still_wins():
    _clear_provider_cache()
    with patch.dict(os.environ, {"LLM_CHEAP_PROVIDER": "groq", "ANTHROPIC_API_KEY": "x"}):
        assert llm_mod.cheap_provider() == "groq"
    _clear_provider_cache()


# ---------------------------------------------------------------------------
# Tolerance guard — accepts genuinely adjacent tiers, still rejects the bugs
# ---------------------------------------------------------------------------

def test_guard_accepts_entry_to_mid_boundary():
    # "open to entry-level to mid-level" — either 25 or 20 is defensible
    sa._check_label_consistency("mid", 25, sa._SENIORITY_OK_SCORES, "seniority_level")
    sa._check_label_consistency("entry", 20, sa._SENIORITY_OK_SCORES, "seniority_level")
    sa._check_label_consistency("fullstack-swe", 13, sa._ROLE_TYPE_OK_SCORES, "role_type")


def test_guard_still_rejects_the_real_google_bug():
    # role_type="ai-engineer" scored 9 — the actual production failure
    try:
        sa._check_label_consistency("ai-engineer", 9, sa._ROLE_TYPE_OK_SCORES, "role_type")
        assert False, "expected _SubScoreInconsistent"
    except sa._SubScoreInconsistent:
        pass


def test_guard_still_rejects_the_real_right_click_bug():
    # role_type="fullstack-swe" scored 10; seniority_level="mid" scored 8
    for label, score, table in [("fullstack-swe", 10, sa._ROLE_TYPE_OK_SCORES),
                                ("mid", 8, sa._SENIORITY_OK_SCORES)]:
        try:
            sa._check_label_consistency(label, score, table, "x")
            assert False, f"expected rejection for {label}={score}"
        except sa._SubScoreInconsistent:
            pass


def test_guard_skips_unknown_labels():
    sa._check_label_consistency("something-new", 3, sa._ROLE_TYPE_OK_SCORES, "role_type")  # no raise


# ---------------------------------------------------------------------------
# Cache-read validation + RUBRIC_VERSION
# ---------------------------------------------------------------------------

def _good_v2_verdict():
    return {
        "rubric_version": sa.RUBRIC_VERSION, "score": 80, "proceed": True, "stage": "llm",
        "role_type": "fullstack-swe", "seniority_level": "mid",
        "sub_scores": {"tech": 35, "seniority": 20, "role_type": 15, "sponsorship_bonus": 7},
        "reasoning": "r", "jd_summary": "s", "red_flags": [],
        "tailoring_recommended": False, "tailoring_reasoning": "t",
    }


def test_verdict_is_consistent_true_for_good_v2():
    assert sa._verdict_is_consistent(_good_v2_verdict()) is True


def test_verdict_is_consistent_false_for_missing_version():
    v = _good_v2_verdict(); del v["rubric_version"]
    assert sa._verdict_is_consistent(v) is False


def test_verdict_is_consistent_false_for_old_version():
    v = _good_v2_verdict(); v["rubric_version"] = sa.RUBRIC_VERSION - 1
    assert sa._verdict_is_consistent(v) is False


def test_verdict_is_consistent_false_for_bad_subscores():
    v = _good_v2_verdict(); v["sub_scores"]["role_type"] = 9  # the Google bug shape
    assert sa._verdict_is_consistent(v) is False


def _evaluate_with_cache(cached_should_apply):
    """Run evaluate() with a controlled cache hit and a mocked LLM."""
    fresh = _good_v2_verdict(); fresh["score"] = 91
    fresh_result = {k: v for k, v in fresh.items() if k not in ("proceed", "stage", "red_flags")}
    with patch.object(sa.jd_cache, "get_entry", return_value={"should_apply": cached_should_apply}), \
         patch.object(sa.jd_cache, "update_entry") as mock_update, \
         patch.object(sa, "_llm_score", return_value=fresh_result) as mock_llm, \
         patch.object(sa, "find_red_flags", return_value=[]):
        verdict = sa.evaluate("some benign jd text", use_cache=True)
    return verdict, mock_llm, mock_update


def test_cache_hit_served_when_current_and_consistent():
    verdict, mock_llm, _ = _evaluate_with_cache(_good_v2_verdict())
    mock_llm.assert_not_called()
    assert verdict["stage"] == "cache"
    assert verdict["score"] == 80


def test_stale_rubric_cache_entry_is_rescored():
    """The poisoned-cache failure: an old verdict must not be served as truth."""
    old = _good_v2_verdict(); old["rubric_version"] = 1
    verdict, mock_llm, mock_update = _evaluate_with_cache(old)
    mock_llm.assert_called_once()
    assert verdict["stage"] == "llm"
    assert verdict["score"] == 91
    assert verdict["rubric_version"] == sa.RUBRIC_VERSION
    mock_update.assert_called_once()  # rewritten under the current rubric


def test_inconsistent_cache_entry_is_rescored():
    bad = _good_v2_verdict(); bad["sub_scores"]["seniority"] = 8  # mid paired with "unspecified" value
    verdict, mock_llm, _ = _evaluate_with_cache(bad)
    mock_llm.assert_called_once()
    assert verdict["score"] == 91


def test_red_flag_cache_entry_still_trusted_without_version():
    """Red-flag verdicts have no sub_scores and don't depend on the rubric."""
    cached = {"score": 0, "proceed": False, "stage": "red_flag", "red_flags": ["x"],
              "reasoning": "r", "jd_summary": "", "seniority_level": "", "role_type": "", "sub_scores": {}}
    verdict, mock_llm, _ = _evaluate_with_cache(cached)
    mock_llm.assert_not_called()
    assert verdict["stage"] == "cache"


def test_fresh_llm_verdict_carries_rubric_version_and_caches_it():
    verdict, _, mock_update = _evaluate_with_cache({})  # empty cache → miss
    assert verdict["rubric_version"] == sa.RUBRIC_VERSION
    saved = mock_update.call_args.kwargs["should_apply"]
    assert saved["rubric_version"] == sa.RUBRIC_VERSION


# ---------------------------------------------------------------------------
# Sponsorship as a pure signal
# ---------------------------------------------------------------------------

def test_external_confirmed_h1b_lifts_silent_jd_toward_explicit_value():
    fresh = _good_v2_verdict()
    fresh_result = {k: v for k, v in fresh.items() if k not in ("proceed", "stage", "red_flags")}
    fresh_result["sub_scores"]["sponsorship_bonus"] = 7  # JD silent on sponsorship
    fresh_result["score"] = 77
    with patch.object(sa.jd_cache, "get_entry", return_value=None), \
         patch.object(sa.jd_cache, "update_entry"), \
         patch.object(sa, "_llm_score", return_value=fresh_result), \
         patch.object(sa, "find_red_flags", return_value=[]):
        verdict = sa.evaluate("jd", use_cache=True, sponsorship_signal="confirmed_h1b")
    assert verdict["sub_scores"]["sponsorship_bonus"] == 13  # 7 + 6
    assert verdict["score"] == 83


def test_external_signal_never_exceeds_cap():
    fresh = _good_v2_verdict()
    fresh_result = {k: v for k, v in fresh.items() if k not in ("proceed", "stage", "red_flags")}
    fresh_result["sub_scores"]["sponsorship_bonus"] = 15
    with patch.object(sa.jd_cache, "get_entry", return_value=None), \
         patch.object(sa.jd_cache, "update_entry"), \
         patch.object(sa, "_llm_score", return_value=fresh_result), \
         patch.object(sa, "find_red_flags", return_value=[]):
        verdict = sa.evaluate("jd", use_cache=True, sponsorship_signal="confirmed_h1b")
    assert verdict["sub_scores"]["sponsorship_bonus"] == 15
    assert verdict["score"] == 80  # unchanged — nothing to add


# ---------------------------------------------------------------------------
# Scraper filter threading
# ---------------------------------------------------------------------------

def test_relative_age_parser():
    f = scrape._hours_from_relative
    assert f("2 hours ago") == 2
    assert f("3 days ago") == 72
    assert f("30+ days ago") == 720
    assert f("Just posted") == 0
    assert f("yesterday") == 24
    assert f("") is None
    assert f("some unknown phrase") is None


def test_dice_url_uses_configured_location_not_hardcoded_us():
    captured = {}

    def fake_parse(url, request_headers=None):
        captured["url"] = url
        return MagicMock(bozo=False, entries=[])

    with patch.object(scrape.feedparser, "parse", side_effect=fake_parse):
        scrape._fetch_dice_rss("Software Engineer", 5, set(), location="Chicago, IL")
    assert "-l-chicago%2C+il.rss" in captured["url"]


def _dice_entry(hours_ago: float, title="Software Engineer - Acme", location="Remote"):
    posted = time.gmtime(time.time() - hours_ago * 3600)
    return {"title": title, "link": f"https://dice.example/{hours_ago}", "summary": "x" * 300,
            "published_parsed": posted, "tags": [{"term": location}]}


def test_dice_recency_and_remote_filters_applied():
    entries = [_dice_entry(5), _dice_entry(200), _dice_entry(3, location="Austin, TX")]
    feed = MagicMock(bozo=False, entries=entries)
    with patch.object(scrape.feedparser, "parse", return_value=feed):
        recent = scrape._fetch_dice_rss("Software Engineer", 10, set(), hours_old=48)
        remote = scrape._fetch_dice_rss("Software Engineer", 10, set(), hours_old=48, remote_only=True)
    assert len(recent) == 2          # the 200h-old posting is dropped
    assert len(remote) == 1          # only the "Remote" one survives remote_only


def test_ats_location_filter_drops_explicit_non_us_keeps_ambiguous():
    ok = ats_scrape._location_ok
    assert ok("London, UK", "United States") is False
    assert ok("Tokyo, Japan", "United States") is False
    assert ok("San Francisco", "United States") is True
    assert ok("Remote", "United States") is True          # ambiguous → keep
    assert ok("", "United States") is True                # unknown → keep
    assert ok("Chicago, IL", "Chicago") is True
    assert ok("Austin, TX", "Chicago") is False


def test_ats_location_filter_wired_into_fetch():
    jobs = [{"title": "SWE", "location": "London, UK", "apply_link": "a"},
            {"title": "SWE", "location": "New York", "apply_link": "b"}]
    with patch.dict(ats_scrape._FETCHERS, {"ashby": lambda *a, **k: list(jobs)}), \
         patch("scraper.ats_companies.load", return_value=[{"company": "X", "ats": "ashby", "identifier": "x"}]):
        out = ats_scrape.fetch_ats_jobs("SWE", location="United States")
    assert [j["location"] for j in out] == ["New York"]
