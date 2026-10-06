"""
scraper/filter.py

Multi-layer dedup + sponsorship + quality + seniority + agency filter for
scraped jobs.

Layer 1 — seen_jobs.json: skip jobs already processed in a prior run.
Layer 2 — cross-platform dedup: collapse identical title+company jobs
          seen across different platforms in the same run (keep longest desc).
Layer 3 — sheet URL dedup: handled in sheets.py before writing rows.

Layers run in order: 1 first (cheapest), 2 second, 3 last (sheets.py).

Conservative sponsorship approach: only reject when JD explicitly says
"no sponsorship". Never reject for absence of sponsorship mention.
"""

import re
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scraper.seen_jobs import load_seen, is_seen
from scraper.agency_blocklist import is_recruiting_agency
from scraper import scraper_settings
from core.red_flags import COMPILED_PATTERNS as _COMPILED

# Sponsorship/clearance/ITAR rejection patterns now live in core/red_flags.py
# (single source of truth shared with core.should_apply).

_MIN_DESCRIPTION_CHARS = 200

# Entry-level/agency toggles are read fresh inside filter_jobs() via
# scraper_settings.load() (NOT frozen here at import time) — same reasoning
# as scraper/scheduler.py's search config: a change in the web UI's Scraper
# tab must apply on the very next cycle without a server restart.

# Free, zero-LLM-cost approximation of an "experience level: new grad / entry
# / associate" source filter (jobspy has no native param for this — see
# scraper/scrape.py's docstring gap note). Word-boundary title match; catches
# the obvious cases, not a substitute for should_apply's real seniority
# scoring, which still runs on everything that passes this gate.
_SENIOR_TITLE_PATTERNS = [
    r"\bsenior\b", r"\bsr\.?\b", r"\bstaff\b", r"\bprincipal\b", r"\blead\b",
    r"\bdirector\b", r"\bvp\b", r"\bvice president\b", r"\bhead of\b",
    r"\bmanager\b", r"\barchitect\b", r"\bchief\b",
]
_SENIOR_TITLE_RE = [re.compile(p, re.IGNORECASE) for p in _SENIOR_TITLE_PATTERNS]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _needs_sponsorship_rejection(description: str) -> bool:
    for pattern in _COMPILED:
        if pattern.search(description):
            return True
    return False


def _is_quality_job(job: dict) -> bool:
    return len(job.get("description", "")) >= _MIN_DESCRIPTION_CHARS


def _is_too_senior(title: str) -> bool:
    return any(p.search(title or "") for p in _SENIOR_TITLE_RE)


_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = s.lower()
    s = _PUNCT_RE.sub("", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


def _cross_platform_key(job: dict) -> str:
    return _normalize(job.get("title", "")) + "|" + _normalize(job.get("company", ""))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def filter_jobs(jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Filter jobs through quality, sponsorship, seniority, agency, and dedup layers.

    Args:
        jobs: Raw job dicts from scrape.py.

    Returns:
        (accepted, rejected) — rejected dicts carry a "reject_reason" key.

    Layer order (cheapest / most decisive first):
        1. Quality + sponsorship + seniority-title + recruiting-agency
        2. Layer 1: seen_jobs.json dedup (skip already-processed URLs)
        3. Layer 2: cross-platform dedup within this run
    """
    # Load seen_jobs once per call
    _, seen_urls = load_seen()

    settings = scraper_settings.load()
    entry_level_only = settings["entry_level_only"]
    exclude_agencies = settings["exclude_recruiting_agencies"]

    accepted_candidates: list[dict] = []
    rejected: list[dict] = []

    for job in jobs:
        desc = job.get("description", "")
        link = (job.get("apply_link") or "").strip()

        # Quality gate
        if not _is_quality_job(job):
            rejected.append({**job, "reject_reason": "description_too_short"})
            continue

        # Sponsorship gate
        if _needs_sponsorship_rejection(desc):
            rejected.append({**job, "reject_reason": "no_sponsorship"})
            continue

        # Seniority gate (title-based, free — approximates experienceLevel scoping)
        if entry_level_only and _is_too_senior(job.get("title", "")):
            rejected.append({**job, "reject_reason": "too_senior_title"})
            continue

        # Recruiting-agency gate (approximates excludeRecruitingAgencies)
        if exclude_agencies and is_recruiting_agency(job.get("company", "")):
            rejected.append({**job, "reject_reason": "recruiting_agency"})
            continue

        # Layer 1: seen_jobs.json
        if link and is_seen(link, seen_urls):
            print(f"  [filter] Skipping {job.get('title')} at {job.get('company')} — already seen")
            rejected.append({**job, "reject_reason": "already_seen"})
            continue

        accepted_candidates.append(job)

    # Layer 2: cross-platform dedup within this run
    # For each (title, company) key, keep the job with the longest description.
    seen_keys: dict[str, dict] = {}  # key → winning job
    for job in accepted_candidates:
        key = _cross_platform_key(job)
        if not key or key == "|":
            continue  # can't dedup without title+company
        if key not in seen_keys:
            seen_keys[key] = job
        else:
            existing = seen_keys[key]
            if len(job.get("description", "")) > len(existing.get("description", "")):
                # New job has more complete data — replace, reject the old one
                print(f"  [filter] Cross-platform dup: keeping {job.get('platform')} "
                      f"over {existing.get('platform')} for '{job.get('title')}' @ {job.get('company')}")
                rejected.append({**existing, "reject_reason": "cross_platform_duplicate"})
                seen_keys[key] = job
            else:
                print(f"  [filter] Cross-platform dup: dropping {job.get('platform')} "
                      f"for '{job.get('title')}' @ {job.get('company')}")
                rejected.append({**job, "reject_reason": "cross_platform_duplicate"})

    accepted = list(seen_keys.values())

    # Summary log
    n_no_sponsor = sum(1 for j in rejected if j["reject_reason"] == "no_sponsorship")
    n_short = sum(1 for j in rejected if j["reject_reason"] == "description_too_short")
    n_senior = sum(1 for j in rejected if j["reject_reason"] == "too_senior_title")
    n_agency = sum(1 for j in rejected if j["reject_reason"] == "recruiting_agency")
    n_seen = sum(1 for j in rejected if j["reject_reason"] == "already_seen")
    n_xdup = sum(1 for j in rejected if j["reject_reason"] == "cross_platform_duplicate")
    print(
        f"  [filter] {len(accepted)} accepted, {len(rejected)} rejected "
        f"({n_no_sponsor} no-sponsorship, {n_short} short-desc, "
        f"{n_senior} too-senior, {n_agency} recruiting-agency, "
        f"{n_seen} already-seen, {n_xdup} cross-platform-dup)"
    )
    return accepted, rejected


if __name__ == "__main__":
    # Smoke test
    sample = [
        {"id": "a", "title": "SWE", "company": "ACME", "description": "x" * 300,
         "apply_link": "http://a", "location": "", "is_remote": True,
         "date_posted": "", "platform": "linkedin", "job_type": ""},
        {"id": "b", "title": "SWE", "company": "Corp", "apply_link": "http://b",
         "description": "We will not provide visa sponsorship for this role. " + "x" * 250,
         "location": "", "is_remote": False, "date_posted": "", "platform": "indeed", "job_type": ""},
        # Cross-platform dup: same title+company on two platforms
        {"id": "c1", "title": "Backend Engineer", "company": "Acme Corp",
         "apply_link": "http://c1", "description": "y" * 400,
         "location": "", "is_remote": True, "date_posted": "", "platform": "linkedin", "job_type": ""},
        {"id": "c2", "title": "Backend Engineer", "company": "Acme Corp",
         "apply_link": "http://c2", "description": "y" * 600,  # longer — should win
         "location": "", "is_remote": True, "date_posted": "", "platform": "indeed", "job_type": ""},
    ]
    ok, bad = filter_jobs(sample)
    assert len(ok) == 2, f"Expected 2 accepted, got {len(ok)}"
    assert any(j["reject_reason"] == "no_sponsorship" for j in bad)
    assert any(j["reject_reason"] == "cross_platform_duplicate" for j in bad)
    # The winner for backend engineer should be the indeed one (longer desc)
    backend = next(j for j in ok if "backend" in j["title"].lower())
    assert backend["platform"] == "indeed", f"Expected indeed to win, got {backend['platform']}"
    print("filter.py smoke test passed")
