"""
scraper/run_history.py — the last few scrape runs, each recorded as a funnel:

    found (per source) -> passed filters (per source) -> scored -> passed the bar -> kept / waitlisted

so the Scraper tab can show where jobs drop out ("LinkedIn found 180, 120 were
already seen, 6 passed your 80 bar") instead of one opaque "done" line, and
"found N jobs last run" next to every watched company.

Cost is reported only for REAL model calls: a job served from the URL cache or
core.jd_cache costs nothing and is counted separately. COST_PER_LLM_SCORE_USD is
the measured average for one Claude Haiku scoring call with prompt caching
(Sep 2026), so the dollar figure is an estimate, labelled as such in the UI.

Same JSON-persisted pattern as scraper/ats_companies.py; keeps the last
MAX_RUNS records.
"""

import datetime
import json
import os
import re
import tempfile
from collections import Counter

_FILENAME = "scrape_history.json"
MAX_RUNS = 30
COST_PER_LLM_SCORE_USD = 0.003
_ATS_PLATFORMS = {"greenhouse", "lever", "ashby", "workday"}


def _path() -> str:
    base = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
    return os.path.join(base, _FILENAME)


def load() -> list[dict]:
    try:
        with open(_path()) as f:
            runs = json.load(f)
        return runs if isinstance(runs, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def append(record: dict) -> None:
    runs = (load() + [record])[-MAX_RUNS:]
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(runs, f, indent=2)
    os.replace(tmp, path)


def last() -> dict | None:
    runs = load()
    return runs[-1] if runs else None


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_record(**fields) -> dict:
    rec = {
        "started_at": now(), "finished_at": None, "status": "running", "error": None,
        "found": {}, "after_filters": {}, "rejected": {},
        "scored": {"total": 0, "model_calls": 0, "from_cache": 0, "red_flag": 0, "errors": 0, "skipped_short_jd": 0},
        "passed_bar": 0, "kept": 0, "waitlisted": 0, "est_cost_usd": 0.0, "company_found": {},
    }
    rec.update(fields)
    return rec


def source_key(job: dict) -> str:
    """Which Scraper-tab source a job came from (matches scraper_settings.SOURCES)."""
    if job.get("via") == "linkedin_company_page":
        return "linkedin_company_pages"
    platform = (job.get("platform") or "").lower()
    if platform in _ATS_PLATFORMS:
        return "company_boards"
    return platform or "unknown"


def count_by_source(jobs: list[dict]) -> dict:
    return dict(Counter(source_key(j) for j in jobs))


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def company_found(jobs: list[dict], targets: list[str]) -> dict:
    """
    {watched company: postings found this run} for jobs that came from the
    company-targeted sources. LinkedIn names vary ("Amazon Web Services (AWS)")
    so a job is credited to the watched company whose normalised name is
    contained in the job's company name (or vice versa).
    """
    norm_targets = [(t, _norm(t)) for t in targets if _norm(t)]
    counts = {t: 0 for t, _ in norm_targets}
    for job in jobs:
        if source_key(job) not in ("company_boards", "linkedin_company_pages"):
            continue
        jc = _norm(job.get("company", ""))
        match = next((t for t, n in norm_targets if n and (n in jc or (jc and jc in n))), None)
        if match:
            counts[match] += 1
    return counts


def finish(record: dict, status: str = "completed", error: str = None) -> dict:
    record["finished_at"] = now()
    record["status"] = status
    record["error"] = error
    record["est_cost_usd"] = round(record["scored"]["model_calls"] * COST_PER_LLM_SCORE_USD, 3)
    try:
        append(record)
    except OSError as e:
        print(f"[run_history] could not save run record: {e}")
    return record
