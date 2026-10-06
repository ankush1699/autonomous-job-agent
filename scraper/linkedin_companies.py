"""
scraper/linkedin_companies.py — persisted list of companies to search FOR
BY NAME on LinkedIn, closing a different gap than scraper/ats_companies.py:
Amazon, Microsoft, Google, Meta and most other large employers don't run a
Greenhouse/Lever/Ashby/Workday board at all (their own proprietary career
sites aren't any of those four), so they can never appear in that list.

They DO nearly all post to LinkedIn, though, and jobspy's LinkedIn scraper
(scraper/scrape.py, already cookie-authenticated via LINKEDIN_COOKIE) exposes
a native `linkedin_company_ids` filter — the same param LinkedIn's own site
sends when you pick a company in its search UI. This module persists that
"which companies" list; scraper/scrape.py's fetch_jobs() uses it to run one
extra, company-scoped LinkedIn search per enabled keyword, on top of the
normal broad keyword search — same profile-based scoring downstream, just
guaranteed to also check these specific employers rather than relying on
them happening to surface in the broad search.

Each entry: {"company": "Display Name", "linkedin_id": 1586}. The id is
LinkedIn's own internal numeric organization id (NOT the URL slug) — every
company page embeds it publicly as `urn:li:organization:<id>` in its raw
HTML, no login or scraping-evasion needed to read it (see
scripts/resolve_linkedin_company_id.py for the one-off lookup used to seed
this list).

Same JSON-persisted pattern as core/entries_store.py / scraper/ats_companies.py.
"""

import os
import json
import tempfile

_FILENAME = "linkedin_target_companies.json"


def _path() -> str:
    base = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
    return os.path.join(base, _FILENAME)


def load() -> list[dict]:
    path = _path()
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            companies = json.load(f)
        return companies if isinstance(companies, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save(companies: list[dict]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(companies, f, indent=2)
    os.replace(tmp, path)


def validate(company: str, linkedin_id) -> str | None:
    """Returns an error message if invalid, else None."""
    if not company or not company.strip():
        return "company is required"
    try:
        lid = int(linkedin_id)
    except (TypeError, ValueError):
        return "linkedin_id must be a number (LinkedIn's numeric organization id, not the URL slug)"
    if lid <= 0:
        return "linkedin_id must be a positive number"
    return None


def company_ids() -> list[int] | None:
    """The plain id list scrape.py's fetch_jobs() wants, or None if the list
    is empty — a falsy return means "don't run the company-targeted pass,"
    matching how ats_companies' empty list skips the ATS step entirely."""
    companies = load()
    return [c["linkedin_id"] for c in companies if c.get("linkedin_id")] or None
