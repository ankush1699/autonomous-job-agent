"""
tests/test_linkedin_company_targeted_scrape.py

Covers the LinkedIn company-targeted pass added to scraper/scrape.py (Sep
2026) for companies that can never appear in scraper/ats_companies.py
because they don't run a Greenhouse/Lever/Ashby/Workday board (Amazon,
Microsoft, Google, Meta, ...).

1. _fetch_jobspy_platform() forwards linkedin_company_ids to jobspy's
   scrape_jobs() as its native company filter, ONLY on the linkedin branch
   (Indeed has no such concept) and ONLY when a non-empty list is given.
2. fetch_jobs() runs one extra LinkedIn call per ENABLED LinkedIn keyword
   when linkedin_company_ids is given — same keywords as the broad search,
   just scoped to those employers — and skips the whole pass when it's
   None/empty (the common case: no companies configured yet).
3. The extra pass shares seen_links with the broad search, so a posting
   already caught there isn't double-counted.

Mocks every network-touching call so this runs with no real HTTP/API calls.

Run: pytest tests/test_linkedin_company_targeted_scrape.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import scrape


def test_fetch_jobspy_platform_forwards_company_ids_only_for_linkedin():
    with patch.object(scrape, "scrape_jobs") as mock_scrape_jobs:
        mock_scrape_jobs.return_value = None
        scrape._fetch_jobspy_platform(
            "linkedin", "Software Engineer", "Chicago", 10, False, set(),
            linkedin_company_ids=[1586, 1035],
        )
    assert mock_scrape_jobs.call_args.kwargs["linkedin_company_ids"] == [1586, 1035]


def test_fetch_jobspy_platform_omits_company_ids_when_none_given():
    with patch.object(scrape, "scrape_jobs") as mock_scrape_jobs:
        mock_scrape_jobs.return_value = None
        scrape._fetch_jobspy_platform("linkedin", "Software Engineer", "Chicago", 10, False, set())
    assert "linkedin_company_ids" not in mock_scrape_jobs.call_args.kwargs


def test_fetch_jobspy_platform_never_sends_company_ids_for_indeed():
    """Indeed has no company-filter concept in jobspy — even if a caller
    mistakenly passed the kwarg through for it, it must not be forwarded."""
    with patch.object(scrape, "scrape_jobs") as mock_scrape_jobs:
        mock_scrape_jobs.return_value = None
        scrape._fetch_jobspy_platform(
            "indeed", "Software Engineer", "Chicago", 10, False, set(),
            linkedin_company_ids=[1586],
        )
    assert "linkedin_company_ids" not in mock_scrape_jobs.call_args.kwargs


def test_fetch_jobs_skips_company_targeted_pass_when_none_given():
    """The common case: no companies configured in scraper/linkedin_companies.py
    yet. Must not add any extra calls beyond the normal broad search."""
    calls = []

    def record(platform, kw, location, count, remote_only, seen_links, hours_old=None, linkedin_company_ids=None):
        calls.append({"platform": platform, "kw": kw, "count": count, "company_ids": linkedin_company_ids})
        return []

    with patch.object(scrape, "_fetch_jobspy_platform", side_effect=record), \
         patch.object(scrape, "_fetch_dice_rss", return_value=[]), \
         patch.object(scrape, "_fetch_google_jobs", return_value=[]), \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]):
        scrape.fetch_jobs(
            platform_keywords={"linkedin": "Software Engineer", "indeed": "Software Engineer",
                                "dice": "Software Engineer", "google_jobs": "Software Engineer"},
            linkedin_company_ids=None,
        )

    # Exactly one call per jobspy platform (linkedin, indeed) from the broad
    # search — no third/company-targeted call.
    assert len(calls) == 2
    assert all(c["company_ids"] is None for c in calls)


def test_fetch_jobs_runs_one_extra_linkedin_call_per_keyword_when_companies_given():
    calls = []

    def record(platform, kw, location, count, remote_only, seen_links, hours_old=None, linkedin_company_ids=None):
        calls.append({"platform": platform, "kw": kw, "count": count, "company_ids": linkedin_company_ids})
        return []

    with patch.object(scrape, "_fetch_jobspy_platform", side_effect=record), \
         patch.object(scrape, "_fetch_dice_rss", return_value=[]), \
         patch.object(scrape, "_fetch_google_jobs", return_value=[]), \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]), \
         patch.object(scrape, "time"):  # no real sleeps
        scrape.fetch_jobs(
            platform_keywords={"linkedin": "Software Engineer,AI Engineer", "indeed": "Software Engineer",
                                "dice": "Software Engineer", "google_jobs": "Software Engineer"},
            linkedin_company_ids=[1586, 1035],
        )

    # Broad search: 2 linkedin keywords + 1 indeed keyword = 3 calls.
    # Company-targeted pass: 1 extra call per linkedin keyword = 2 more.
    company_targeted = [c for c in calls if c["company_ids"] == [1586, 1035]]
    broad_linkedin = [c for c in calls if c["platform"] == "linkedin" and c["company_ids"] is None]
    assert len(company_targeted) == 2
    assert {c["kw"] for c in company_targeted} == {"Software Engineer", "AI Engineer"}
    assert all(c["platform"] == "linkedin" for c in company_targeted)
    assert len(broad_linkedin) == 2  # the normal broad pass is untouched
    assert len(calls) == 5


def test_fetch_jobs_company_targeted_pass_dedupes_against_broad_search():
    """A posting the broad search already caught (same apply_link) must not
    be counted again by the company-targeted pass."""
    shared_job = {"id": "x", "title": "SWE", "company": "Amazon", "location": "",
                  "is_remote": False, "apply_link": "https://example.com/job/1",
                  "description": "", "date_posted": "", "platform": "linkedin",
                  "job_type": "", "sponsorship_safe": False}

    def record(platform, kw, location, count, remote_only, seen_links, hours_old=None, linkedin_company_ids=None):
        if linkedin_company_ids is None and platform == "linkedin":
            seen_links.add(shared_job["apply_link"])
            return [shared_job]
        if linkedin_company_ids is not None:
            # Company-targeted pass would also surface this same posting —
            # but seen_links already has it.
            if shared_job["apply_link"] in seen_links:
                return []
            seen_links.add(shared_job["apply_link"])
            return [shared_job]
        return []

    with patch.object(scrape, "_fetch_jobspy_platform", side_effect=record), \
         patch.object(scrape, "_fetch_dice_rss", return_value=[]), \
         patch.object(scrape, "_fetch_google_jobs", return_value=[]), \
         patch("scraper.ats_scrape.fetch_ats_jobs", return_value=[]):
        jobs = scrape.fetch_jobs(
            platform_keywords={"linkedin": "Software Engineer", "indeed": "",
                                "dice": "", "google_jobs": ""},
            linkedin_company_ids=[1586],
        )

    assert len(jobs) == 1  # not duplicated
