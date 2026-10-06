"""
scraper/scraper_settings.py — live-editable scrape search config, so the
web UI's Scraper tab can change keywords/location/remote/entry-level/agency
filtering without editing .env or restarting the server.

Keywords live in ONE master pool ("keywords": [{"value","enabled"}, ...]) —
this is what makes them select/deselect in the UI, and what lets a keyword
be re-enabled later without retyping it. Each PLATFORM then selects its own
subset of that pool plus its own per-keyword result count, because
different platforms warrant different depth: SerpApi (Google Jobs) has a
hard 100/month free quota (one unit per keyword per cycle, regardless of
count requested), so it wants few keywords; Dice is free with no quota risk
so it can afford full depth; LinkedIn scrapes are block-risk, so shallower.

LinkedIn is the one exception to "one shared count per platform": its
"keywords" list holds {"value","count"} objects instead of plain strings,
so each title can be scraped to a different depth (e.g. "Software Engineer"
40 vs "AI Engineer" 10) — the daily_cap's ranking is global and score-based,
not keyword-aware, so a broad title that happens to score high across many
generic postings can otherwise crowd out a narrower title's candidates
entirely; per-keyword counts are the lever to deliberately favor one title's
representation over another. Other platforms keep the simpler shared-count
shape (a plain list of strings + one "count") since this was requested
specifically for LinkedIn — see platform_keyword_counts() below, which
normalizes both shapes into one {keyword: count} dict for callers.

daily_cap: the scrape cycle scores everything that passes the earlier
filters, then keeps only the top-N by score (ATS-sourced and confirmed-H1B
jobs weighted higher as a tie-break, since those correlate with actually
hearing back) — everything beyond the cap is written as a hidden waitlist
(cap_missed=True) in entries mode rather than discarded, and every scored job
is marked "seen" so it's never re-scraped and re-billed on a future cycle.

min_score: the ONE apply threshold (0-100). core.should_apply.min_score()
reads it from here so the verdict's `proceed` flag, the scraper's candidate
cut, notifications and /api/meta all use the same number — previously two
env vars (SHOULD_APPLY_MIN_SCORE for core, MIN_ALIGNMENT_SCORE for the
scraper) were read independently and only agreed by coincidence.

Same JSON-persisted pattern as core/entries_store.py and friends. Read fresh
on every scrape cycle (scraper/scheduler.py) and every filter pass
(scraper/filter.py) — never frozen at import time — so a change here takes
effect on the very next cycle, no restart needed.
"""

import os
import json
import tempfile

_FILENAME = "scraper_settings.json"

_DEFAULT_KEYWORDS = [
    "Software Engineer", "Full Stack Engineer", "Backend Engineer",
    "Frontend Engineer", "AI Engineer",
]

_PLATFORMS = ["linkedin", "indeed", "dice", "google_jobs"]

# Per-platform defaults reflect real constraints: SerpApi has a hard 100/mo
# free quota (1 keyword search = 1 unit, so keep this list short); LinkedIn
# scrapes carry block risk (moderate depth). Indeed is kept deliberately
# SHALLOW, not "safe to search harder" — jobspy's Indeed scraping is
# unauthenticated (no login/cookie, unlike LinkedIn) but Indeed's own bot
# detection appears to correlate automated traffic with accounts by IP: a
# real account got flagged ("Job Seeker Guidelines" violation, profile
# hidden from employers) after scraping from the same network as that
# account's normal logged-in browsing. Kept active at low volume rather
# than disabled, per explicit user choice — not risk-free either way.
# ATS boards aren't listed here — they have no per-keyword API cost, so
# they always match against the full master pool.
_PLATFORM_DEFAULTS = {
    "linkedin":    {"keywords": [{"value": "Software Engineer", "count": 15},
                                  {"value": "AI Engineer", "count": 15}]},
    "indeed":      {"keywords": ["Software Engineer"], "count": 10},
    "dice":        {"keywords": list(_DEFAULT_KEYWORDS), "count": 25},
    "google_jobs": {"keywords": ["Software Engineer"], "count": 20},
}


def _env_defaults() -> dict:
    """Computed lazily (not at import) so a .env change before first load()
    is picked up — matches every other env-backed setting in this codebase."""
    return {
        "keywords": [{"value": kw, "enabled": True} for kw in _DEFAULT_KEYWORDS],
        "platforms": {p: dict(cfg) for p, cfg in _PLATFORM_DEFAULTS.items()},
        "location": os.getenv("JOB_SEARCH_LOCATION", "United States"),
        "remote_only": os.getenv("JOB_SEARCH_REMOTE", "false").lower() == "true",
        "hours_old": int(os.getenv("JOB_SEARCH_HOURS_OLD", "24")),
        "entry_level_only": os.getenv("SCRAPER_ENTRY_LEVEL_ONLY", "true").lower() == "true",
        "exclude_recruiting_agencies": os.getenv("SCRAPER_EXCLUDE_RECRUITING_AGENCIES", "false").lower() == "true",
        "daily_cap": int(os.getenv("SCRAPER_DAILY_CAP", "25")),
        "min_score": _env_min_score(),
    }


def _env_min_score() -> int:
    """The apply threshold's env default lives in core (SHOULD_APPLY_MIN_SCORE,
    with the older MIN_ALIGNMENT_SCORE honored as a fallback). Lazy import:
    core.should_apply reads THIS module lazily too (min_score()), so neither
    side imports the other at module load."""
    from core.should_apply import _env_min_score as core_env_min_score
    return core_env_min_score()


def _path() -> str:
    base = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
    return os.path.join(base, _FILENAME)


def load() -> dict:
    """Persisted settings merged over env-derived defaults (so adding a new
    setting later doesn't break an existing saved file missing that key)."""
    defaults = _env_defaults()
    path = _path()
    if not os.path.exists(path):
        return defaults
    try:
        with open(path) as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            return defaults
        merged = {**defaults, **saved}
        # Shallow-merge platforms too, so an old saved file missing a newly
        # added platform key still gets that platform's default instead of
        # silently losing it (e.g. google_jobs added after linkedin/indeed).
        merged["platforms"] = {**defaults["platforms"], **(saved.get("platforms") or {})}
        return merged
    except (json.JSONDecodeError, OSError):
        return defaults


def save(settings: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, path)


def enabled_keywords_string(settings: dict = None) -> str:
    """
    Comma-joined values of every ENABLED master-pool keyword — used by
    scraper/ats_scrape.py, which matches against the whole pool (no per-
    keyword API cost, so no reason to restrict it per-platform).
    """
    settings = settings if settings is not None else load()
    enabled = [k["value"] for k in settings.get("keywords", []) if k.get("enabled") and k.get("value")]
    return ",".join(enabled)


def _kw_value(entry) -> str:
    """A platform's keyword list entries are either a plain string (most
    platforms) or a {"value","count"} dict (LinkedIn's per-keyword-count
    shape) — this normalizes either to just the keyword text."""
    return entry["value"] if isinstance(entry, dict) else entry


def platform_keywords_string(platform: str, settings: dict = None) -> str:
    """
    Comma-joined keywords configured for ONE platform, filtered to only
    those still enabled in the master pool (so disabling a keyword globally
    removes it everywhere without having to edit every platform's list too).
    """
    settings = settings if settings is not None else load()
    enabled_values = {k["value"] for k in settings.get("keywords", []) if k.get("enabled")}
    platform_cfg = (settings.get("platforms") or {}).get(platform, {})
    selected = [_kw_value(kw) for kw in platform_cfg.get("keywords", []) if _kw_value(kw) in enabled_values]
    return ",".join(selected)


def platform_count(platform: str, settings: dict = None) -> int:
    """
    Single representative count for a platform — used for the CLI/daemon
    startup banner only. For LinkedIn (per-keyword counts), this is the
    average across its keywords, purely for that one-line display; the
    actual scrape uses platform_keyword_counts() below, not this.
    """
    settings = settings if settings is not None else load()
    platform_cfg = (settings.get("platforms") or {}).get(platform, {})
    keywords = platform_cfg.get("keywords", [])
    if keywords and isinstance(keywords[0], dict):
        counts = [int(kw.get("count") or 20) for kw in keywords]
        return round(sum(counts) / len(counts)) if counts else 20
    return int(platform_cfg.get("count") or _PLATFORM_DEFAULTS.get(platform, {}).get("count", 20))


def platform_keyword_counts(platform: str, settings: dict = None) -> dict:
    """
    {keyword: count} for every ENABLED keyword configured on one platform.
    Platforms with a shared count (all except LinkedIn) get that same value
    repeated per keyword; LinkedIn's per-keyword counts pass through as-is.
    This is what scraper/scrape.py's fetch_jobs() actually scrapes with —
    platform_count() above is display-only.
    """
    settings = settings if settings is not None else load()
    enabled_values = {k["value"] for k in settings.get("keywords", []) if k.get("enabled")}
    platform_cfg = (settings.get("platforms") or {}).get(platform, {})
    keywords = platform_cfg.get("keywords", [])
    shared_count = platform_cfg.get("count") or _PLATFORM_DEFAULTS.get(platform, {}).get("count", 20)
    result = {}
    for kw in keywords:
        value = _kw_value(kw)
        if value not in enabled_values:
            continue
        result[value] = int(kw.get("count") or 20) if isinstance(kw, dict) else int(shared_count)
    return result
