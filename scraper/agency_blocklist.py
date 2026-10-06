"""
scraper/agency_blocklist.py — company-name blocklist for staffing/recruiting
agencies (approximates Apify-style excludeRecruitingAgencies).

Name-based heuristic, not perfect: a posting from "Kforce" is filtered, but
a posting from an unlisted boutique staffing shop is not, and a real
employer whose name happens to contain a listed substring would be a false
positive (none currently do, but if one shows up, remove it from the list).

Curated default list ships in code; a persisted, user-editable override (same
JSON pattern as core/entries_store.py) lets false positives/negatives be
fixed without a code change — save() replaces the whole list.
"""

import os
import re
import json
import tempfile

_FILENAME = "agency_blocklist.json"

# Common US IT staffing/recruiting agencies that repost jobs on behalf of an
# undisclosed end client — matched as whole words/phrases (word-boundary),
# not raw substrings. Short/generic-sounding names are spelled out in full
# specifically to avoid false positives — e.g. "volt" alone would match
# "Voltage" or "Revolt"; "ust" alone would match "Trust" or "August".
_DEFAULT_AGENCIES = [
    "kforce", "teksystems", "robert half", "insight global", "apex systems",
    "randstad", "manpowergroup", "adecco", "motion recruitment", "beacon hill",
    "collabera", "mastech digital", "pyramid consulting", "cybercoders",
    "modis", "akkodis", "the judge group", "diverse lynx", "v-soft consulting",
    "zolon tech", "software guidance", "artech", "compunnel",
    "genesis10", "aditi consulting", "experis", "aerotek",
    "volt information sciences", "volt workforce solutions",
    "cynet systems", "signature consultants", "talentburst", "harvey nash",
    "tekshapers", "dexian", "hexaware", "ust global", "photon infotech",
    "synechron",
]


def _path() -> str:
    base = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
    return os.path.join(base, _FILENAME)


def load() -> list[str]:
    """Persisted override if one exists, else the curated default list."""
    path = _path()
    if not os.path.exists(path):
        return list(_DEFAULT_AGENCIES)
    try:
        with open(path) as f:
            agencies = json.load(f)
        return agencies if isinstance(agencies, list) else list(_DEFAULT_AGENCIES)
    except (json.JSONDecodeError, OSError):
        return list(_DEFAULT_AGENCIES)


def save(agencies: list[str]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(agencies, f, indent=2)
    os.replace(tmp, path)


def is_recruiting_agency(company_name: str) -> bool:
    if not company_name:
        return False
    lowered = company_name.strip().lower()
    for agency in load():
        pattern = r"\b" + re.escape(agency.strip().lower()) + r"\b"
        if re.search(pattern, lowered):
            return True
    return False
