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

SIMPLE vs CUSTOM mode (Oct 2026 redesign — the Scraper tab became five plain
questions): in "simple" mode the per-platform keyword lists and counts above are
NOT used; every source searches the same enabled roles at a depth chosen from a
preset (light / normal / deep — see DEPTH_PRESETS), with starred ("focus") roles
searched deeper and quota/block-risk limits applied automatically. "custom" mode
is the original per-platform table, unchanged. A settings file saved before the
redesign has no "mode" key and loads as "custom", so an upgrade changes nothing
until the user picks a preset. `sources` switches whole sources on/off in BOTH
modes (including company career boards and the LinkedIn company-page pass).

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


# ── Simple mode ──────────────────────────────────────────────────────────────
MODES = ("simple", "custom")
DEPTHS = ("light", "normal", "deep")
SOURCES = ("linkedin", "indeed", "dice", "google_jobs", "company_boards", "linkedin_company_pages")
_JOBSPY_STYLE = ("linkedin", "indeed", "dice", "google_jobs")
_DEFAULT_SOURCES = {"linkedin": True, "indeed": True, "dice": True, "google_jobs": False,
                    "company_boards": True, "linkedin_company_pages": True}

# Results per role per run. Limits encode the real constraints so the user never
# has to: Google Jobs (SerpApi) spends 1 of 100 free monthly searches per role per
# run -> few roles, starred first; Indeed is kept shallow on purpose (an account
# was flagged after scraping from the same network) -> few roles, low counts;
# LinkedIn carries block risk -> moderate; Dice is free -> can go deeper.
DEPTH_PRESETS = {
    "light":  {"linkedin": {"per_role": 10, "focus": 20}, "indeed": {"per_role": 5, "max_roles": 2},
               "dice": {"per_role": 10}, "google_jobs": {"per_role": 10, "max_roles": 1},
               "linkedin_company_pages": {"per_role": 5}},
    "normal": {"linkedin": {"per_role": 20, "focus": 40}, "indeed": {"per_role": 10, "max_roles": 3},
               "dice": {"per_role": 20}, "google_jobs": {"per_role": 10, "max_roles": 2},
               "linkedin_company_pages": {"per_role": 10}},
    "deep":   {"linkedin": {"per_role": 35, "focus": 60}, "indeed": {"per_role": 15, "max_roles": 3},
               "dice": {"per_role": 40}, "google_jobs": {"per_role": 20, "max_roles": 3},
               "linkedin_company_pages": {"per_role": 15}},
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
        "mode": "simple",
        "depth": "normal",
        "sources": dict(_DEFAULT_SOURCES),
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
        if "mode" not in saved:
            # Saved before the Oct 2026 redesign: keep EXACTLY the old behaviour
            # (custom per-platform table) and derive which sources were in use.
            merged["mode"] = "custom"
            merged["sources"] = {
                **_DEFAULT_SOURCES,
                **{p: bool((merged["platforms"].get(p) or {}).get("keywords")) for p in _JOBSPY_STYLE},
            }
        else:
            merged["sources"] = {**_DEFAULT_SOURCES, **(saved.get("sources") or {})}
        if merged.get("mode") not in MODES:
            merged["mode"] = "custom"
        if merged.get("depth") not in DEPTHS:
            merged["depth"] = "normal"
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
    if not source_on(platform, settings):
        return ""
    if settings.get("mode") == "simple":
        return ",".join(simple_plan(settings).get(platform, {}))
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
    if settings.get("mode") == "simple":
        counts = list(simple_plan(settings).get(platform, {}).values())
        return max(counts) if counts else 0
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
    if not source_on(platform, settings):
        return {}
    if settings.get("mode") == "simple":
        return dict(simple_plan(settings).get(platform, {}))
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



# ── Simple-mode helpers ──────────────────────────────────────────────────────

def source_on(name: str, settings: dict = None) -> bool:
    """Whether a whole source is switched on (both modes). Unknown names count as on."""
    settings = settings if settings is not None else load()
    return bool((settings.get("sources") or _DEFAULT_SOURCES).get(name, True))


def roles(settings: dict = None) -> list[str]:
    """Enabled roles, starred ("focus") ones first, otherwise in the user's order."""
    settings = settings if settings is not None else load()
    enabled = [k for k in settings.get("keywords", []) if k.get("enabled") and k.get("value")]
    return [k["value"] for k in enabled if k.get("focus")] + [k["value"] for k in enabled if not k.get("focus")]


def focus_roles(settings: dict = None) -> list[str]:
    settings = settings if settings is not None else load()
    return [k["value"] for k in settings.get("keywords", []) if k.get("enabled") and k.get("focus") and k.get("value")]


def simple_plan(settings: dict = None) -> dict:
    """
    {platform: {role: results_per_run}} for the four search platforms in simple
    mode, from the chosen depth preset. A switched-off source gets {}.
      linkedin     every role; starred roles get the deeper "focus" count
      indeed       first `max_roles` roles (starred first), shallow
      dice         every role, one shared count
      google_jobs  first `max_roles` roles (starred first): each role costs one of
                   SerpApi's 100 free monthly searches per run
    """
    settings = settings if settings is not None else load()
    preset = DEPTH_PRESETS.get(settings.get("depth"), DEPTH_PRESETS["normal"])
    ordered = roles(settings)
    starred = set(focus_roles(settings))
    plan = {}
    for platform in _JOBSPY_STYLE:
        cfg = preset[platform]
        if not source_on(platform, settings) or not ordered:
            plan[platform] = {}
            continue
        chosen = ordered[: cfg["max_roles"]] if "max_roles" in cfg else ordered
        plan[platform] = {r: (cfg["focus"] if (r in starred and "focus" in cfg) else cfg["per_role"]) for r in chosen}
    return plan


def company_pass_results(settings: dict = None):
    """Results per role for the LinkedIn company-page pass: preset value in simple
    mode, None in custom mode (scrape.py then uses its env default)."""
    settings = settings if settings is not None else load()
    if settings.get("mode") != "simple":
        return None
    return DEPTH_PRESETS.get(settings.get("depth"), DEPTH_PRESETS["normal"])["linkedin_company_pages"]["per_role"]


_SOURCE_LABELS = {"linkedin": "LinkedIn", "indeed": "Indeed", "dice": "Dice", "google_jobs": "Google Jobs",
                  "company_boards": "company career pages", "linkedin_company_pages": "LinkedIn company pages"}


def _hours_label(hours: int) -> str:
    if hours % 168 == 0:
        weeks = hours // 168
        return "the last week" if weeks == 1 else f"the last {weeks} weeks"
    if hours % 24 == 0:
        days = hours // 24
        return "the last 24 hours" if days == 1 else f"the last {days} days"
    return f"the last {hours} hours"


def describe_plan(settings: dict = None, n_company_boards: int = 0, n_linkedin_pages: int = 0) -> dict:
    """
    Plain-English description of what the NEXT scheduled/manual run will do —
    the Scraper tab's summary banner. Structured fields plus one sentence, so the
    UI never has to re-derive the rules.
    """
    settings = settings if settings is not None else load()
    mode = settings.get("mode", "custom")
    platforms = {}
    for p in _JOBSPY_STYLE:
        counts = platform_keyword_counts(p, settings)
        if counts:
            platforms[p] = counts
    role_list = roles(settings)
    max_postings = sum(sum(c.values()) for c in platforms.values())
    company_pages_on = source_on("linkedin_company_pages", settings) and n_linkedin_pages > 0
    li_role_count = len(platforms.get("linkedin", {})) or len(role_list)
    if company_pages_on:
        per = company_pass_results(settings) or 10
        max_postings += per * li_role_count
    boards_on = source_on("company_boards", settings) and n_company_boards > 0

    where = [ _SOURCE_LABELS[p] for p in platforms ]
    watched = []
    if boards_on:
        watched.append(f"{n_company_boards} company career page{'s' if n_company_boards != 1 else ''}")
    if company_pages_on:
        watched.append(f"{n_linkedin_pages} LinkedIn company page{'s' if n_linkedin_pages != 1 else ''}")
    hours = int(settings.get("hours_old") or 24)
    level = "entry-level" if settings.get("entry_level_only") else "any level"
    remote = ", remote only" if settings.get("remote_only") else ""
    agencies = ", skipping recruiting agencies" if settings.get("exclude_recruiting_agencies") else ""
    looks = ", ".join(where) if where else "no job boards"
    if watched:
        looks += " + " + " and ".join(watched)
    sentence = (
        f"Searches {len(role_list)} role{'s' if len(role_list) != 1 else ''} on {looks}, "
        f"in {settings.get('location') or 'United States'}{remote}, posted in {_hours_label(hours)}, "
        f"{level}{agencies}. Keeps your best {settings.get('daily_cap', 25)} jobs scoring "
        f"{settings.get('min_score', 80)}+; the rest that pass go to the waitlist."
    )
    return {
        "mode": mode,
        "depth": settings.get("depth") if mode == "simple" else None,
        "roles": role_list,
        "focus_roles": focus_roles(settings),
        "platforms": platforms,
        "google_jobs_searches_per_run": len(platforms.get("google_jobs", {})),
        "company_boards": n_company_boards if boards_on else 0,
        "linkedin_company_pages": n_linkedin_pages if company_pages_on else 0,
        "max_postings_from_boards": max_postings,
        "hours_old": hours,
        "sentence": sentence,
    }


def validate(settings: dict) -> str | None:
    """Error message for an invalid settings payload, else None."""
    if settings.get("mode") not in (None, *MODES):
        return f"mode must be one of {list(MODES)}"
    if settings.get("depth") not in (None, *DEPTHS):
        return f"depth must be one of {list(DEPTHS)}"
    unknown = set((settings.get("sources") or {})) - set(SOURCES)
    if unknown:
        return f"unknown sources: {sorted(unknown)}"
    ms = settings.get("min_score")
    if ms is not None and not (0 <= int(ms) <= 100):
        return "min_score must be between 0 and 100"
    if int(settings.get("daily_cap") or 1) < 1:
        return "daily_cap must be at least 1"
    if int(settings.get("hours_old") or 1) < 1:
        return "hours_old must be at least 1"
    return None
