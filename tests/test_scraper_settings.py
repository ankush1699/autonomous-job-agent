"""
tests/test_scraper_settings.py

Covers scraper/scraper_settings.py's per-platform keyword/count model —
each platform searches its own subset of the master keyword pool at its own
result count (SerpApi's hard monthly quota wants few keywords, Dice is free
and can afford many), and disabling a keyword in the master pool removes it
from every platform's effective search without needing per-platform edits.

Run: pytest tests/test_scraper_settings.py -v
"""
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import scraper_settings

import pytest


@pytest.fixture(autouse=True)
def _custom_mode_defaults(monkeypatch):
    """Everything in this module tests the per-platform table, which since the
    Oct 2026 redesign is "custom" mode (a fresh install now starts in "simple"
    mode — see tests/test_scraper_simple_mode.py). Pin the fresh-install defaults
    to custom mode with every source on so these assertions keep testing exactly
    what they were written for."""
    original = scraper_settings._env_defaults

    def custom_defaults():
        d = original()
        d["mode"] = "custom"
        d["sources"] = {k: True for k in scraper_settings.SOURCES}
        return d

    monkeypatch.setattr(scraper_settings, "_env_defaults", custom_defaults)



def _isolated_output_dir():
    return patch.dict(os.environ, {"OUTPUT_BASE_PATH": tempfile.mkdtemp()})


def test_defaults_give_each_platform_its_own_keywords_and_count():
    with _isolated_output_dir():
        settings = scraper_settings.load()

    assert scraper_settings.platform_keywords_string("google_jobs", settings) == "Software Engineer"
    assert scraper_settings.platform_count("google_jobs", settings) == 20
    assert scraper_settings.platform_count("dice", settings) == 25
    # LinkedIn's default subset is narrower than the full 5-keyword pool
    linkedin_kw = scraper_settings.platform_keywords_string("linkedin", settings)
    assert "Software Engineer" in linkedin_kw
    assert "Full Stack Engineer" not in linkedin_kw


def test_disabling_a_master_keyword_removes_it_from_every_platform():
    with _isolated_output_dir():
        settings = scraper_settings.load()
        for kw in settings["keywords"]:
            if kw["value"] == "Software Engineer":
                kw["enabled"] = False
        scraper_settings.save(settings)
        reloaded = scraper_settings.load()

    for platform in ("linkedin", "indeed", "dice", "google_jobs"):
        assert "Software Engineer" not in scraper_settings.platform_keywords_string(platform, reloaded)
    assert "Software Engineer" not in scraper_settings.enabled_keywords_string(reloaded)


def test_platform_specific_keyword_subset_is_independent():
    """Removing a keyword from ONE platform's list (but keeping it enabled
    in the master pool) must not affect other platforms."""
    with _isolated_output_dir():
        settings = scraper_settings.load()
        settings["platforms"]["dice"]["keywords"] = ["AI Engineer"]
        scraper_settings.save(settings)
        reloaded = scraper_settings.load()

    assert scraper_settings.platform_keywords_string("dice", reloaded) == "AI Engineer"
    # Indeed's default list is untouched
    assert "Software Engineer" in scraper_settings.platform_keywords_string("indeed", reloaded)


def test_missing_platform_key_in_old_saved_file_falls_back_to_default():
    """A settings file saved before google_jobs existed as a platform key
    shouldn't crash or silently disable it — the merge should backfill it."""
    with _isolated_output_dir():
        scraper_settings.save({"platforms": {"linkedin": {"keywords": ["Software Engineer"], "count": 5}}})
        reloaded = scraper_settings.load()

    assert scraper_settings.platform_count("google_jobs", reloaded) == 20
    assert scraper_settings.platform_count("linkedin", reloaded) == 5  # explicit override preserved


def test_daily_cap_default():
    with _isolated_output_dir():
        settings = scraper_settings.load()
    assert settings["daily_cap"] == 25


def test_linkedin_default_gives_each_keyword_its_own_count():
    with _isolated_output_dir():
        settings = scraper_settings.load()
    counts = scraper_settings.platform_keyword_counts("linkedin", settings)
    assert counts == {"Software Engineer": 15, "AI Engineer": 15}


def test_platform_keyword_counts_supports_different_counts_per_keyword():
    """Real feature this was built for: 'Software Engineer' at 40, 'AI
    Engineer' at 10 — deliberately unequal depth per title."""
    with _isolated_output_dir():
        settings = scraper_settings.load()
        settings["platforms"]["linkedin"]["keywords"] = [
            {"value": "Software Engineer", "count": 40},
            {"value": "AI Engineer", "count": 10},
        ]
        scraper_settings.save(settings)
        reloaded = scraper_settings.load()

    counts = scraper_settings.platform_keyword_counts("linkedin", reloaded)
    assert counts == {"Software Engineer": 40, "AI Engineer": 10}


def test_platform_keyword_counts_respects_master_pool_disabling():
    with _isolated_output_dir():
        settings = scraper_settings.load()
        settings["platforms"]["linkedin"]["keywords"] = [
            {"value": "Software Engineer", "count": 40},
            {"value": "AI Engineer", "count": 10},
        ]
        for kw in settings["keywords"]:
            if kw["value"] == "AI Engineer":
                kw["enabled"] = False
        scraper_settings.save(settings)
        reloaded = scraper_settings.load()

    counts = scraper_settings.platform_keyword_counts("linkedin", reloaded)
    assert counts == {"Software Engineer": 40}  # AI Engineer disabled globally, dropped


def test_platform_keyword_counts_backward_compatible_with_shared_count_shape():
    """A platform still using the old plain-string-list + shared-count shape
    (every non-LinkedIn platform, or an old saved LinkedIn config) gets that
    one count repeated for every enabled keyword."""
    with _isolated_output_dir():
        settings = scraper_settings.load()
    counts = scraper_settings.platform_keyword_counts("dice", settings)
    assert len(counts) > 1
    assert len(set(counts.values())) == 1  # every keyword shares the same count
    assert list(counts.values())[0] == 25  # dice's default shared count


def test_platform_count_averages_linkedin_per_keyword_counts_for_display():
    """platform_count() (the CLI banner's representative number, not what
    actually gets scraped) should still return something sane for LinkedIn's
    new per-keyword shape instead of crashing or returning a dict."""
    with _isolated_output_dir():
        settings = scraper_settings.load()
        settings["platforms"]["linkedin"]["keywords"] = [
            {"value": "Software Engineer", "count": 40},
            {"value": "AI Engineer", "count": 10},
        ]
        scraper_settings.save(settings)
        reloaded = scraper_settings.load()

    assert scraper_settings.platform_count("linkedin", reloaded) == 25  # average of 40 and 10
