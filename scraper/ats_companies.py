"""
scraper/ats_companies.py — persisted list of companies to check directly on
their ATS (applicant tracking system) job board, closing the biggest gap in
job coverage: LinkedIn/Indeed/Dice/Google Jobs only surface postings that
got syndicated there, but a large share of real openings — especially at
startups — live ONLY on the company's own Greenhouse/Lever/Ashby/Workday
board and never get syndicated anywhere.

Unlike LinkedIn/Indeed, none of these ATS platforms expose a global
"search all companies" endpoint — each board is fetched per-company. So
this is a user-curated list, not a search: add the companies you actually
want covered.

Each entry: {"company": "Display Name", "ats": "greenhouse"|"lever"|"ashby"|"workday",
             "identifier": "<board token, or full careers URL for workday>"}

Same JSON-persisted pattern as core/entries_store.py / scraper/agency_blocklist.py.
"""

import os
import json
import tempfile

_FILENAME = "ats_companies.json"
_VALID_ATS = {"greenhouse", "lever", "ashby", "workday"}


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


def validate(company: str, ats: str, identifier: str) -> str | None:
    """Returns an error message if invalid, else None."""
    if not company or not company.strip():
        return "company is required"
    if ats not in _VALID_ATS:
        return f"ats must be one of {sorted(_VALID_ATS)}"
    if not identifier or not identifier.strip():
        return "identifier is required"
    if ats == "workday" and not identifier.strip().startswith("http"):
        return "workday identifier must be the full careers page URL (e.g. https://company.wd1.myworkdayjobs.com/en-US/CompanyCareers)"
    return None
