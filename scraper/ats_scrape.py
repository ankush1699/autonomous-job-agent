"""
scraper/ats_scrape.py — fetch job postings directly from company ATS boards
(Greenhouse, Lever, Ashby, Workday), for scraper/ats_companies.py's curated
company list.

Each platform has a different (mostly undocumented, publicly-observable)
JSON API — no auth needed for any of them, since these are the same public
career-board APIs the company's own careers page JavaScript calls. Workday
is the most fragile of the four (internal API, varies slightly by tenant,
needs two requests — list then per-job detail) and is the most likely to
need a fix if a specific company's board stops working; the others are
comparatively stable and widely relied on by other scraping tools.

Returns job dicts in the SAME normalized schema as scraper/scrape.py's
platform fetchers (id, title, company, location, is_remote, apply_link,
description, date_posted, platform, job_type, sponsorship_safe), so they
flow through the exact same filter -> score -> entries/sheet pipeline
without any special-casing downstream.

Never raises past fetch_ats_jobs() — one company's board being down or
malformed must not abort the rest of the list, matching this scraper's
existing "never crash on a single failure" rule.
"""

import os
import re
import sys
import time
import hashlib
import datetime
import urllib.parse

import requests
from bs4 import BeautifulSoup

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_REQUEST_TIMEOUT = 15
_DELAY_BETWEEN_COMPANIES = 1.5  # seconds — polite pacing across sequential board fetches
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _strip_html(text: str) -> str:
    text = _HTML_TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Recency filtering — unlike LinkedIn (which sends hours_old as a server-side
# search param), ATS boards return their ENTIRE current posting list with no
# way to ask for "just the recent ones" server-side, so this is applied
# client-side per job after fetching. Real problem this fixes: adding a new
# company to the curated list used to score its WHOLE historical backlog in
# one shot (e.g. 171 keyword-matching postings from one company on the very
# first cycle) instead of just what's actually new — expensive and mostly
# irrelevant, since a posting sitting there for months isn't "new today."
# Reuses the same `hours_old` setting LinkedIn already uses (scraper tab's
# "Posted within (hours)"), rather than adding a second knob.
# ---------------------------------------------------------------------------

_WORKDAY_RELATIVE_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s*days?\s+ago)", re.IGNORECASE)


def _hours_since_iso(date_str: str) -> float | None:
    """Hours elapsed since an ISO8601 timestamp (Greenhouse's updated_at,
    Ashby's publishedAt). None if missing/unparseable."""
    if not date_str:
        return None
    try:
        dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def _hours_since_epoch_ms(epoch_ms) -> float | None:
    """Hours elapsed since a millisecond epoch timestamp (Lever's createdAt)."""
    if not epoch_ms:
        return None
    try:
        dt = datetime.datetime.fromtimestamp(epoch_ms / 1000, tz=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - dt).total_seconds() / 3600
    except (TypeError, ValueError, OSError):
        return None


def _hours_since_workday_relative(text: str) -> float | None:
    """Workday's postedOn is a human string ('Posted Today', 'Posted 3 Days
    Ago', 'Posted 30+ Days Ago'), not a machine timestamp — best-effort
    parse. None if the phrase isn't recognized (caller keeps the job rather
    than risk dropping a real posting over an unexpected format)."""
    if not text:
        return None
    m = _WORKDAY_RELATIVE_RE.search(text)
    if not m:
        return None
    phrase = m.group(1).lower()
    if phrase == "today":
        return 0.0
    if phrase == "yesterday":
        return 24.0
    days_match = re.search(r"\d+", phrase)
    return int(days_match.group()) * 24.0 if days_match else None


# ATS boards have no location search at all — a company's board returns
# every office worldwide, so OpenAI's board surfaced London and Tokyo roles
# into a US-only search. Applied client-side in fetch_ats_jobs(). The
# common case (location = "United States") is handled by REJECTING only on
# an explicit non-US marker and keeping everything else — ATS location
# strings are free text ("Remote", "SF Bay Area", "NYC or Remote") and a
# positive US-match would wrongly drop most of them. Any other wanted
# location falls back to a plain case-insensitive substring match.
_NON_US_MARKERS = re.compile(
    r"\b(london|uk|united kingdom|england|ireland|dublin|tokyo|japan|canada|toronto|"
    r"vancouver|montreal|india|bangalore|bengaluru|hyderabad|mumbai|pune|germany|"
    r"berlin|munich|france|paris|netherlands|amsterdam|spain|madrid|barcelona|"
    r"australia|sydney|melbourne|singapore|israel|tel aviv|brazil|s[aã]o paulo|"
    r"mexico city|poland|warsaw|sweden|stockholm|switzerland|zurich|emea|apac|latam)\b",
    re.IGNORECASE,
)
_US_LOCATION_WORDS = {"united states", "usa", "us", "u.s.", "u.s.a."}


def _location_ok(job_location: str, wanted: str | None) -> bool:
    if not wanted or not job_location:
        return True  # nothing to filter on — keep
    if wanted.strip().lower() in _US_LOCATION_WORDS:
        return not _NON_US_MARKERS.search(job_location)
    return wanted.strip().lower() in job_location.lower()


def _passes_recency(hours_since: float | None, hours_old: int | None) -> bool:
    """True if the job should be kept. hours_old=None disables filtering
    entirely (keep everything, the pre-existing behavior). An unparseable
    date (hours_since=None) also keeps the job — an unrecognized format
    should never silently drop a real posting."""
    if hours_old is None or hours_since is None:
        return True
    return hours_since <= hours_old


def _stable_id(platform: str, url: str) -> str:
    return f"{platform}:{hashlib.md5(url.encode()).hexdigest()[:10]}"


def _matches_keywords(title: str, keywords: list[str]) -> bool:
    """
    ATS board APIs don't support server-side keyword search (unlike LinkedIn/
    Indeed) — every posting on the board is returned, so keyword matching
    happens client-side against the title. Case-insensitive substring match
    against ANY keyword.
    """
    if not keywords:
        return True
    title_lower = (title or "").lower()
    return any(kw.strip().lower() in title_lower for kw in keywords if kw.strip())


# ---------------------------------------------------------------------------
# Greenhouse — https://boards-api.greenhouse.io/v1/boards/{token}/jobs
# ---------------------------------------------------------------------------

def _fetch_greenhouse(company_label: str, token: str, keywords: list[str], hours_old: int = None) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    resp = requests.get(url, headers=_UA, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for raw in data.get("jobs", []):
        title = raw.get("title", "")
        if not _matches_keywords(title, keywords):
            continue
        if not _passes_recency(_hours_since_iso(raw.get("updated_at", "")), hours_old):
            continue
        apply_link = raw.get("absolute_url", "")
        if not apply_link:
            continue
        location = (raw.get("location") or {}).get("name", "")
        jobs.append({
            "id": _stable_id("greenhouse", apply_link),
            "title": title,
            "company": company_label,
            "location": location,
            "is_remote": "remote" in location.lower(),
            "apply_link": apply_link,
            "description": _strip_html(raw.get("content", "")),
            "date_posted": raw.get("updated_at", ""),
            "platform": "greenhouse",
            "job_type": "",
            "sponsorship_safe": False,
        })
    return jobs


# ---------------------------------------------------------------------------
# Lever — https://api.lever.co/v0/postings/{company}?mode=json
# ---------------------------------------------------------------------------

def _fetch_lever(company_label: str, slug: str, keywords: list[str], hours_old: int = None) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = requests.get(url, headers=_UA, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    postings = resp.json()

    jobs = []
    for raw in postings:
        title = raw.get("text", "")
        if not _matches_keywords(title, keywords):
            continue
        if not _passes_recency(_hours_since_epoch_ms(raw.get("createdAt")), hours_old):
            continue
        apply_link = raw.get("hostedUrl", "")
        if not apply_link:
            continue
        categories = raw.get("categories") or {}
        location = categories.get("location", "") or ""
        created_ms = raw.get("createdAt")
        date_posted = ""
        if created_ms:
            try:
                date_posted = time.strftime("%Y-%m-%d", time.gmtime(created_ms / 1000))
            except (TypeError, ValueError):
                pass
        jobs.append({
            "id": _stable_id("lever", apply_link),
            "title": title,
            "company": company_label,
            "location": location,
            "is_remote": "remote" in location.lower(),
            "apply_link": apply_link,
            "description": _strip_html(raw.get("descriptionPlain") or raw.get("description", "")),
            "date_posted": date_posted,
            "platform": "lever",
            "job_type": categories.get("commitment", "") or "",
            "sponsorship_safe": False,
        })
    return jobs


# ---------------------------------------------------------------------------
# Ashby — https://api.ashbyhq.com/posting-api/job-board/{org}
# ---------------------------------------------------------------------------

def _fetch_ashby(company_label: str, org: str, keywords: list[str], hours_old: int = None) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{org}"
    resp = requests.get(url, headers=_UA, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for raw in data.get("jobs", []):
        title = raw.get("title", "")
        if not _matches_keywords(title, keywords):
            continue
        if not _passes_recency(_hours_since_iso(raw.get("publishedAt", "")), hours_old):
            continue
        apply_link = raw.get("jobUrl") or raw.get("applyUrl", "")
        if not apply_link:
            continue
        location = raw.get("location", "") or ""
        jobs.append({
            "id": _stable_id("ashby", apply_link),
            "title": title,
            "company": company_label,
            "location": location,
            "is_remote": bool(raw.get("isRemote")) or "remote" in location.lower(),
            "apply_link": apply_link,
            "description": _strip_html(raw.get("descriptionHtml") or raw.get("descriptionPlain", "")),
            "date_posted": raw.get("publishedAt", ""),
            "platform": "ashby",
            "job_type": raw.get("employmentType", "") or "",
            "sponsorship_safe": False,
        })
    return jobs


# ---------------------------------------------------------------------------
# Workday — internal API, reverse-engineered from the careers page's own
# network calls. Most fragile of the four: needs the tenant/wdN/site parsed
# out of the public careers URL, and two requests per job (list, then detail
# for the full description). If a specific company's board stops matching
# this shape, that company's fetch fails gracefully — it does not affect
# the other three ATS types or other companies.
# ---------------------------------------------------------------------------

_WORKDAY_HOST_RE = re.compile(r"^([\w-]+)\.(wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)


def _parse_workday_url(careers_url: str) -> tuple[str, str, str] | None:
    """Returns (tenant, wdN, site) or None if the URL doesn't match the
    expected myworkdayjobs.com shape."""
    parsed = urllib.parse.urlparse(careers_url)
    match = _WORKDAY_HOST_RE.match(parsed.netloc)
    if not match:
        return None
    tenant, wdn = match.group(1), match.group(2)
    path_parts = [p for p in parsed.path.split("/") if p]
    if not path_parts:
        return None
    site = path_parts[-1]  # last path segment is the site name (e.g. "databricks")
    return tenant, wdn, site


def _fetch_workday(company_label: str, careers_url: str, keywords: list[str], hours_old: int = None) -> list[dict]:
    parsed = _parse_workday_url(careers_url)
    if parsed is None:
        print(f"  [ats] Workday URL for '{company_label}' doesn't match the expected "
              f"'{{tenant}}.wd#.myworkdayjobs.com/.../{{site}}' shape — skipping: {careers_url}")
        return []
    tenant, wdn, site = parsed

    list_url = f"https://{tenant}.{wdn}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    resp = requests.post(list_url, headers={**_UA, "Content-Type": "application/json"},
                          json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
                          timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    postings = resp.json().get("jobPostings", [])

    jobs = []
    for raw in postings:
        title = raw.get("title", "")
        if not _matches_keywords(title, keywords):
            continue
        # Checked before the detail request below (not after) specifically
        # to skip the extra per-job HTTP call for postings recency would
        # drop anyway — no point paying for a description fetch on a job
        # that's about to be filtered out.
        if not _passes_recency(_hours_since_workday_relative(raw.get("postedOn", "")), hours_old):
            continue
        external_path = raw.get("externalPath", "")
        if not external_path:
            continue
        apply_link = f"https://{tenant}.{wdn}.myworkdayjobs.com/{site}{external_path}"

        # Second request for the full description — Workday's list endpoint
        # only returns a title/location summary, not the job body.
        description = ""
        try:
            detail_url = f"https://{tenant}.{wdn}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}"
            detail_resp = requests.get(detail_url, headers=_UA, timeout=_REQUEST_TIMEOUT)
            detail_resp.raise_for_status()
            posting_info = detail_resp.json().get("jobPostingInfo", {})
            description = _strip_html(posting_info.get("jobDescription", ""))
        except Exception as e:
            print(f"  [ats] Workday detail fetch failed for '{title}' @ {company_label}: {e}")

        jobs.append({
            "id": _stable_id("workday", apply_link),
            "title": title,
            "company": company_label,
            "location": raw.get("locationsText", "") or "",
            "is_remote": "remote" in (raw.get("locationsText", "") or "").lower(),
            "apply_link": apply_link,
            "description": description,
            "date_posted": raw.get("postedOn", ""),
            "platform": "workday",
            "job_type": "",
            "sponsorship_safe": False,
        })
        time.sleep(0.5)  # per-job detail fetch — light pacing within one company's board
    return jobs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "workday": _fetch_workday,
}


def fetch_ats_jobs(keywords: str, hours_old: int = None, location: str = None) -> list[dict]:
    """
    Fetch matching postings from every company in scraper/ats_companies.py's
    list. keywords: comma-separated string (same format as JOB_SEARCH_KEYWORDS).
    Returns [] immediately if no companies are configured — this whole
    source is opt-in, adding zero overhead until you add a company.

    hours_old: same "Posted within (hours)" setting LinkedIn already uses.
    None means no recency filtering (every matching posting on the board,
    the original behavior). Without this, adding a new company to the list
    scores its entire historical backlog on the very first cycle — real
    example: 171 keyword-matching postings from one company in one shot.
    """
    from scraper.ats_companies import load as load_companies

    companies = load_companies()
    if not companies:
        return []

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    all_jobs: list[dict] = []

    for i, entry in enumerate(companies):
        company_label = entry.get("company", "?")
        ats = entry.get("ats", "")
        identifier = entry.get("identifier", "")
        fetcher = _FETCHERS.get(ats)
        if fetcher is None:
            print(f"  [ats] Unknown ats type '{ats}' for '{company_label}' — skipping")
            continue
        try:
            jobs = fetcher(company_label, identifier, kw_list, hours_old)
            before = len(jobs)
            jobs = [j for j in jobs if _location_ok(j.get("location", ""), location)]
            dropped = before - len(jobs)
            print(f"  [ats] {ats} '{company_label}': {len(jobs)} matching jobs"
                  + (f" (within {hours_old}h)" if hours_old else "")
                  + (f", {dropped} dropped for location" if dropped else ""))
            all_jobs.extend(jobs)
        except Exception as e:
            print(f"  [ats] {ats} fetch failed for '{company_label}': {e}")
        if i < len(companies) - 1:
            time.sleep(_DELAY_BETWEEN_COMPANIES)

    print(f"  [ats] Total from {len(companies)} configured companies: {len(all_jobs)}")
    return all_jobs
