"""
core/jd_cache.py — permanent, content-addressed cache for job descriptions.

Keyed by a hash of the *normalized JD text*, not the URL, so the same posting
scraped from two platforms (or pasted manually) is only ever processed once.

Entry schema (all fields optional except jd_text/created_at):
  {
    "jd_text": str,           # full JD as first seen
    "company": str, "title": str,
    "should_apply": {...},    # verdict from core.should_apply
    "keywords": [...],        # ATS keywords extracted by triage
    "created_at": iso, "updated_at": iso,
  }

No TTL by default — a verdict on a posting never goes stale within a job
search. Set JD_CACHE_TTL_DAYS to expire entries anyway.

File: {OUTPUT_BASE_PATH}/jd_cache.json (atomic writes).
"""

import os
import re
import json
import hashlib
import tempfile
import datetime
import threading

_CACHE_FILENAME = "jd_cache.json"
_WS_RE = re.compile(r"\s+")

# update_entry() does load-mutate-save with no atomicity across those three
# steps — a scrape cycle fires many should_apply calls back-to-back (each
# scored job writes its own verdict), and two writes landing close together
# can both load the same pre-write snapshot, then each save their own
# version, with whichever saves last silently discarding the other's entry.
# Real damage this caused: the cache dropped from ~282 entries to 6
# overnight from a single scrape cycle's worth of lost writes. A single
# process-wide lock serializes update_entry() calls — cheap (this is a
# small JSON file, not a hot path) and sufficient since this is a
# single-process, single-user app (no cross-process contention to guard).
_LOCK = threading.Lock()


def _cache_path() -> str:
    base = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
    return os.path.join(base, _CACHE_FILENAME)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def jd_hash(jd_text: str) -> str:
    """Content hash of the normalized JD: lowercased, whitespace-collapsed."""
    normalized = _WS_RE.sub(" ", jd_text.lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _load() -> dict:
    path = _cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    ttl_days = int(os.getenv("JD_CACHE_TTL_DAYS", "0"))
    if ttl_days > 0:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
        def _fresh(e):
            try:
                return datetime.datetime.fromisoformat(e.get("updated_at", e.get("created_at", ""))) >= cutoff
            except ValueError:
                return False
        cache = {k: v for k, v in cache.items() if _fresh(v)}
    return cache


def _save(cache: dict) -> None:
    path = _cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, path)


def get_entry(jd_text: str) -> dict | None:
    """Return the cached entry for this JD content, or None."""
    return _load().get(jd_hash(jd_text))


def update_entry(jd_text: str, **fields) -> dict:
    """
    Merge fields into the entry for this JD (creating it if new).
    Returns the updated entry.
    """
    with _LOCK:
        cache = _load()
        key = jd_hash(jd_text)
        entry = cache.get(key) or {"jd_text": jd_text, "created_at": _now_iso()}
        entry.update(fields)
        entry["updated_at"] = _now_iso()
        cache[key] = entry
        _save(cache)
        return entry


def stats() -> dict:
    cache = _load()
    verdicts = [e.get("should_apply", {}).get("proceed") for e in cache.values()]
    return {
        "entries": len(cache),
        "passed": sum(1 for v in verdicts if v is True),
        "rejected": sum(1 for v in verdicts if v is False),
        "path": _cache_path(),
    }
