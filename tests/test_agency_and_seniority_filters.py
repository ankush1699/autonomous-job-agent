"""
tests/test_agency_and_seniority_filters.py

Covers two new scraper/filter.py gates added to approximate an Apify-style
scrape config (experienceLevel: new grad/entry/associate, and
excludeRecruitingAgencies):

1. scraper/agency_blocklist.py::is_recruiting_agency() — word-boundary
   matching, specifically guarding against the false-positive risk of short
   substrings (e.g. a bare "ust" or "volt" pattern would match "Trust",
   "August", "Voltage", "Revolt" — the real bug caught and fixed while
   writing this list, which is why every short/generic-sounding entry was
   spelled out in full instead of left as a bare fragment).
2. scraper/filter.py::_is_too_senior() — free, pre-LLM title filter.

Run: pytest tests/test_agency_and_seniority_filters.py -v
"""
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import agency_blocklist
from scraper.filter import _is_too_senior


def _isolated_output_dir():
    return patch.dict(os.environ, {"OUTPUT_BASE_PATH": tempfile.mkdtemp()})


# ---------------------------------------------------------------------------
# agency_blocklist
# ---------------------------------------------------------------------------

def test_known_agency_names_are_flagged():
    assert agency_blocklist.is_recruiting_agency("Kforce Inc")
    assert agency_blocklist.is_recruiting_agency("Pyramid Consulting, Inc")
    assert agency_blocklist.is_recruiting_agency("TEKsystems")


def test_real_employers_are_not_false_positives():
    """Regression test for the actual bug caught while building this list:
    bare short substrings like 'ust' or 'volt' would have matched real
    company names — the fix was spelling risky entries out in full."""
    assert not agency_blocklist.is_recruiting_agency("Trust Company of America")
    assert not agency_blocklist.is_recruiting_agency("August Health")
    assert not agency_blocklist.is_recruiting_agency("Justworks")
    assert not agency_blocklist.is_recruiting_agency("Voltage Park")
    assert not agency_blocklist.is_recruiting_agency("Revolt Media")
    assert not agency_blocklist.is_recruiting_agency("Google")
    assert not agency_blocklist.is_recruiting_agency("Microsoft")


def test_empty_or_none_company_is_not_an_agency():
    assert not agency_blocklist.is_recruiting_agency("")
    assert not agency_blocklist.is_recruiting_agency(None)


def test_persisted_override_replaces_defaults():
    with _isolated_output_dir():
        agency_blocklist.save(["totallycustomstaffingco"])
        assert agency_blocklist.is_recruiting_agency("TotallyCustomStaffingCo LLC")
        assert not agency_blocklist.is_recruiting_agency("Kforce Inc")  # default list overridden, not merged


# ---------------------------------------------------------------------------
# _is_too_senior title filter
# ---------------------------------------------------------------------------

def test_senior_titles_are_flagged():
    assert _is_too_senior("Senior Software Engineer")
    assert _is_too_senior("Staff Engineer")
    assert _is_too_senior("Principal Engineer")
    assert _is_too_senior("Engineering Manager")
    assert _is_too_senior("Director of Engineering")
    assert _is_too_senior("VP of Engineering")
    assert _is_too_senior("Solutions Architect")
    assert _is_too_senior("Team Lead, Backend")


def test_entry_level_titles_are_not_flagged():
    assert not _is_too_senior("Software Engineer")
    assert not _is_too_senior("Software Engineer I")
    assert not _is_too_senior("Junior Full Stack Developer")
    assert not _is_too_senior("Associate Software Engineer")
    assert not _is_too_senior("New Grad Software Engineer")
    assert not _is_too_senior("AI Engineer")


def test_word_boundary_avoids_false_positive_substrings():
    """'lead' as a bare substring shouldn't flag titles like 'Leadership
    Development Engineer' or company-specific titles containing 'leader'."""
    assert not _is_too_senior("Software Engineer, Leadership Development Program")
