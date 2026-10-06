"""
tests/test_fetch_jobs_per_platform.py

Covers scraper/scrape.py::fetch_jobs()'s per-platform keyword/count wiring,
and a real regression: Dice was fully implemented (_fetch_dice_rss existed
and worked) but was never actually CALLED from fetch_jobs() — it was
documented as an active source in the module docstring but silently did
nothing. This locks in that it's wired in now, and that each platform gets
its OWN keyword list/count rather than one shared global.

Mocks every network-touching helper so this runs with no real HTTP calls.

Run: pytest tests/test_fetch_jobs_per_platform.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import scrape


def test_dice_is_actually_called_with_its_own_keywords_and_count():
    """Regression test: Dice was documented as active but dead code before
    this fix — _fetch_dice_rss was defined but never invoked."""
    with patch.object(scrape, "_fetch_jobspy_platform", return_value=[]), \
         patch.object(scrape, "_fetch_dice_rss", return_value=[]) as mock_dice, \
         patch.object(scrape, "_fetch_google_jobs", return_value=[]), \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]):
        scrape.fetch_jobs(
            platform_keywords={"linkedin": "Software Engineer", "indeed": "Software Engineer",
                                "dice": "AI Engineer", "google_jobs": "Software Engineer"},
            platform_counts={"dice": 25},
        )

    mock_dice.assert_called_once()
    args, kwargs = mock_dice.call_args
    assert args[0] == "AI Engineer"  # dice's OWN keyword string, not linkedin's/indeed's
    assert args[1] == 25             # dice's OWN count


def test_each_platform_gets_its_own_keyword_string_and_count():
    calls = []

    def record_jobspy(platform, kw, location, count, remote_only, seen_links, hours_old=None):
        calls.append({"platform": platform, "kw": kw, "count": count})
        return []

    with patch.object(scrape, "_fetch_jobspy_platform", side_effect=record_jobspy), \
         patch.object(scrape, "_fetch_dice_rss", return_value=[]), \
         patch.object(scrape, "_fetch_google_jobs", return_value=[]) as mock_gj, \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]):
        scrape.fetch_jobs(
            platform_keywords={
                "linkedin": "Software Engineer,AI Engineer",
                "indeed": "Software Engineer,Full Stack Engineer,Backend Engineer",
                "dice": "Software Engineer",
                "google_jobs": "Software Engineer",
            },
            platform_counts={"linkedin": 15, "indeed": 20, "dice": 25, "google_jobs": 20},
        )

    linkedin_calls = [c for c in calls if c["platform"] == "linkedin"]
    indeed_calls = [c for c in calls if c["platform"] == "indeed"]
    assert len(linkedin_calls) == 2  # 2 keywords for linkedin
    assert len(indeed_calls) == 3    # 3 keywords for indeed — independent from linkedin
    assert all(c["count"] == 15 for c in linkedin_calls)
    assert all(c["count"] == 20 for c in indeed_calls)

    mock_gj.assert_called_once_with("Software Engineer", scrape._DEFAULT_LOCATION, 20, set(),
                                    remote_only=scrape._DEFAULT_REMOTE, hours_old=None)


def test_platform_with_no_keywords_is_skipped_entirely():
    with patch.object(scrape, "_fetch_jobspy_platform") as mock_jobspy, \
         patch.object(scrape, "_fetch_dice_rss") as mock_dice, \
         patch.object(scrape, "_fetch_google_jobs") as mock_gj, \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]):
        scrape.fetch_jobs(
            platform_keywords={"linkedin": "", "indeed": "", "dice": "", "google_jobs": ""},
            platform_counts={},
        )

    mock_jobspy.assert_not_called()
    mock_dice.assert_not_called()
    mock_gj.assert_not_called()


def test_linkedin_supports_a_different_count_per_keyword():
    """
    Real feature: LinkedIn's count can be a {keyword: count} dict so
    "Software Engineer" and "AI Engineer" scrape to different depths (e.g.
    40 vs 10) instead of one shared count applying to both — the daily_cap's
    ranking is global/score-based, not keyword-aware, so this is the lever
    for deliberately favoring one title's representation over another.
    """
    calls = []

    def record_jobspy(platform, kw, location, count, remote_only, seen_links, hours_old=None):
        calls.append({"platform": platform, "kw": kw, "count": count})
        return []

    # Explicitly disable indeed/dice/google_jobs (empty keyword strings) so
    # this test isn't sensitive to _DEFAULT_KEYWORDS' fallback value, which
    # is an env-derived module constant computed at import time — its value
    # can vary with test-session import order and, if indeed's fallback ever
    # also includes "Software Engineer", would collide on the same dict key
    # below. Matches the isolation pattern in test_platform_with_no_keywords_
    # is_skipped_entirely.
    with patch.object(scrape, "_fetch_jobspy_platform", side_effect=record_jobspy), \
         patch.object(scrape, "_fetch_dice_rss", return_value=[]), \
         patch.object(scrape, "_fetch_google_jobs", return_value=[]), \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]):
        scrape.fetch_jobs(
            platform_keywords={"linkedin": "Software Engineer,AI Engineer",
                                "indeed": "", "dice": "", "google_jobs": ""},
            platform_counts={"linkedin": {"Software Engineer": 40, "AI Engineer": 10}},
        )

    linkedin_calls = [c for c in calls if c["platform"] == "linkedin"]
    by_kw = {c["kw"]: c["count"] for c in linkedin_calls}
    assert by_kw == {"Software Engineer": 40, "AI Engineer": 10}


def test_linkedin_plain_int_count_still_shared_across_keywords():
    """Backward compatibility: a plain int (not a dict) still applies the
    same count to every LinkedIn keyword, as before this feature existed."""
    calls = []

    def record_jobspy(platform, kw, location, count, remote_only, seen_links, hours_old=None):
        calls.append({"platform": platform, "kw": kw, "count": count})
        return []

    with patch.object(scrape, "_fetch_jobspy_platform", side_effect=record_jobspy), \
         patch.object(scrape, "_fetch_dice_rss", return_value=[]), \
         patch.object(scrape, "_fetch_google_jobs", return_value=[]), \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]):
        scrape.fetch_jobs(
            platform_keywords={"linkedin": "Software Engineer,AI Engineer",
                                "indeed": "", "dice": "", "google_jobs": ""},
            platform_counts={"linkedin": 20},
        )

    linkedin_calls = [c for c in calls if c["platform"] == "linkedin"]
    assert len(linkedin_calls) == 2
    assert all(c["count"] == 20 for c in linkedin_calls)


def test_ats_uses_its_own_keyword_pool_independent_of_platform_keywords():
    with patch.object(scrape, "_fetch_jobspy_platform", return_value=[]), \
         patch.object(scrape, "_fetch_dice_rss", return_value=[]), \
         patch.object(scrape, "_fetch_google_jobs", return_value=[]), \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]) as mock_ats:
        scrape.fetch_jobs(
            platform_keywords={"linkedin": "", "indeed": "", "dice": "", "google_jobs": ""},
            ats_keywords="Software Engineer,AI Engineer,Backend Engineer",
        )

    mock_ats.assert_called_once_with("Software Engineer,AI Engineer,Backend Engineer",
                                     hours_old=None, location=scrape._DEFAULT_LOCATION)


def test_ats_recency_filter_passes_through_hours_old():
    """
    Real feature: ATS boards return their WHOLE current posting list with no
    server-side recency filter (unlike LinkedIn's hours_old search param), so
    adding a new company used to score its entire historical backlog in one
    shot. hours_old is now threaded through to fetch_ats_jobs() so it can be
    filtered client-side the same way LinkedIn already limits by recency.
    """
    with patch.object(scrape, "_fetch_jobspy_platform", return_value=[]), \
         patch.object(scrape, "_fetch_dice_rss", return_value=[]), \
         patch.object(scrape, "_fetch_google_jobs", return_value=[]), \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]) as mock_ats:
        scrape.fetch_jobs(
            platform_keywords={"linkedin": "", "indeed": "", "dice": "", "google_jobs": ""},
            ats_keywords="Software Engineer",
            hours_old=32,
        )

    mock_ats.assert_called_once_with("Software Engineer", hours_old=32, location=scrape._DEFAULT_LOCATION)
