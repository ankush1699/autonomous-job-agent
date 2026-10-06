"""
tests/test_scraper_simple_mode.py

Scraper tab redesign (Oct 2026): "simple" mode replaces the per-platform
keyword/count matrix with enabled roles (starred = deeper) + one depth preset +
source switches. The old matrix is "custom" mode, and any settings file saved
before the redesign must load as custom so an upgrade changes nothing.
OUTPUT_BASE_PATH is isolated per test by conftest.py.
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import scraper_settings as ss


def _simple(roles, depth="normal", sources=None, focus=()):
    s = ss._env_defaults()
    s["mode"], s["depth"] = "simple", depth
    s["keywords"] = [{"value": r, "enabled": True, "focus": r in focus} for r in roles]
    if sources is not None:
        s["sources"] = sources
    return s


def test_fresh_install_starts_in_simple_normal_with_google_jobs_off():
    s = ss.load()
    assert s["mode"] == "simple" and s["depth"] == "normal"
    assert s["sources"]["google_jobs"] is False and s["sources"]["linkedin"] is True


def test_pre_redesign_settings_file_loads_as_custom_and_keeps_its_platforms():
    saved = {"keywords": [{"value": "Software Engineer", "enabled": True}],
             "platforms": {"linkedin": {"keywords": [{"value": "Software Engineer", "count": 50}]},
                           "google_jobs": {"keywords": [], "count": 10}},
             "hours_old": 48, "daily_cap": 30, "min_score": 80}
    os.makedirs(os.environ["OUTPUT_BASE_PATH"], exist_ok=True)
    with open(ss._path(), "w") as f:
        json.dump(saved, f)
    s = ss.load()
    assert s["mode"] == "custom"
    assert s["sources"]["linkedin"] is True and s["sources"]["google_jobs"] is False  # derived: it had no keywords
    assert ss.platform_keyword_counts("linkedin", s) == {"Software Engineer": 50}     # unchanged behaviour


def test_linkedin_searches_every_role_and_starred_roles_deeper():
    s = _simple(["Software Engineer", "AI Engineer", "Backend Engineer"], focus=("AI Engineer",))
    plan = ss.simple_plan(s)
    assert plan["linkedin"] == {"AI Engineer": 40, "Software Engineer": 20, "Backend Engineer": 20}
    assert list(plan["linkedin"])[0] == "AI Engineer"   # starred first


def test_indeed_and_google_jobs_are_limited_to_the_first_roles_starred_first():
    roles = ["A", "B", "C", "D", "E"]
    s = _simple(roles, focus=("E",), sources={**ss._DEFAULT_SOURCES, "google_jobs": True})
    plan = ss.simple_plan(s)
    assert list(plan["indeed"]) == ["E", "A", "B"]      # max 3 on normal, starred first
    assert list(plan["google_jobs"]) == ["E", "A"]       # max 2 on normal -> 2 of 100 monthly searches per run
    assert set(plan["dice"].values()) == {20} and len(plan["dice"]) == 5


def test_depth_changes_the_numbers():
    light, deep = ss.simple_plan(_simple(["A"], "light")), ss.simple_plan(_simple(["A"], "deep"))
    assert light["linkedin"]["A"] < deep["linkedin"]["A"]
    assert light["dice"]["A"] < deep["dice"]["A"]
    assert ss.company_pass_results(_simple(["A"], "light")) < ss.company_pass_results(_simple(["A"], "deep"))


def test_switched_off_source_searches_nothing_in_either_mode():
    off = {**ss._DEFAULT_SOURCES, "dice": False}
    s = _simple(["A"], sources=off)
    assert ss.platform_keywords_string("dice", s) == "" and ss.platform_keyword_counts("dice", s) == {}
    custom = ss._env_defaults()
    custom.update(mode="custom", sources=off)
    assert ss.platform_keywords_string("dice", custom) == ""


def test_disabled_roles_are_never_searched():
    s = _simple(["A", "B"])
    s["keywords"][1]["enabled"] = False
    assert set(ss.simple_plan(s)["linkedin"]) == {"A"}


def test_custom_mode_has_no_company_pass_override():
    s = ss._env_defaults()
    s["mode"] = "custom"
    assert ss.company_pass_results(s) is None


def test_plan_sentence_says_what_the_run_will_do():
    s = _simple(["Software Engineer", "AI Engineer"])
    s.update(location="Chicago, IL", hours_old=48, daily_cap=30, min_score=80, entry_level_only=True)
    plan = ss.describe_plan(s, n_company_boards=24, n_linkedin_pages=21)
    text = plan["sentence"]
    assert "2 roles" in text and "Chicago, IL" in text and "the last 2 days" in text
    assert "24 company career pages" in text and "21 LinkedIn company pages" in text
    assert "best 30" in text and "80+" in text and "entry-level" in text
    assert plan["google_jobs_searches_per_run"] == 0
    assert plan["max_postings_from_boards"] > 0


def test_plan_drops_watched_sources_that_are_switched_off():
    s = _simple(["A"], sources={**ss._DEFAULT_SOURCES, "company_boards": False})
    plan = ss.describe_plan(s, n_company_boards=24, n_linkedin_pages=0)
    assert plan["company_boards"] == 0 and "career page" not in plan["sentence"]


def test_validate_rejects_bad_values():
    assert ss.validate({"mode": "turbo"})
    assert ss.validate({"depth": "extreme"})
    assert ss.validate({"sources": {"monster": True}})
    assert ss.validate({"min_score": 120})
    assert ss.validate({"mode": "simple", "depth": "deep", "sources": {"dice": False}, "min_score": 80}) is None
