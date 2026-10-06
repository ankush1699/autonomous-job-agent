"""
core/entries_store.py — durable JSON-backed storage for server.py's ENTRIES.

server.py previously kept ENTRIES as a plain in-memory dict, which meant every
process restart (including `uvicorn --reload` triggering on every file save
during development — which happens constantly) silently wiped every
application you'd added. This makes the in-memory dict a write-through cache
instead: every mutation is persisted immediately, and the file is loaded back
on startup so the Applications list survives restarts.

Same atomic-write pattern as core/jd_cache.py (temp file + os.replace) for
crash safety, and same OUTPUT_BASE_PATH convention for where it lives.
"""

import os
import json
import tempfile

_FILENAME = "entries.json"


def path() -> str:
    """Public accessor for the storage path — used for startup logging."""
    return _path()


def _path() -> str:
    base = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
    return os.path.join(base, _FILENAME)


def load() -> dict:
    """
    Load persisted entries, sanitizing any that were mid-flight when the
    process last stopped — a "scoring" or "running" entry with no live
    background thread behind it (because the process just started) would
    otherwise show a permanently stuck spinner forever.
    """
    path = _path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            entries: dict = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    for entry in entries.values():
        if entry.get("score_status") == "scoring":
            entry["score_status"] = "error"
            entry["score_error"] = "Interrupted by a server restart — re-add this JD to retry."
        if entry.get("apply_status") == "running":
            entry["apply_status"] = "error"
            entry["apply_error"] = "Interrupted by a server restart — click Generate again."
    return entries


def save(entries: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, path)
