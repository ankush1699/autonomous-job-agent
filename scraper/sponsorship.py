"""
scraper/sponsorship.py

Looks up H-1B visa sponsorship history for a company via SerpAPI.

Requires:
  SERPAPI_KEY env var (already used by scrape.py for Google Jobs)
  SPONSORSHIP_LOOKUP_ENABLED=true in .env (default: false — opt-in to avoid extra API calls)

Returns one of:
  "confirmed_h1b"  — strong positive signal (company appears in H-1B records / LCA filings)
  "likely"         — moderate positive signal
  "neutral"        — no clear signal either way
  "no_sponsorship" — explicit negative signal ("does not sponsor", "must be authorized")

Results are cached in-memory for the process lifetime so one company is only
looked up once per scheduler cycle even across multiple keyword searches.
"""

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
_ENABLED = os.getenv("SPONSORSHIP_LOOKUP_ENABLED", "false").lower() == "true"

# Per-process company→signal cache; resets on scheduler restart
_company_cache: dict[str, str] = {}

_POSITIVE_SIGNALS = [
    "h-1b sponsor",
    "h1b sponsor",
    "visa sponsor",
    "will sponsor",
    "sponsorship available",
    "h1b certified",
    "labor condition application",
    " lca ",
    "myvisajobs",
    "work authorization provided",
]

_NEGATIVE_SIGNALS = [
    "no sponsorship",
    "cannot sponsor",
    "not sponsor",
    "not able to sponsor",
    "must be authorized to work",
    "must be eligible to work",
    "does not sponsor",
    "unable to sponsor",
    "not eligible to sponsor",
    "sponsorship is not available",
    "we do not offer sponsorship",
]


def lookup_sponsorship(company_name: str) -> str:
    """
    Query SerpAPI for H-1B sponsorship signals for company_name.

    Returns "confirmed_h1b" | "likely" | "neutral" | "no_sponsorship".
    Always returns "neutral" if SPONSORSHIP_LOOKUP_ENABLED is not true or
    SERPAPI_KEY is not set.
    """
    if not _ENABLED or not _SERPAPI_KEY or not company_name:
        return "neutral"

    cleaned = company_name.strip().lower()
    if cleaned in _company_cache:
        return _company_cache[cleaned]

    try:
        from serpapi import GoogleSearch

        search = GoogleSearch({
            "q":       f'"{company_name}" H-1B visa sponsorship',
            "api_key": _SERPAPI_KEY,
            "num":     5,
            "hl":      "en",
        })
        # The serpapi package's own default (timeout=60000) is meant to read
        # as milliseconds but is passed straight into requests.get(timeout=...),
        # which takes SECONDS — that's a ~16.7 HOUR effective timeout, so a
        # stalled connection hangs the whole scrape cycle almost indefinitely.
        # Override the instance attribute post-construction (GoogleSearch's
        # own __init__ doesn't expose a way to pass this through).
        search.timeout = 15
        results = search.get_dict()
        organic = results.get("organic_results") or []

        # Combine title + snippet from top results into one text blob
        combined = " ".join(
            (r.get("title", "") + " " + r.get("snippet", "")).lower()
            for r in organic
        )

        for neg in _NEGATIVE_SIGNALS:
            if neg in combined:
                _company_cache[cleaned] = "no_sponsorship"
                print(f"  [sponsorship] {company_name}: no_sponsorship")
                return "no_sponsorship"

        positive_hits = sum(1 for pos in _POSITIVE_SIGNALS if pos in combined)
        if positive_hits >= 2:
            result = "confirmed_h1b"
        elif positive_hits == 1:
            result = "likely"
        else:
            result = "neutral"

        _company_cache[cleaned] = result
        if result != "neutral":
            print(f"  [sponsorship] {company_name}: {result}")
        return result

    except Exception as e:
        print(f"  [sponsorship] Lookup failed for '{company_name}': {e}")
        _company_cache[cleaned] = "neutral"
        return "neutral"
