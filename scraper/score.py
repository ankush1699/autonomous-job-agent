"""
scraper/score.py

Scores each filtered job against the candidate profile using core/should_apply.py
(regex red flags → JD cache → cheap-tier LLM rubric — the same engine every
entry mode shares).
Adds "score", "reasoning", "jd_summary", and "salary" fields to each job dict.

Sequential with a 5-second delay between calls — never parallel.
Score results are ALSO cached in scored_jobs_cache.json (48h TTL) here, on top
of core.jd_cache's permanent cache, to avoid redundant LLM calls on
crash-recovery and across multi-keyword cycles within one scrape run.
"""

import os
import re
import sys
import time

# Ensure repo root is on path for core/ imports
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.should_apply import evaluate as score_jd, RUBRIC_VERSION, min_score as core_min_score
from core.profile_cache import load_profile_cache
from scraper.seen_jobs import get_cached_score, cache_score

# The apply threshold is NOT read here — core.should_apply.min_score() is the
# single source (UI-editable, env-defaulted); see partition_by_score().
_DELAY_BETWEEN_CALLS = 5  # seconds — never parallel

# Matches patterns like: $120,000/year  $80k–$100k  $45/hour  120k-150k
_SALARY_RE = re.compile(
    r'\$\s*\d[\d,]*(?:\.\d+)?(?:\s*[kK])?'
    r'(?:\s*(?:[-–—]|to)\s*\$?\s*\d[\d,]*(?:\.\d+)?(?:\s*[kK])?)?'
    r'(?:\s*(?:per year|/year|/yr|a year|annually|per hour|/hour|/hr))?'
    r'|\d{2,3}(?:,\d{3})?(?:\s*[kK])\s*[-–]\s*\d{2,3}(?:,\d{3})?(?:\s*[kK])',
    re.IGNORECASE,
)


def _extract_salary(jd: str) -> str:
    """Extract first salary range from JD text. Returns '' if none found."""
    matches = _SALARY_RE.findall(jd[:3000])
    return matches[0].strip() if matches else ""


def score_single_job(job: dict, profile: str = None) -> None:
    """
    Score one job dict in-place.
    Adds: score, reasoning, jd_summary, seniority_level, role_type, sub_scores, salary.
    On failure sets score=0 and logs the error — never raises.
    Checks scored_jobs_cache.json before calling the LLM.
    """
    if profile is None:
        profile = load_profile_cache()

    # Salary extraction — regex only, no LLM needed
    job["salary"] = _extract_salary(job.get("description", ""))

    # Check score cache before calling the LLM
    url = (job.get("apply_link") or "").strip()
    if url:
        cached = get_cached_score(url)
        # This URL-keyed 48h cache sits in FRONT of core.jd_cache and used to
        # bypass its rubric-version check entirely — a verdict scored under
        # an older rubric within the last 48h would be served here as-is.
        # Same rule as core.should_apply's cache read: a version mismatch
        # (or a pre-versioning entry) is a miss, not a hit.
        if cached and cached.get("rubric_version") != RUBRIC_VERSION:
            print(f"  [score] cached score for {job.get('company','?')} is from an older rubric — re-scoring")
            cached = None
        if cached:
            job["rubric_version"]  = cached.get("rubric_version")
            job["score"]           = cached.get("score", 0)
            job["reasoning"]       = cached.get("reasoning", "")
            job["jd_summary"]      = cached.get("jd_summary", "")
            job["seniority_level"] = cached.get("seniority_level", "unspecified")
            job["role_type"]       = cached.get("role_type", "")
            job["sub_scores"]      = cached.get("sub_scores", {})
            job["tailoring_recommended"] = cached.get("tailoring_recommended", True)
            job["tailoring_reasoning"]   = cached.get("tailoring_reasoning", "")
            print(
                f"  [score] CACHE HIT {job.get('company','?')} — "
                f"{job.get('title','?')}: {job['score']}/100"
            )
            return

    try:
        result = score_jd(
            job.get("description", ""), company=job.get("company", ""), title=job.get("title", ""),
            sponsorship_signal=job.get("sponsorship_signal"),
        )
        job["score"]           = result["score"]
        job["reasoning"]       = result["reasoning"]
        job["jd_summary"]      = result["jd_summary"]
        job["seniority_level"] = result.get("seniority_level", "unspecified")
        job["role_type"]       = result.get("role_type", "")
        job["sub_scores"]      = result.get("sub_scores", {})
        job["tailoring_recommended"] = result.get("tailoring_recommended", True)
        job["tailoring_reasoning"]   = result.get("tailoring_reasoning", "")
        job["rubric_version"]        = result.get("rubric_version")
        sub = job["sub_scores"]
        print(
            f"  [score] {job.get('company','?')} — {job.get('title','?')}: {job['score']}/100 "
            f"(tech={sub.get('tech','?')} sen={sub.get('seniority','?')} "
            f"role={sub.get('role_type','?')} spons={sub.get('sponsorship_bonus','?')}) "
            f"[{job['seniority_level']}]"
        )
        # Persist score so a restart or re-appearance skips the LLM call
        if url:
            cache_score(url, {
                "score":           job["score"],
                "reasoning":       job["reasoning"],
                "jd_summary":      job["jd_summary"],
                "seniority_level": job["seniority_level"],
                "role_type":       job["role_type"],
                "sub_scores":      job["sub_scores"],
                "tailoring_recommended": job["tailoring_recommended"],
                "tailoring_reasoning":   job["tailoring_reasoning"],
                "rubric_version":        job["rubric_version"],
            })
    except Exception as e:
        print(f"  [score] ERROR scoring {job.get('company','?')} — {job.get('title','?')}: {e}")
        job["score"]     = 0
        job["reasoning"] = f"Scoring failed: {e}"
        job["jd_summary"] = ""
        job["tailoring_recommended"] = True
        job["tailoring_reasoning"] = ""
        job["rubric_version"] = None


def score_jobs(jobs: list[dict]) -> list[dict]:
    """
    Score each job and attach score/reasoning/jd_summary in-place.

    Returns:
        The same list with score fields added to each job dict.
        Jobs that fail scoring get score=0 and an error note in reasoning.
    """
    profile = load_profile_cache()

    for i, job in enumerate(jobs):
        desc = job.get("description", "")
        try:
            result = score_jd(desc, company=job.get("company", ""), title=job.get("title", ""))
            job["score"] = result["score"]
            job["reasoning"] = result["reasoning"]
            job["jd_summary"] = result["jd_summary"]
            job["seniority_level"] = result.get("seniority_level", "unspecified")
            job["role_type"] = result.get("role_type", "")
            job["sub_scores"] = result.get("sub_scores", {})
            sub = job["sub_scores"]
            print(
                f"  [score] {i+1}/{len(jobs)} {job['company']} — {job['title']}: "
                f"{job['score']}/100  "
                f"(tech={sub.get('tech','?')} sen={sub.get('seniority','?')} "
                f"role={sub.get('role_type','?')} spons={sub.get('sponsorship_bonus','?')}) "
                f"[{job['seniority_level']}]"
            )
        except Exception as e:
            print(f"  [score] ERROR scoring {job['company']} — {job['title']}: {e}")
            job["score"] = 0
            job["reasoning"] = f"Scoring failed: {e}"
            job["jd_summary"] = ""

        if i < len(jobs) - 1:
            time.sleep(_DELAY_BETWEEN_CALLS)

    return jobs


def partition_by_score(jobs: list[dict], min_score: int | None = None) -> tuple[list[dict], list[dict]]:
    """
    Split scored jobs into high-match (>= min_score) and low-match.
    min_score defaults to the effective apply threshold (core.should_apply.min_score()).

    Returns:
        (high_match, low_match) sorted by score descending within each group.
    """
    if min_score is None:
        min_score = core_min_score()
    high = sorted([j for j in jobs if j.get("score", 0) >= min_score],
                  key=lambda j: j["score"], reverse=True)
    low = sorted([j for j in jobs if j.get("score", 0) < min_score],
                 key=lambda j: j["score"], reverse=True)
    print(f"  [score] {len(high)} high-match (>={min_score}), {len(low)} below threshold")
    return high, low
