"""
scraper/company_resolver.py — "type a company name, we work out how to watch it".

Replaces asking the user for board tokens, Workday URLs and numeric LinkedIn
organization ids (the old Scraper tab). Given a name (and optionally a careers
or LinkedIn URL), it probes the same PUBLIC endpoints the scraper already reads:

  1. the company's own job board — Greenhouse, Lever, Ashby (direct-to-company
     postings, preferred: they're the source of truth and win ranking ties);
  2. otherwise its public LinkedIn company page, for the numeric organization id
     jobspy's company filter needs (big employers like Amazon/Microsoft run none
     of the boards above).

Nothing is saved here. detect() returns CANDIDATES (with the board's own name
and how many jobs are open) and the user confirms one in the UI — a slug like
"meta" can belong to an unrelated company's board, so a human checks the match.

Workday has no name-based lookup (each tenant has its own URL), so it is only
handled when the user pastes the careers URL.
"""

import concurrent.futures
import re

import requests

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_TIMEOUT = 8
_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "plc", "gmbh", "the"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _norm_company(s: str) -> str:
    """'Meta Materials Inc' -> 'metamaterials'; 'Stripe, Inc.' -> 'stripe' (legal suffixes dropped)."""
    return "".join(w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _SUFFIXES)


def slug_candidates(name: str) -> list[str]:
    """'Scale AI, Inc.' -> ['scaleai', 'scale-ai', 'scale']"""
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _SUFFIXES]
    if not words:
        return []
    out = ["".join(words), "-".join(words), words[0]]
    seen, result = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            result.append(s)
    return result


def parse_url(url: str) -> dict | None:
    """Recognise a pasted careers / board / LinkedIn URL. Returns {source, identifier} or None."""
    u = (url or "").strip()
    patterns = [
        ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
        ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
        ("lever", r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
        ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)"),
        ("linkedin", r"linkedin\.com/company/([A-Za-z0-9_%-]+)"),
    ]
    for source, pat in patterns:
        m = re.search(pat, u)
        if m:
            return {"source": source, "identifier": m.group(1)}
    if re.search(r"\.myworkdayjobs\.com/", u):
        return {"source": "workday", "identifier": u if u.startswith("http") else "https://" + u}
    return None


def _get(url: str):
    try:
        return requests.get(url, headers=_UA, timeout=_TIMEOUT)
    except requests.RequestException:
        return None


def probe_greenhouse(slug: str) -> dict | None:
    meta = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    if meta is None or meta.status_code != 200:
        return None
    try:
        label = meta.json().get("name") or slug
    except ValueError:
        return None
    jobs = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    try:
        n = len(jobs.json().get("jobs", [])) if jobs is not None and jobs.status_code == 200 else None
    except ValueError:
        n = None
    return {"source": "greenhouse", "identifier": slug, "label": label, "open_jobs": n}


def probe_lever(slug: str) -> dict | None:
    r = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if r is None or r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return {"source": "lever", "identifier": slug, "label": slug, "open_jobs": len(data)}


def probe_ashby(slug: str) -> dict | None:
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if r is None or r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return {"source": "ashby", "identifier": slug, "label": slug, "open_jobs": len(data.get("jobs") or [])}


_LI_ID_RE = re.compile(r"urn:li:organization:(\d+)")
_LI_TITLE_RE = re.compile(r"<title>\s*([^<|]+?)\s*\|\s*LinkedIn", re.I)


def probe_linkedin(slug: str) -> dict | None:
    """Public company page -> numeric organization id. LinkedIn sometimes answers
    automated requests with HTTP 999; that is reported as 'not found' here, never
    retried or worked around."""
    r = _get(f"https://www.linkedin.com/company/{slug}/")
    if r is None or r.status_code != 200:
        return None
    m = _LI_ID_RE.search(r.text)
    if not m:
        return None
    t = _LI_TITLE_RE.search(r.text)
    return {"source": "linkedin", "identifier": int(m.group(1)), "label": t.group(1).strip() if t else slug,
            "open_jobs": None}


_PROBES = {"greenhouse": probe_greenhouse, "lever": probe_lever, "ashby": probe_ashby, "linkedin": probe_linkedin}


def detect(name: str, url: str = None) -> list[dict]:
    """
    Candidate ways to watch `name`, best first: company boards (most open jobs
    first, exact name match first), then the LinkedIn page. Each candidate:
    {source, identifier, label, open_jobs, name_matches}.
    """
    if url:
        parsed = parse_url(url)
        if not parsed:
            return []
        if parsed["source"] == "workday":
            return [{**parsed, "label": name or "Workday careers site", "open_jobs": None, "name_matches": True}]
        found = _PROBES[parsed["source"]](parsed["identifier"])
        return [{**found, "name_matches": (not name) or _norm_company(found["label"]) == _norm_company(name)
                 or _norm(str(found["identifier"])) == _norm_company(name)}] if found else []

    slugs = slug_candidates(name)
    if not slugs:
        return []
    jobs = [(src, slug) for slug in slugs for src in ("greenhouse", "lever", "ashby")]
    jobs += [("linkedin", slug) for slug in slugs[:2]]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_PROBES[src], slug): (src, slug) for src, slug in jobs}
        for fut in concurrent.futures.as_completed(futures):
            try:
                r = fut.result()
            except Exception:
                r = None
            if r:
                results.append(r)

    # one entry per (source, identifier)
    unique = {(r["source"], str(r["identifier"])): r for r in results}.values()
    target = _norm_company(name)
    out = []
    for r in unique:
        # Whole-name match only (a prefix match let "Meta" claim "Meta Materials Inc").
        # Lever/Ashby expose no company name, so their slug is compared instead.
        r["name_matches"] = bool(target) and (
            _norm_company(r["label"]) == target
            or (r["source"] in ("lever", "ashby") and _norm(str(r["identifier"])) == target))
        out.append(r)
    out.sort(key=lambda r: (r["source"] == "linkedin", not r["name_matches"], -(r["open_jobs"] or 0)))
    return out
