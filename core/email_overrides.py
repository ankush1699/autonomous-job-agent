"""
core/email_overrides.py — per-company email override for generated resumes.

Some companies get a dedicated email alias (e.g. tracking which postings
came from where, or a company-specific inbox). This is a small, persisted
list of {pattern, email} rules — pattern is matched case-insensitively as a
substring of the company name (e.g. pattern "microsoft" matches "Microsoft",
"Microsoft Corporation", "Microsoft - Redmond"). First match wins.

Same atomic-write JSON pattern as core/entries_store.py, same
OUTPUT_BASE_PATH convention for where it lives, so it survives restarts.
"""

import os
import json
import tempfile

_FILENAME = "email_overrides.json"


def _path() -> str:
    base = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
    return os.path.join(base, _FILENAME)


def load() -> list[dict]:
    """Return the list of {pattern, email} rules, or [] if none configured."""
    path = _path()
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            rules = json.load(f)
        return rules if isinstance(rules, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save(rules: list[dict]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(rules, f, indent=2)
    os.replace(tmp, path)


def get_email_override(company_name: str) -> str | None:
    """First matching rule's email, or None if no rule matches company_name."""
    if not company_name:
        return None
    lowered = company_name.lower()
    for rule in load():
        pattern = (rule.get("pattern") or "").strip().lower()
        if pattern and pattern in lowered:
            return rule.get("email") or None
    return None
