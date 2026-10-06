"""
scraper/scrape.py

Fetches job listings from five platforms:
  1. LinkedIn   — jobspy (cookie-authenticated)
  2. Indeed     — jobspy
  3. Dice       — RSS feed via feedparser
  4. Wellfound  — jobspy (fails gracefully if this version lacks support)
  5. Google Jobs — SerpAPI (skipped silently if SERPAPI_KEY not set)

Returns a list of normalized job dicts ready for filter.py.

Schema per job dict:
  id, title, company, location, is_remote, apply_link,
  description, date_posted, platform, job_type, sponsorship_safe
"""

import os
import sys
import re
import hashlib
import time
import urllib.parse
from typing import Optional

import requests
import feedparser
from bs4 import BeautifulSoup
from jobspy import scrape_jobs

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_JOBSPY_PLATFORMS = ["linkedin", "indeed"]

_DEFAULT_KEYWORDS = os.getenv("JOB_SEARCH_KEYWORDS", "software engineer")
_DEFAULT_LOCATION = os.getenv("JOB_SEARCH_LOCATION", "United States")
_DEFAULT_REMOTE   = os.getenv("JOB_SEARCH_REMOTE", "true").lower() == "true"
_DEFAULT_RESULTS  = int(os.getenv("JOB_RESULTS_PER_PLATFORM", "20"))
_LINKEDIN_COOKIE  = os.getenv("LINKEDIN_COOKIE", "").strip()
_DICE_API_DELAY   = float(os.getenv("DICE_API_DELAY", "2"))
# Modest per-keyword depth for the LinkedIn company-targeted pass — it's
# already narrowed to a short, curated employer list, so results are
# naturally sparse; no need for the broad search's higher counts.
_COMPANY_TARGET_RESULTS = int(os.getenv("LINKEDIN_COMPANY_TARGET_RESULTS", "10"))
_HOURS_OLD        = int(os.getenv("JOB_SEARCH_HOURS_OLD", "24"))
_SERPAPI_KEY      = os.getenv("SERPAPI_KEY", "").strip()

_DICE_RSS_URL     = "https://www.dice.com/jobs/q-{slug}-l-{loc}.rss"
_DICE_HEADERS     = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _stable_id(platform: str, url: str) -> str:
    return f"{platform}:{hashlib.md5(url.encode()).hexdigest()[:10]}"


def _strip_html(text: str) -> str:
    text = _HTML_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _keyword_list(keywords: str) -> list[str]:
    """Split comma-separated keywords; fall back to single entry."""
    parts = [k.strip() for k in keywords.split(",") if k.strip()]
    return parts if parts else [keywords]


# ---------------------------------------------------------------------------
# 1 & 2: LinkedIn + Indeed + Wellfound via jobspy
# ---------------------------------------------------------------------------

def _normalize_jobspy_row(row) -> Optional[dict]:
    try:
        apply_link = str(row.get("job_url") or row.get("job_url_direct") or "").strip()
        if not apply_link:
            return None
        title   = str(row.get("title")   or "").strip()
        company = str(row.get("company") or "").strip()
        if not title or not company:
            return None
        description = str(row.get("description") or "").strip()
        location    = str(row.get("location")    or "").strip()
        platform    = str(row.get("site")        or "").strip().lower()
        is_remote   = bool(row.get("is_remote")) or "remote" in location.lower()
        date_posted = ""
        raw = row.get("date_posted")
        if raw:
            try:
                date_posted = raw.isoformat() if hasattr(raw, "isoformat") else str(raw)
            except Exception:
                pass
        return {
            "id": _stable_id(platform, apply_link),
            "title": title,
            "company": company,
            "location": location,
            "is_remote": is_remote,
            "apply_link": apply_link,
            "description": description,
            "date_posted": date_posted,
            "platform": platform,
            "job_type": str(row.get("job_type") or "").strip(),
            "sponsorship_safe": False,
        }
    except Exception as e:
        print(f"  [scrape] Row normalization error: {e}")
        return None


def _fetch_jobspy_platform(
    platform: str,
    keywords: str,
    location: str,
    results_per_platform: int,
    remote_only: bool,
    seen_links: set[str],
    hours_old: int = None,
    linkedin_company_ids: list[int] = None,
) -> list[dict]:
    kwargs = dict(
        site_name=[platform],
        search_term=keywords,
        location=location,
        results_wanted=results_per_platform,
        is_remote=remote_only,
        hours_old=hours_old if hours_old is not None else _HOURS_OLD,
        country_indeed="USA",
    )
    if platform == "linkedin":
        # LinkedIn-only: always scope to full-time postings — jobspy maps this
        # string to its JobType.FULL_TIME enum and sends LinkedIn's own f_JT
        # query param, same filter as clicking "Full-time" in LinkedIn's own
        # search UI. Fixed on, not a setting — explicit user choice (Sep 2026),
        # not worth a toggle since it's always what's wanted here. Indeed is
        # untouched; this kwarg is only added on the LinkedIn branch.
        kwargs["job_type"] = "fulltime"
        if _LINKEDIN_COOKIE:
            kwargs["linkedin_fetch_description"] = True
            kwargs["linkedin_cookie"] = _LINKEDIN_COOKIE
        else:
            print("  [scrape] Warning: LinkedIn cookie not set — results may be limited or blocked")
        # Company-targeted pass (scraper/linkedin_companies.py, Sep 2026):
        # jobspy's own LinkedIn company filter, same `f_C` param LinkedIn's
        # search UI sends when you pick a company. Restricts this ONE call to
        # only the given employers instead of the broad keyword search —
        # lets fetch_jobs() guarantee specific companies (e.g. Amazon,
        # Microsoft — neither runs a Greenhouse/Lever/Ashby/Workday board, so
        # they can never be covered by ats_companies.py) get checked every
        # cycle rather than relying on them happening to surface.
        if linkedin_company_ids:
            kwargs["linkedin_company_ids"] = linkedin_company_ids

    df = scrape_jobs(**kwargs)
    if df is None or df.empty:
        return []

    jobs = []
    for _, row in df.iterrows():
        job = _normalize_jobspy_row(row)
        if job is None or job["apply_link"] in seen_links:
            continue
        seen_links.add(job["apply_link"])
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# 3: Dice RSS feed
# ---------------------------------------------------------------------------

def _normalize_dice_rss_entry(entry: dict) -> Optional[dict]:
    """Map a feedparser entry to the standard job dict."""
    try:
        apply_link = (entry.get("link") or entry.get("id") or "").strip()
        if not apply_link:
            return None

        # Dice RSS titles are typically "Job Title - Company Name" or "Job Title at Company"
        raw_title = entry.get("title", "").strip()
        title, company = raw_title, ""
        if " - " in raw_title:
            parts = raw_title.rsplit(" - ", 1)
            title, company = parts[0].strip(), parts[1].strip()
        elif " at " in raw_title.lower():
            idx = raw_title.lower().rfind(" at ")
            title   = raw_title[:idx].strip()
            company = raw_title[idx + 4:].strip()

        if not title:
            return None

        # Description from summary HTML
        raw_desc = entry.get("summary") or entry.get("description") or ""
        description = _strip_html(raw_desc)

        # Location: may appear in tags or description
        location = ""
        tags = entry.get("tags", [])
        if tags:
            location = tags[0].get("term", "")

        date_posted = entry.get("published", "")

        return {
            "id": _stable_id("dice", apply_link),
            "title": title,
            "company": company,
            "location": location,
            "is_remote": "remote" in location.lower() or "remote" in description.lower(),
            "apply_link": apply_link,
            "description": description,
            "date_posted": date_posted,
            "platform": "dice",
            "job_type": "",
            "sponsorship_safe": False,  # RSS doesn't filter; filter.py handles it
        }
    except Exception as e:
        print(f"  [scrape] Dice RSS entry error: {e}")
        return None


def _hours_since_struct(published_parsed) -> Optional[float]:
    """Hours since a feedparser `*_parsed` time.struct_time (UTC). None if absent."""
    if not published_parsed:
        return None
    try:
        import calendar
        posted = calendar.timegm(published_parsed)
        return (time.time() - posted) / 3600
    except (TypeError, ValueError, OverflowError):
        return None


_RELATIVE_AGE_RE = re.compile(
    r"(?:(\d+)\+?\s*(hour|hr|day|week|month)s?\s+ago)|(just posted|today|yesterday)",
    re.IGNORECASE,
)


def _hours_from_relative(text: str) -> Optional[float]:
    """
    Hours implied by a human relative-age string — SerpApi's
    detected_extensions.posted_at is '3 days ago' / '2 hours ago' /
    '30+ days ago' / 'Just posted', never a real timestamp. None if the
    phrase isn't recognized (caller keeps the job rather than drop a real
    posting over an unexpected format).
    """
    if not text:
        return None
    m = _RELATIVE_AGE_RE.search(text)
    if not m:
        return None
    if m.group(3):
        word = m.group(3).lower()
        return 24.0 if word == "yesterday" else 0.0
    n, unit = int(m.group(1)), m.group(2).lower()
    per_unit = {"hour": 1, "hr": 1, "day": 24, "week": 168, "month": 720}[unit]
    return n * per_unit


def _fetch_dice_rss(
    keywords: str,
    results_per_platform: int,
    seen_links: set[str],
    location: str = _DEFAULT_LOCATION,
    remote_only: bool = False,
    hours_old: int = None,
) -> list[dict]:
    """
    Scrape Dice via RSS feed. One call per comma-separated keyword.
    URL: https://www.dice.com/jobs/q-{keyword}-l-{location}.rss

    location goes into the feed URL (was hardcoded to "united+states" before,
    silently ignoring the Scraper tab's setting). Dice's feed has no
    recency or remote params, so hours_old and remote_only are applied
    client-side from the entry's published date / location text — before
    this, Dice returned postings of any age and they competed on equal
    footing with fresh LinkedIn results for the daily cap.
    """
    kw_list = _keyword_list(keywords)
    all_jobs: list[dict] = []
    loc_slug = urllib.parse.quote_plus((location or _DEFAULT_LOCATION).strip().lower())

    for idx, kw in enumerate(kw_list):
        slug = urllib.parse.quote_plus(kw)
        url  = _DICE_RSS_URL.format(slug=slug, loc=loc_slug)
        try:
            feed = feedparser.parse(url, request_headers=_DICE_HEADERS)

            # Detect bot-redirect: feedparser sets bozo=True when it gets HTML not XML
            if feed.bozo and "html" in str(feed.get("bozo_exception", "")).lower():
                print(f"  [scrape] Dice RSS '{kw}': bot-redirected to HTML — 0 results")
            elif not feed.entries:
                print(f"  [scrape] Dice RSS '{kw}': empty feed — 0 results")
            else:
                count = 0
                for entry in feed.entries[:results_per_platform]:
                    if hours_old is not None:
                        age = _hours_since_struct(entry.get("published_parsed"))
                        if age is not None and age > hours_old:
                            continue
                    job = _normalize_dice_rss_entry(entry)
                    if job is None or job["apply_link"] in seen_links:
                        continue
                    if remote_only and not job.get("is_remote"):
                        continue
                    seen_links.add(job["apply_link"])
                    all_jobs.append(job)
                    count += 1
                print(f"  [scrape] Dice RSS '{kw}': {count} new jobs")

        except Exception as e:
            print(f"  [scrape] Dice RSS error for '{kw}': {e}")

        if idx < len(kw_list) - 1:
            time.sleep(_DICE_API_DELAY)

    return all_jobs


# ---------------------------------------------------------------------------
# 5: Google Jobs via SerpAPI
# ---------------------------------------------------------------------------

def _normalize_serpapi_job(raw: dict, seen_links: set[str]) -> Optional[dict]:
    """Map a SerpAPI google_jobs result to the standard job dict."""
    try:
        title   = str(raw.get("title")        or "").strip()
        company = str(raw.get("company_name") or "").strip()
        if not title or not company:
            return None

        # Apply link: prefer direct apply options, fall back to job URL
        apply_link = ""
        for opt in raw.get("apply_options") or []:
            link = opt.get("link", "").strip()
            if link:
                apply_link = link
                break
        if not apply_link:
            # Construct from job_id
            job_id = raw.get("job_id", "")
            apply_link = f"https://www.google.com/search?q={urllib.parse.quote(title+' '+company)}&ibp=htl;jobs&htidocid={job_id}" if job_id else ""
        if not apply_link:
            return None
        if apply_link in seen_links:
            return None

        location    = str(raw.get("location") or "").strip()
        description = str(raw.get("description") or "").strip()
        extensions  = raw.get("detected_extensions") or {}
        date_posted = str(extensions.get("posted_at") or "").strip()
        job_type    = str(extensions.get("schedule_type") or "").strip()
        is_remote   = (
            extensions.get("work_from_home", False)
            or "remote" in location.lower()
        )

        return {
            "id": _stable_id("google_jobs", apply_link),
            "title": title,
            "company": company,
            "location": location,
            "is_remote": is_remote,
            "apply_link": apply_link,
            "description": description,
            "date_posted": date_posted,
            "platform": "google_jobs",
            "job_type": job_type,
            "sponsorship_safe": False,
            "jd_truncated": True,   # default True; set False after successful enrichment
        }
    except Exception as e:
        print(f"  [scrape] SerpAPI job normalization error: {e}")
        return None


def _fetch_google_jobs(
    keywords: str,
    location: str,
    results_per_platform: int,
    seen_links: set[str],
    remote_only: bool = False,
    hours_old: int = None,
) -> list[dict]:
    """
    Fetch jobs from Google Jobs via SerpAPI.
    Skipped silently if SERPAPI_KEY is not set.
    One call per comma-separated keyword with DICE_API_DELAY between calls.

    remote_only / hours_old are applied client-side from the result's
    detected_extensions (work_from_home, posted_at) rather than as SerpApi
    query params: the query-side `chips` filter has changed shape across
    SerpApi versions and can't be verified here without spending quota,
    whereas the extensions are present on every result regardless. Before
    this, both settings were silently ignored for Google Jobs.
    """
    if not _SERPAPI_KEY:
        print("  [scrape] Google Jobs: SERPAPI_KEY not set — skipping")
        return []

    try:
        from serpapi import GoogleSearch
    except ImportError:
        print("  [scrape] Google Jobs: serpapi package not installed — skipping")
        return []

    kw_list   = _keyword_list(keywords)
    all_jobs: list[dict] = []

    for idx, kw in enumerate(kw_list):
        try:
            search = GoogleSearch({
                "engine":   "google_jobs",
                "q":        kw,
                "location": location,
                "api_key":  _SERPAPI_KEY,
                "hl":       "en",
            })
            # See scraper/sponsorship.py's comment on this same fix: the
            # serpapi package's default timeout (60000) is meant as
            # milliseconds but is passed straight into requests.get(timeout=...)
            # which takes seconds — an effective ~16.7 hour hang risk.
            search.timeout = 15
            results = search.get_dict()
            jobs_raw = results.get("jobs_results") or []

            count = 0
            for raw in jobs_raw[:results_per_platform]:
                job = _normalize_serpapi_job(raw, seen_links)
                if job is None:
                    continue
                if remote_only and not job.get("is_remote"):
                    continue
                if hours_old is not None:
                    age = _hours_from_relative(job.get("date_posted", ""))
                    if age is not None and age > hours_old:
                        continue
                seen_links.add(job["apply_link"])
                all_jobs.append(job)
                count += 1

            print(f"  [scrape] Google Jobs (SerpAPI) '{kw}': {count} new jobs")

        except Exception as e:
            print(f"  [scrape] Google Jobs error for '{kw}': {e}")

        if idx < len(kw_list) - 1:
            time.sleep(_DICE_API_DELAY)

    return all_jobs


# ---------------------------------------------------------------------------
# Google Jobs JD enrichment — fetch full page from apply_link
# ---------------------------------------------------------------------------

_JD_FETCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_JD_FETCH_TIMEOUT = 10
_JD_FETCH_DELAY   = 2   # seconds between fetches

# Ordered list of (selector_type, selector_value) to try when extracting JD text.
# First match with 200+ words wins.
_JD_SELECTORS = [
    ("id",    "job-description"),
    ("id",    "jobDescriptionText"),
    ("id",    "job_description"),
    ("id",    "jobdescription"),
    ("class", "job-description"),
    ("class", "jobDescription"),
    ("class", "jobDescriptionText"),
    ("class", "jobsearch-jobDescriptionText"),
    ("class", "description"),
    ("attr",  "data-testid=job-description"),
    ("tag",   "article"),
    ("tag",   "main"),
]


def _extract_jd_from_html(html: str) -> str:
    """
    Try each selector in order; return cleaned text of the first element
    that contains 200+ words. Returns empty string if nothing qualifies.
    """
    soup = BeautifulSoup(html, "html.parser")

    for selector_type, selector_value in _JD_SELECTORS:
        try:
            if selector_type == "id":
                el = soup.find(id=selector_value)
            elif selector_type == "class":
                el = soup.find(class_=selector_value)
            elif selector_type == "attr":
                attr_name, attr_val = selector_value.split("=", 1)
                el = soup.find(attrs={attr_name: attr_val})
            else:  # tag
                el = soup.find(selector_value)

            if el is None:
                continue

            text = re.sub(r"\s+", " ", el.get_text(separator=" ")).strip()
            if len(text.split()) >= 200:
                return text
        except Exception:
            continue

    return ""


def _enrich_google_job_descriptions(jobs: list[dict]) -> None:
    """
    For each google_jobs result, attempt to fetch the full JD from apply_link.

    Mutates jobs in place:
      - On success (200+ words extracted): replaces description, sets jd_truncated=False
      - On failure or short content:       keeps original description, jd_truncated stays True

    Never raises — all failures are logged and skipped.
    """
    if not jobs:
        return

    print(f"  [scrape] Enriching {len(jobs)} Google Jobs JDs from apply links...")

    for i, job in enumerate(jobs):
        url     = job.get("apply_link", "")
        company = job.get("company", "?")
        title   = job.get("title", "?")[:40]

        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _JD_FETCH_UA},
                timeout=_JD_FETCH_TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                print(f"  [scrape] JD fetch {i+1}/{len(jobs)}: {resp.status_code} — {company} ({title})")
                # jd_truncated stays True
            else:
                full_text = _extract_jd_from_html(resp.text)
                word_count = len(full_text.split())
                if word_count >= 200:
                    job["description"] = full_text
                    job["jd_truncated"] = False
                    print(f"  [scrape] JD fetch {i+1}/{len(jobs)}: {word_count}w — {company} ({title})")
                else:
                    print(f"  [scrape] JD fetch {i+1}/{len(jobs)}: <200w ({word_count}) — keeping snippet — {company}")
                    # jd_truncated stays True

        except requests.exceptions.Timeout:
            print(f"  [scrape] Warning: JD fetch timeout — {company} ({title})")
        except Exception as e:
            print(f"  [scrape] Warning: JD fetch failed — {company} ({title}): {type(e).__name__}: {e}")

        if i < len(jobs) - 1:
            time.sleep(_JD_FETCH_DELAY)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_jobs(
    platform_keywords: dict = None,
    platform_counts: dict = None,
    ats_keywords: str = None,
    location: str = _DEFAULT_LOCATION,
    remote_only: bool = _DEFAULT_REMOTE,
    hours_old: int = None,
    linkedin_company_ids: list[int] = None,
    include_ats: bool = True,
    linkedin_company_results: int = None,
) -> list[dict]:
    """
    Scrape jobs from all active platforms and return deduplicated normalized list.

    platform_keywords / platform_counts: {"linkedin": "...", "indeed": "...",
    "dice": "...", "google_jobs": "..."} — each platform searches its OWN
    keyword list at its OWN per-keyword result count (see
    scraper/scraper_settings.py's module docstring for why: SerpApi's hard
    monthly quota wants few keywords, Dice is free and can afford many).
    platform_counts["linkedin"] may be either a plain int (one shared count
    for every keyword) OR a {keyword: count} dict (a different depth per
    title — see scraper_settings.platform_keyword_counts()); every other
    platform only supports the plain-int form.
    A platform with an empty/missing keyword string is skipped entirely —
    that's what "0 keywords enabled for this platform" means, not "use the
    default." Falls back to JOB_SEARCH_KEYWORDS for any platform not given
    an explicit entry, for backward-compatible standalone/CLI use.

    ats_keywords: comma-separated keywords for ATS boards specifically — no
    per-platform cost there, so this is normally the full enabled pool.

    linkedin_company_ids: LinkedIn's own numeric organization ids (NOT
    ats_companies.py — that's Greenhouse/Lever/Ashby/Workday only, which
    Amazon/Microsoft/Google/Meta/etc. don't run) from
    scraper/linkedin_companies.py's persisted list. When given, runs ONE
    EXTRA LinkedIn search per enabled LinkedIn keyword, restricted to just
    these employers via jobspy's native company filter — guarantees these
    specific companies get checked every cycle instead of relying on them
    happening to surface in the broad keyword search. None/empty skips it.
    linkedin_company_results: results per keyword for that pass (the Scraper
    tab's depth preset); None uses LINKEDIN_COMPANY_TARGET_RESULTS (default 10).

    include_ats: False skips the company career-board step entirely (the
    "company career pages" source switch in the Scraper tab).

    Active platforms (in order):
      1. LinkedIn  (jobspy, cookie-authenticated)
      1b. LinkedIn, company-targeted (same jobspy call, scoped to
          linkedin_company_ids — skipped if that list is empty)
      2. Indeed    (jobspy)
      3. Dice      (RSS feed via feedparser)
      4. Google Jobs (SerpAPI — skipped if SERPAPI_KEY not set)
      5. ATS boards (Greenhouse/Lever/Ashby/Workday — only companies configured
         in scraper/ats_companies.py; skipped entirely if that list is empty)
    """
    platform_keywords = platform_keywords or {}
    platform_counts = platform_counts or {}
    all_jobs:   list[dict] = []
    seen_links: set[str]   = set()

    # --- jobspy platforms (LinkedIn, Indeed) — one call per keyword ---
    for platform in _JOBSPY_PLATFORMS:
        kw_string = platform_keywords.get(platform, _DEFAULT_KEYWORDS)
        count = platform_counts.get(platform, _DEFAULT_RESULTS)
        kw_list = _keyword_list(kw_string)
        if not kw_string.strip():
            print(f"  [scrape] {platform}: no keywords enabled — skipping")
            continue
        for idx_kw, kw in enumerate(kw_list):
            # LinkedIn's count can be a {keyword: count} dict for per-keyword
            # depth (e.g. "Software Engineer": 40, "AI Engineer": 10) — see
            # scraper_settings.platform_keyword_counts(). Every other
            # platform (and LinkedIn if ever passed a plain int) keeps the
            # simple shared-count behavior unchanged.
            kw_count = count.get(kw, _DEFAULT_RESULTS) if isinstance(count, dict) else count
            try:
                print(f"  [scrape] Fetching from {platform} ('{kw}', count={kw_count})...")
                jobs = _fetch_jobspy_platform(
                    platform, kw, location, kw_count, remote_only, seen_links,
                    hours_old=hours_old,
                )
                print(f"  [scrape] {platform} '{kw}': {len(jobs)} new jobs")
                all_jobs.extend(jobs)
            except Exception as e:
                print(f"  [scrape] ERROR fetching from {platform} ('{kw}'): {e}")
                if platform == "linkedin" and idx_kw == 0:
                    print("  [scrape] Warning: LinkedIn failure — continuing with other platforms")
            # Small delay between keyword iterations for the same platform
            if idx_kw < len(kw_list) - 1:
                time.sleep(_DICE_API_DELAY)

    # --- LinkedIn, company-targeted — scraper/linkedin_companies.py ---
    # Reuses the SAME enabled LinkedIn keyword list (so "Software Engineer",
    # "AI Engineer", etc. still apply — this narrows WHICH employers, not
    # what role), one extra call per keyword with jobspy's company filter.
    # seen_links is shared with the broad search above, so a posting already
    # caught there isn't fetched/counted twice.
    if linkedin_company_ids:
        li_kw_string = platform_keywords.get("linkedin", _DEFAULT_KEYWORDS)
        li_kw_list = _keyword_list(li_kw_string)
        if li_kw_list:
            print(f"  [scrape] LinkedIn company-targeted pass: {len(linkedin_company_ids)} companies × {len(li_kw_list)} keywords")
        for idx_kw, kw in enumerate(li_kw_list):
            try:
                jobs = _fetch_jobspy_platform(
                    "linkedin", kw, location, linkedin_company_results or _COMPANY_TARGET_RESULTS,
                    remote_only, seen_links, hours_old=hours_old, linkedin_company_ids=linkedin_company_ids,
                )
                for job in jobs:
                    job["via"] = "linkedin_company_page"   # lets the run funnel credit watched companies
                print(f"  [scrape] linkedin (company-targeted) '{kw}': {len(jobs)} new jobs")
                all_jobs.extend(jobs)
            except Exception as e:
                print(f"  [scrape] ERROR in LinkedIn company-targeted pass ('{kw}'): {e}")
            if idx_kw < len(li_kw_list) - 1:
                time.sleep(_DICE_API_DELAY)

    # --- Dice RSS feed — free, no quota, own keyword list/count ---
    dice_kw_string = platform_keywords.get("dice", _DEFAULT_KEYWORDS)
    if dice_kw_string.strip():
        try:
            dice_count = platform_counts.get("dice", _DEFAULT_RESULTS)
            print(f"  [scrape] Fetching from dice (count={dice_count})...")
            dice_jobs = _fetch_dice_rss(dice_kw_string, dice_count, seen_links,
                                         location=location, remote_only=remote_only,
                                         hours_old=hours_old)
            all_jobs.extend(dice_jobs)
        except Exception as e:
            print(f"  [scrape] Dice top-level error: {e}")
    else:
        print("  [scrape] dice: no keywords enabled — skipping")

    # --- Google Jobs via SerpAPI + JD enrichment ---
    gj_kw_string = platform_keywords.get("google_jobs", _DEFAULT_KEYWORDS)
    if gj_kw_string.strip():
        try:
            gj_count = platform_counts.get("google_jobs", _DEFAULT_RESULTS)
            print(f"  [scrape] Fetching from google_jobs (SerpAPI, count={gj_count})...")
            gj_jobs = _fetch_google_jobs(gj_kw_string, location, gj_count, seen_links,
                                          remote_only=remote_only, hours_old=hours_old)
            if gj_jobs:
                _enrich_google_job_descriptions(gj_jobs)
            all_jobs.extend(gj_jobs)
        except Exception as e:
            print(f"  [scrape] Google Jobs top-level error: {e}")
    else:
        print("  [scrape] google_jobs: no keywords enabled — skipping")

    # --- ATS boards (Greenhouse/Lever/Ashby/Workday) — curated company list ---
    if not include_ats:
        print("  [scrape] company career pages: switched off — skipping")
    else:
        try:
            from scraper.ats_scrape import fetch_ats_jobs
            ats_jobs = fetch_ats_jobs(ats_keywords if ats_keywords is not None else _DEFAULT_KEYWORDS,
                                       hours_old=hours_old, location=location)
            for job in ats_jobs:
                link = job.get("apply_link", "")
                if link and link not in seen_links:
                    seen_links.add(link)
                    all_jobs.append(job)
        except Exception as e:
            print(f"  [scrape] ATS boards top-level error: {e}")

    print(f"  [scrape] Total unique jobs across all platforms: {len(all_jobs)}")
    return all_jobs


if __name__ == "__main__":
    import json
    jobs = fetch_jobs(
        platform_keywords={"linkedin": "software engineer", "indeed": "software engineer",
                            "dice": "software engineer", "google_jobs": "software engineer"},
        platform_counts={"linkedin": 3, "indeed": 3, "dice": 3, "google_jobs": 3},
        ats_keywords="software engineer",
    )
    print(json.dumps(jobs[:2], indent=2, default=str))
