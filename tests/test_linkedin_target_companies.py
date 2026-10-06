"""
tests/test_linkedin_target_companies.py

scraper/linkedin_companies.py (Sep 2026): a persisted "search these specific
companies on LinkedIn" list, separate from scraper/ats_companies.py (which
only covers Greenhouse/Lever/Ashby/Workday boards — Amazon, Microsoft,
Google, Meta etc. run none of those and can never appear there).

Covers: load/save round-trip against a tmp OUTPUT_BASE_PATH (same isolation
pattern as tests/test_email_overrides.py), validate()'s rejection rules, and
company_ids()'s None-when-empty / list-of-ints-when-populated contract that
scraper/scrape.py's fetch_jobs() depends on to decide whether to run the
company-targeted LinkedIn pass at all.
"""
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import linkedin_companies as lc


def _tmp_env():
    tmp = tempfile.mkdtemp()
    return patch.dict(os.environ, {"OUTPUT_BASE_PATH": tmp})


def test_load_with_no_file_returns_empty_list():
    with _tmp_env():
        assert lc.load() == []


def test_save_then_load_round_trips():
    with _tmp_env():
        lc.save([{"company": "Amazon", "linkedin_id": 1586}])
        assert lc.load() == [{"company": "Amazon", "linkedin_id": 1586}]


def test_company_ids_is_none_when_list_is_empty():
    with _tmp_env():
        assert lc.company_ids() is None


def test_company_ids_returns_plain_int_list():
    with _tmp_env():
        lc.save([{"company": "Amazon", "linkedin_id": 1586}, {"company": "Microsoft", "linkedin_id": 1035}])
        assert lc.company_ids() == [1586, 1035]


def test_company_ids_skips_entries_missing_the_id():
    """A malformed/half-written entry (e.g. a failed manual JSON edit)
    shouldn't crash the whole company-targeted pass — just drop it."""
    with _tmp_env():
        lc.save([{"company": "Amazon", "linkedin_id": 1586}, {"company": "Broken"}])
        assert lc.company_ids() == [1586]


def test_validate_requires_company_name():
    assert lc.validate("", 1586) == "company is required"
    assert lc.validate("   ", 1586) == "company is required"


def test_validate_requires_numeric_id():
    assert "number" in lc.validate("Amazon", "not-a-number")
    assert "number" in lc.validate("Amazon", None)


def test_validate_rejects_the_url_slug_by_mistake():
    """The most likely real mistake: pasting the company's URL slug
    ("amazon") instead of its numeric id (1586) — must fail, not silently
    coerce/truncate into some other number."""
    err = lc.validate("Amazon", "amazon")
    assert err is not None and "number" in err


def test_validate_rejects_non_positive_id():
    assert lc.validate("Amazon", 0) is not None
    assert lc.validate("Amazon", -5) is not None


def test_validate_accepts_valid_entry():
    assert lc.validate("Amazon", 1586) is None
    assert lc.validate("Amazon", "1586") is None  # numeric string from a form field is fine
