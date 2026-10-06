"""
scraper/seen_jobs.py

Persistent store of already-processed job URLs, used by filter.py (Layer 1 dedup).

File: {OUTPUT_BASE_PATH}/seen_jobs.json
Schema: [{"url": "...", "seen_at": "<ISO timestamp>"}, ...]

Also maintains scored_jobs_cache.json for score caching (48-hour TTL).
Schema: {"<url>": {"score": int, "sub_scores": {...}, ..., "scored_at": "<ISO>"}}

Rules:
- Entries older than 30 days are trimmed on load, not on save.
- mark_seen() appends immediately and writes atomically via a tmp file.
- All functions are safe to call even if the file doesn't exist yet.
"""

import json
import os
import tempfile
import datetime

_OUTPUT_BASE = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
_SEEN_JOBS_FILENAME = "seen_jobs.json"
_MAX_AGE_DAYS = 30

_SCORE_CACHE_FILENAME = "scored_jobs_cache.json"
_SCORE_CACHE_TTL_HOURS = 48


def _seen_jobs_path() -> str:
    return os.path.join(
        os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes")),
        _SEEN_JOBS_FILENAME,
    )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _parse_dt(iso: str) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(iso)
    except Exception:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _is_fresh(entry: dict) -> bool:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=_MAX_AGE_DAYS)
    dt = _parse_dt(entry.get("seen_at", ""))
    # Make timezone-aware for comparison
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt >= cutoff


def load_seen() -> tuple[list[dict], set[str]]:
    """
    Load seen_jobs.json, trim entries older than 30 days.

    Returns:
        (entries, url_set) — entries is the trimmed list (for rewriting),
        url_set is a set of URL strings for O(1) lookup.
    """
    path = _seen_jobs_path()
    if not os.path.exists(path):
        return [], set()

    try:
        with open(path, "r") as f:
            raw: list = json.load(f)
    except (json.JSONDecodeError, OSError):
        return [], set()

    # Trim entries older than 30 days
    fresh = [e for e in raw if isinstance(e, dict) and _is_fresh(e)]
    trimmed = len(raw) - len(fresh)
    if trimmed > 0:
        print(f"  [seen_jobs] Trimmed {trimmed} entries older than {_MAX_AGE_DAYS} days")
        _write_entries(fresh)

    url_set = {e["url"] for e in fresh if e.get("url")}
    return fresh, url_set


def is_seen(url: str, url_set: set[str]) -> bool:
    return url.strip() in url_set


def mark_seen(url: str) -> None:
    """
    Append url to seen_jobs.json immediately.
    Trims stale entries on the way through.
    Thread-safe via atomic tmp-file write.
    """
    if not url or not url.strip():
        return

    path = _seen_jobs_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    entries, url_set = load_seen()
    if url.strip() in url_set:
        return  # already present

    entries.append({"url": url.strip(), "seen_at": _now_iso()})
    _write_entries(entries)


def _write_entries(entries: list[dict]) -> None:
    """Atomically write entries to seen_jobs.json."""
    path = _seen_jobs_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dir_ = os.path.dirname(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp_path, path)  # atomic on POSIX
    except Exception as e:
        print(f"  [seen_jobs] Write error: {e}")


# ---------------------------------------------------------------------------
# Score cache — avoids re-calling the LLM for jobs seen in recent cycles
# ---------------------------------------------------------------------------

def _score_cache_path() -> str:
    return os.path.join(
        os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes")),
        _SCORE_CACHE_FILENAME,
    )


def _load_score_cache() -> dict:
    """Load scored_jobs_cache.json, dropping entries older than TTL_HOURS."""
    path = _score_cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            raw: dict = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=_SCORE_CACHE_TTL_HOURS)
    return {
        url: entry
        for url, entry in raw.items()
        if _parse_dt(entry.get("scored_at", "")) >= cutoff
    }


def _save_score_cache(cache: dict) -> None:
    path = _score_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dir_ = os.path.dirname(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"  [seen_jobs] Score cache write error: {e}")


def get_cached_score(url: str) -> dict | None:
    """
    Return cached score dict for this URL if scored within last 48 hours.
    Keys: score, reasoning, jd_summary, seniority_level, role_type, sub_scores.
    Returns None if not cached or expired.
    """
    if not url or not url.strip():
        return None
    cache = _load_score_cache()
    return cache.get(url.strip())


def cache_score(url: str, score_data: dict) -> None:
    """
    Persist score results for a URL (TTL = 48h).
    score_data should contain at minimum: score, reasoning, jd_summary, sub_scores.
    """
    if not url or not url.strip():
        return
    cache = _load_score_cache()
    cache[url.strip()] = {**score_data, "scored_at": _now_iso()}
    _save_score_cache(cache)


# ---------------------------------------------------------------------------
# Job cache — stores full job dicts so manual trigger has access to full JD
# ---------------------------------------------------------------------------

_JOB_CACHE_FILENAME = "job_cache.json"
_JOB_CACHE_MAX_AGE_DAYS = 30


def _job_cache_path() -> str:
    return os.path.join(
        os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes")),
        _JOB_CACHE_FILENAME,
    )


def _load_job_cache() -> dict:
    """Load job_cache.json, dropping entries older than 30 days."""
    path = _job_cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            raw: dict = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=_JOB_CACHE_MAX_AGE_DAYS)
    return {
        url: entry
        for url, entry in raw.items()
        if _parse_dt(entry.get("cached_at", "")) >= cutoff
    }


def _save_job_cache(cache: dict) -> None:
    path = _job_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dir_ = os.path.dirname(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"  [seen_jobs] Job cache write error: {e}")


def save_job_to_cache(job: dict) -> None:
    """
    Save the full job dict (including description) keyed by apply_link.
    The manual trigger reads this to get the full JD for pipeline input.
    """
    url = (job.get("apply_link") or "").strip()
    if not url:
        return
    cache = _load_job_cache()
    cache[url] = {**job, "cached_at": _now_iso()}
    _save_job_cache(cache)


def load_job_from_cache(apply_link: str) -> dict | None:
    """Return the full job dict for this apply_link, or None if not cached / expired."""
    if not apply_link or not apply_link.strip():
        return None
    cache = _load_job_cache()
    return cache.get(apply_link.strip())
