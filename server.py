"""
server.py — web API over the existing core engine.

Wraps modes/manual.py, modes/scraper.py, and core/should_apply.py behind
HTTP endpoints so the pipeline can run from a browser instead of a terminal
+ copy-pasting into Sheets. No new business logic lives here — every request
delegates straight into the same code apply.py already uses.

Auth: a single shared-secret bearer token (APP_PASSWORD env var). This is a
personal single-user tool making paid Anthropic API calls — the point of the
gate is to stop a stranger who finds the URL from burning your credits, not
to be enterprise auth. Every mutating/paid endpoint requires it.

Run locally:
    APP_PASSWORD=changeme uvicorn server:app --reload --port 8000

Long-running work (the tailoring pipeline, a scrape cycle) is dispatched to a
background thread and tracked in an in-memory store, polled via GET endpoints.
State does not survive a process restart — acceptable for a single-user tool;
the finalizer's own output-folder files remain the durable record either way.
"""

import os
import re
import sys
import time
import uuid
import logging
import threading
import datetime
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("resume_agent")

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.abspath(__file__))
for p in (ROOT, os.path.join(ROOT, "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")]

app = FastAPI(title="Resume Agent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_auth(authorization: Optional[str] = Header(None), token: Optional[str] = None):
    """
    Accepts the token via the Authorization header (used by all fetch() calls)
    or a `token` query param (needed for plain <a href> downloads, which can't
    set custom headers). Both are checked against the same shared secret.
    """
    if not APP_PASSWORD:
        raise HTTPException(500, "Server misconfigured: APP_PASSWORD is not set.")
    if authorization == f"Bearer {APP_PASSWORD}":
        return
    if token == APP_PASSWORD:
        return
    raise HTTPException(401, "Invalid or missing credentials.")


# ---------------------------------------------------------------------------
# In-memory entry tracking (single-process; fine for a single-user tool)
#
# One ENTRIES row per JD you add — created immediately, scored in the
# background, and later (independently, whenever you choose) generated into a
# tailored resume/CL in the background too. This is deliberately ONE model
# covering both phases rather than two separate "score" and "run" trackers,
# so the frontend can show one persistent, non-blocking list: add a JD, see
# it appear right away, watch its score fill in, click Generate whenever
# you're ready — without the add-a-JD form ever blocking on anything.
# ---------------------------------------------------------------------------
from core import entries_store, jd_cache  # noqa: E402

ENTRIES: dict[str, dict] = entries_store.load()
SCRAPE_RUNS: dict[str, dict] = {}
_SAFE_FILENAME_RE = re.compile(r'^[\w.\- ]+$')
logger.info(f"[entries] loaded {len(ENTRIES)} persisted entries from {entries_store.path()}")


def _find_duplicate_entry(jd_text: str, apply_link: str = "") -> dict | None:
    """
    An entry counts as a duplicate — regardless of its current status
    (still scoring, scored, applied, whatever) — if either:
      1. Its JD content hash matches (core.jd_cache's same normalized-text
         hash used for should_apply's own cache, so "already scored before"
         and "already an entry" mean the same thing), or
      2. Its apply_link matches (catches the case where the JD text differs
         slightly between a scraped/truncated version and a manually pasted
         full version of the SAME posting).
    Checks every entry regardless of source (manual or scraper) — this is
    what makes it bidirectional: a manual paste of an already-scraped
    posting is caught, and vice versa.
    """
    target_hash = jd_cache.jd_hash(jd_text) if jd_text else None
    target_link = (apply_link or "").strip()
    for entry in ENTRIES.values():
        if target_hash and entry.get("jd_hash") == target_hash:
            return entry
        if target_link and (entry.get("apply_link") or "").strip() == target_link:
            return entry
    return None


def _duplicate_note(existing: dict) -> str:
    """
    Human-readable warning attached to a NEW entry that matched an existing
    one (by JD hash or apply_link). Used instead of silently discarding the
    new entry — job boards like LinkedIn mint a fresh job ID for a repost of
    the same JD text, so a match here is a strong signal but not certain
    enough to auto-delete without the user's own eyes on it.
    """
    when = (existing.get("created_at") or "")[:10] or "an earlier date"
    if existing.get("applied"):
        status = "you marked it applied"
    else:
        status = f"status: {existing.get('apply_status', 'idle')}"
    return (
        f"Possible duplicate — matches an entry from {when} "
        f"({existing.get('company', '?')} — {existing.get('title', '?')}, {status}). "
        f"Check before applying again."
    )

# Each JD add / rescore spawns its own thread, so adding several JDs at once
# (or clicking "Retry all failed") can fire many should_apply LLM calls
# simultaneously. scraper/score.py deliberately scores sequentially with a
# delay between calls ("never parallel") specifically to avoid tripping
# provider rate limits — this lock + minimum spacing gives the web UI the
# same discipline instead of bursting every call at once.
_SCORE_LOCK = threading.Lock()
_SCORE_MIN_INTERVAL = 5  # seconds between should_apply LLM calls, matches scraper/score.py
_last_score_call_at = 0.0


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _score_entry(entry_id: str):
    global _last_score_call_at
    from core.should_apply import evaluate
    entry = ENTRIES[entry_id]
    try:
        with _SCORE_LOCK:
            elapsed = time.monotonic() - _last_score_call_at
            if elapsed < _SCORE_MIN_INTERVAL:
                time.sleep(_SCORE_MIN_INTERVAL - elapsed)
            try:
                verdict = evaluate(entry["jd"], company=entry["company"], title=entry["title"])
            finally:
                _last_score_call_at = time.monotonic()
        entry.update({
            "score_status": "scored",
            "score": verdict.get("score"),
            "proceed": verdict.get("proceed"),
            "stage": verdict.get("stage"),
            "reasoning": verdict.get("reasoning", ""),
            "red_flags": verdict.get("red_flags", []),
            # Separate from score/proceed — see should_apply.py's
            # tailoring_recommended docstring for why these two questions
            # ("should I apply" vs "does my base resume already cover this")
            # deliberately don't share a number.
            "tailoring_recommended": verdict.get("tailoring_recommended", True),
            "tailoring_reasoning": verdict.get("tailoring_reasoning", ""),
            # Which rubric produced this number — the UI flags entries scored
            # under an older version so stale scores aren't read as current.
            "rubric_version": verdict.get("rubric_version"),
        })
    except Exception as e:
        logger.exception(f"[score] entry {entry_id} failed for company={entry['company']!r}")
        entry.update({"score_status": "error", "score_error": f"{type(e).__name__}: {e}"})
    finally:
        entries_store.save(ENTRIES)


def _generate_entry(entry_id: str, generate_cover_letter: bool):
    from modes.manual import run as run_manual
    entry = ENTRIES[entry_id]
    try:
        result = run_manual(entry["company"], entry["title"], entry["jd"],
                             generate_resume=True, generate_cover_letter=generate_cover_letter)

        # The generation pipeline runs as one opaque synchronous call — it
        # can't be safely force-killed mid-LLM-call, so "Stop" (see
        # cancel_generate below) doesn't actually interrupt this thread. It
        # just marks the entry "cancelled" and moves on. If that happened
        # while we were running, honor it: don't clobber "cancelled" with a
        # "completed" the user already told the UI to forget about.
        if entry.get("apply_status") == "cancelled":
            print(f"  [generate] entry {entry_id} finished after being cancelled — discarding result")
            return

        output_folder = result.get("output_folder", "")
        files = []
        if output_folder and os.path.isdir(output_folder):
            files = sorted(f for f in os.listdir(output_folder) if f.lower().endswith(".pdf"))
        entry.update({
            "apply_status": "completed",
            "pipeline_status": result.get("pipeline_status", ""),
            "output_folder": output_folder,
            "files": files,
            "apply_finished_at": _now(),
        })
        # The graph re-runs its own should_apply gate internally (cache-hit,
        # so free) regardless of what the UI already showed — if that gate
        # says no, nothing gets generated even though apply_status is
        # "completed". Reflect that honestly rather than implying success.
        if not result.get("proceed", True):
            entry["apply_status"] = "skipped_by_gate"
    except Exception as e:
        if entry.get("apply_status") == "cancelled":
            print(f"  [generate] entry {entry_id} errored after being cancelled — discarding: {e}")
            return
        logger.exception(f"[generate] entry {entry_id} failed for company={entry['company']!r}")
        entry.update({
            "apply_status": "error",
            "apply_error": f"{type(e).__name__}: {e}",
            "apply_finished_at": _now(),
        })
    finally:
        entries_store.save(ENTRIES)


def _new_entry_from_scraped_job(job: dict, created_at: str = None) -> dict:
    """
    Build an ENTRIES row from an already-scored scraped job dict (see
    scraper/score.py). Unlike create_entry(), this is created already
    "scored" — the LLM call already happened during the scrape cycle, so
    there's nothing left to do in the background.

    created_at defaults to now (a live scrape cycle scoring this job right
    now), but callers importing HISTORICAL jobs (e.g. from the sheet's own
    "Date Added" column) should pass the real original timestamp — otherwise
    every backfilled job would misleadingly show up as "added today" in the
    date filter.
    """
    from core.should_apply import min_score as _min_score
    entry_id = uuid.uuid4().hex[:12]
    score = job.get("score", 0)
    description = job.get("description", "")
    return {
        "id": entry_id, "company": job.get("company", "?"), "title": job.get("title", "?"),
        "jd": description, "jd_hash": jd_cache.jd_hash(description) if description else None,
        "apply_link": (job.get("apply_link") or "").strip(),
        "source": "scraper",
        # Scraped facts that used to be dropped on the floor here — the
        # scraper normalizes all of these (and score.py runs a dedicated
        # salary regex), then the entry never carried them, so the UI could
        # not show whether a job was remote, where it was, or how old.
        "platform": job.get("platform"),
        "location": job.get("location") or "",
        "is_remote": bool(job.get("is_remote")),
        "date_posted": job.get("date_posted") or "",
        "salary": job.get("salary") or "",
        "created_at": created_at or _now(),
        "score_status": "scored", "score": score, "proceed": score >= _min_score(),
        "stage": "llm", "reasoning": job.get("reasoning", ""), "red_flags": [], "score_error": None,
        "rubric_version": job.get("rubric_version"),
        # Cleared the score bar but landed outside the cycle's daily_cap —
        # kept as a hidden waitlist rather than discarded (see scheduler.py).
        "cap_missed": bool(job.get("cap_missed", False)),
        "tailoring_recommended": job.get("tailoring_recommended", True),
        "tailoring_reasoning": job.get("tailoring_reasoning", ""),
        "apply_status": "idle", "pipeline_status": None,
        "output_folder": None, "files": [], "apply_error": None,
        "apply_started_at": None, "apply_finished_at": None,
        "applied": False, "applied_at": None,
        "not_applying": False, "not_applying_at": None,
        "possible_duplicate": False, "duplicate_of": None, "duplicate_note": None,
    }


def _run_scrape_cycle(job_id: str, overrides: dict | None = None):
    from modes.scraper import run_once

    def on_progress(**fields):
        # SCRAPE_RUNS[job_id] may not exist yet if the run was cleared by a
        # server restart mid-cycle — guard rather than crash the thread.
        if job_id in SCRAPE_RUNS:
            SCRAPE_RUNS[job_id].update(fields)

    def on_new_entry(job: dict):
        # Same dedup check as create_entry()'s manual-add path, applied here
        # so a scrape cycle flags (rather than silently drops) a JD that
        # looks like it already exists — e.g. LinkedIn reposts the same
        # posting under a brand-new job ID, so apply_link alone won't catch
        # it, but the JD text hash still matches. We still CREATE the entry
        # (matching on hash/link is a strong signal, not a certainty) and
        # just flag it so the user can eyeball it in the Applications tab
        # instead of it vanishing without a trace.
        duplicate = _find_duplicate_entry(job.get("description", ""), job.get("apply_link", ""))
        entry = _new_entry_from_scraped_job(job)
        if duplicate:
            entry["possible_duplicate"] = True
            entry["duplicate_of"] = duplicate["id"]
            entry["duplicate_note"] = _duplicate_note(duplicate)
        ENTRIES[entry["id"]] = entry
        entries_store.save(ENTRIES)

    try:
        run_once(on_progress=on_progress, on_new_entry=on_new_entry, overrides=overrides)
        SCRAPE_RUNS[job_id].update({"status": "completed", "finished_at": _now()})
    except Exception as e:
        logger.exception(f"[scrape] job {job_id} failed")
        SCRAPE_RUNS[job_id].update({"status": "error", "error": str(e), "finished_at": _now()})


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class EntryCreateRequest(BaseModel):
    company: str
    title: str = "Software Engineer"
    jd: str
    apply_link: str = ""


class GenerateRequest(BaseModel):
    generate_cover_letter: bool = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/entries", dependencies=[Depends(require_auth)])
def create_entry(req: EntryCreateRequest):
    """
    Add a JD to the list immediately — returns in milliseconds, before
    scoring even starts. Scoring runs in the background; poll GET /api/entries
    (or the single entry) to watch score_status go scoring -> scored.

    Duplicate-safe: if this JD (by content or apply_link) already exists as
    an entry — scored, scoring, applied, whatever its state — that existing
    entry is returned instead of creating a second row for the same
    posting. The response carries "duplicate": true so the frontend can
    tell the two cases apart without guessing from field values.
    """
    duplicate = _find_duplicate_entry(req.jd, req.apply_link)
    if duplicate:
        return {**duplicate, "duplicate": True}

    entry_id = uuid.uuid4().hex[:12]
    ENTRIES[entry_id] = {
        "id": entry_id, "company": req.company, "title": req.title, "jd": req.jd,
        "jd_hash": jd_cache.jd_hash(req.jd),
        "apply_link": req.apply_link.strip(), "source": "manual",
        "created_at": _now(),
        "platform": None, "location": "", "is_remote": False, "date_posted": "", "salary": "",
        "score_status": "scoring", "score": None, "proceed": None,
        "stage": None, "reasoning": "", "red_flags": [], "score_error": None,
        "rubric_version": None,
        "tailoring_recommended": None, "tailoring_reasoning": "",
        "apply_status": "idle", "pipeline_status": None,
        "output_folder": None, "files": [], "apply_error": None,
        "apply_started_at": None, "apply_finished_at": None,
        "applied": False, "applied_at": None,
        "not_applying": False, "not_applying_at": None,
        "possible_duplicate": False, "duplicate_of": None, "duplicate_note": None,
    }
    entries_store.save(ENTRIES)
    threading.Thread(target=_score_entry, args=(entry_id,), daemon=True).start()
    return ENTRIES[entry_id]


@app.post("/api/entries/{entry_id}/toggle-applied", dependencies=[Depends(require_auth)])
def toggle_applied(entry_id: str):
    """Flip an entry's applied flag — one click to mark, one more to undo."""
    entry = ENTRIES.get(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found.")
    entry["applied"] = not entry.get("applied", False)
    entry["applied_at"] = _now() if entry["applied"] else None
    # Mutually exclusive with not_applying — you can't be both "I applied"
    # and "I've decided not to" for the same posting.
    if entry["applied"]:
        entry["not_applying"] = False
        entry["not_applying_at"] = None
    entries_store.save(ENTRIES)
    return entry


@app.post("/api/entries/{entry_id}/toggle-not-applying", dependencies=[Depends(require_auth)])
def toggle_not_applying(entry_id: str):
    """
    Flip an entry's not_applying flag — for postings you've decided to pass
    on (bad fit despite the score, stale listing, changed your mind) but
    want to keep around rather than delete, so they don't get silently
    reconsidered later. One click to mark, one more to undo.
    """
    entry = ENTRIES.get(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found.")
    entry["not_applying"] = not entry.get("not_applying", False)
    entry["not_applying_at"] = _now() if entry["not_applying"] else None
    if entry["not_applying"]:
        entry["applied"] = False
        entry["applied_at"] = None
    entries_store.save(ENTRIES)
    return entry


@app.delete("/api/entries/{entry_id}", dependencies=[Depends(require_auth)])
def delete_entry(entry_id: str):
    """
    Remove a JD from the tracking list. Only deletes the entries.json record —
    any already-generated resume/cover-letter files stay on disk untouched
    (deleting the tracking row shouldn't silently destroy generated work).
    """
    if entry_id not in ENTRIES:
        raise HTTPException(404, "Entry not found.")
    del ENTRIES[entry_id]
    entries_store.save(ENTRIES)
    return {"deleted": entry_id}


class BulkDeleteRequest(BaseModel):
    min_score: int = 75
    skip_applied: bool = True
    also_delete_from_sheet: bool = False


@app.post("/api/entries/delete-below-threshold", dependencies=[Depends(require_auth)])
def delete_below_threshold(req: BulkDeleteRequest):
    """
    Declutter: remove every SCORED entry below min_score. Only touches
    score_status == "scored" entries — anything still scoring, errored, or
    missing a score is left alone rather than guessed at. skip_applied=True
    (default) protects anything you've marked applied even if it scored low,
    since that's a real record of action taken, not clutter. Same
    non-destructive rule as single delete: entries.json only, generated
    files and all caches (jd_cache, job_cache) are untouched.

    also_delete_from_sheet additionally rewrites the Google Sheet itself
    (same min_score/skip_applied rule) — the two are separate systems since
    the scraper stopped writing new rows there, so a threshold cleanup here
    doesn't touch the sheet unless explicitly asked.
    """
    to_delete = [
        eid for eid, e in ENTRIES.items()
        if e.get("score_status") == "scored"
        and (e.get("score") or 0) < req.min_score
        and not (req.skip_applied and e.get("applied"))
    ]
    for eid in to_delete:
        del ENTRIES[eid]
    entries_store.save(ENTRIES)

    result = {"deleted_count": len(to_delete), "deleted_ids": to_delete}

    if req.also_delete_from_sheet:
        from scraper.sheets import delete_rows_below_score
        try:
            sheet_result = delete_rows_below_score(req.min_score, skip_applied=req.skip_applied)
            result["sheet_deleted_count"] = sheet_result["deleted_count"]
            result["sheet_kept_count"] = sheet_result["kept_count"]
        except Exception as e:
            logger.exception("[delete-below-threshold] sheet cleanup failed")
            result["sheet_error"] = f"{type(e).__name__}: {e}"

    return result


class ImportFromSheetRequest(BaseModel):
    min_score: int = 0     # 0 = no floor, import every score
    date_from: str = ""    # "YYYY-MM-DD", "" = no lower bound
    date_to: str = ""      # "YYYY-MM-DD", "" = no upper bound


@app.post("/api/entries/import-from-sheet", dependencies=[Depends(require_auth)])
def import_from_sheet(req: ImportFromSheetRequest):
    """
    Backfill jobs scored BEFORE the scraper switched from writing to Google
    Sheets to writing directly into entries.json. Reads every row in the
    sheet and, for each one not already tracked (matched by apply_link) AND
    within the requested min_score/date_from/date_to window, recovers the
    job. Prefers scraper/seen_jobs.py's job_cache.json (30-day TTL) since it
    has the FULL job description — the sheet itself only ever stored a
    truncated summary/reasoning, never the full JD text needed to actually
    tailor a resume. Falls back to the sheet's own truncated fields only if
    the cache entry has expired.

    Date filtering uses the sheet's "Date Added" column (when a row predates
    that column, it has no date to filter on — those rows are INCLUDED
    rather than silently dropped, since "unknown" isn't "out of range").
    """
    from scraper.sheets import read_all_rows
    from scraper.seen_jobs import load_job_from_cache
    from core.should_apply import min_score as _min_score

    rows = read_all_rows()
    existing_links = {
        (e.get("apply_link") or "").strip() for e in ENTRIES.values() if e.get("apply_link")
    }

    imported_full, imported_summary_only = 0, 0
    skipped_existing, skipped_no_link, skipped_below_threshold, skipped_out_of_range = 0, 0, 0, 0

    for row in rows:
        link = str(row.get("Apply Link") or "").strip()
        if not link:
            skipped_no_link += 1
            continue
        if link in existing_links:
            skipped_existing += 1
            continue

        score = int(row.get("Score") or 0)
        if score < req.min_score:
            skipped_below_threshold += 1
            continue

        # "Date Added" is a newer column — older rows written before it
        # existed have it blank; treated as unknown (included), not excluded.
        row_created_at = str(row.get("Date Added") or "").strip() or None
        if req.date_from or req.date_to:
            day = (row_created_at or "")[:10]
            if day:
                if req.date_from and day < req.date_from:
                    skipped_out_of_range += 1
                    continue
                if req.date_to and day > req.date_to:
                    skipped_out_of_range += 1
                    continue

        cached_job = load_job_from_cache(link)
        if cached_job:
            entry = _new_entry_from_scraped_job(cached_job, created_at=row_created_at)
            imported_full += 1
        else:
            entry = {
                "id": uuid.uuid4().hex[:12],
                "company": str(row.get("Company") or "?"), "title": str(row.get("Job Title") or "?"),
                "jd": str(row.get("JD Summary") or "") or "(full JD unavailable — recovered from sheet only, job_cache.json had expired)",
                "apply_link": link, "source": "scraper",
                "created_at": row_created_at or _now(),
                "score_status": "scored", "score": score, "proceed": score >= _min_score(),
                "stage": "llm", "reasoning": str(row.get("Fit Reasoning") or ""), "red_flags": [], "score_error": None,
                "apply_status": "idle", "pipeline_status": None,
                "output_folder": str(row.get("Output Folder") or "") or None, "files": [], "apply_error": None,
                "apply_started_at": None, "apply_finished_at": None,
                "applied": str(row.get("Application Status") or "").strip() not in ("", "—"),
                "applied_at": None,
            }
            imported_summary_only += 1

        ENTRIES[entry["id"]] = entry
        existing_links.add(link)

    entries_store.save(ENTRIES)
    return {
        "imported_full": imported_full,
        "imported_summary_only": imported_summary_only,
        "skipped_existing": skipped_existing,
        "skipped_no_link": skipped_no_link,
        "skipped_below_threshold": skipped_below_threshold,
        "skipped_out_of_range": skipped_out_of_range,
    }


@app.get("/api/entries", dependencies=[Depends(require_auth)])
def list_entries():
    # jd text omitted from the list view (can be long); full text still
    # available via the single-entry GET below if ever needed.
    return sorted(
        ({k: v for k, v in e.items() if k != "jd"} for e in ENTRIES.values()),
        key=lambda e: e["created_at"], reverse=True,
    )


@app.get("/api/entries/{entry_id}", dependencies=[Depends(require_auth)])
def get_entry(entry_id: str):
    entry = ENTRIES.get(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found.")
    return entry


@app.post("/api/entries/{entry_id}/rescore", dependencies=[Depends(require_auth)])
def rescore_entry(entry_id: str):
    """
    Re-run should-apply scoring for one entry — for entries whose
    score_status is "error" (e.g. Groq's daily quota was exhausted when they
    were first added). Refuses to interrupt a scoring run already in flight.
    """
    entry = ENTRIES.get(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found.")
    if entry["score_status"] == "scoring":
        raise HTTPException(409, "Already scoring this entry.")
    entry.update({"score_status": "scoring", "score": None, "proceed": None,
                  "stage": None, "reasoning": "", "red_flags": [], "score_error": None})
    entries_store.save(ENTRIES)
    threading.Thread(target=_score_entry, args=(entry_id,), daemon=True).start()
    return entry


@app.post("/api/entries/rescore-failed", dependencies=[Depends(require_auth)])
def rescore_failed_entries():
    """Re-run should-apply scoring for every entry currently in score_status
    "error" — the bulk version of rescore_entry, for clearing out a batch of
    failures caused by one shared outage (e.g. a Groq quota window)."""
    failed_ids = [eid for eid, e in ENTRIES.items() if e["score_status"] == "error"]
    for entry_id in failed_ids:
        entry = ENTRIES[entry_id]
        entry.update({"score_status": "scoring", "score": None, "proceed": None,
                      "stage": None, "reasoning": "", "red_flags": [], "score_error": None})
        threading.Thread(target=_score_entry, args=(entry_id,), daemon=True).start()
    entries_store.save(ENTRIES)
    return {"retried": len(failed_ids), "entry_ids": failed_ids}


@app.post("/api/entries/{entry_id}/generate", dependencies=[Depends(require_auth)])
def generate_entry(entry_id: str, req: GenerateRequest):
    entry = ENTRIES.get(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found.")
    if entry["apply_status"] == "running":
        raise HTTPException(409, "Already generating for this entry.")
    entry.update({"apply_status": "running", "apply_started_at": _now(),
                  "apply_error": None, "files": []})
    entries_store.save(ENTRIES)
    threading.Thread(
        target=_generate_entry, args=(entry_id, req.generate_cover_letter), daemon=True,
    ).start()
    return entry


@app.post("/api/entries/{entry_id}/cancel-generate", dependencies=[Depends(require_auth)])
def cancel_generate(entry_id: str):
    """
    'Stop' for a running generation. Honest about what this actually does:
    the pipeline is one opaque synchronous call (LangGraph nodes making
    real LLM requests) with no cancellation hook threaded through it, so
    the background thread is NOT killed — it keeps running to completion.
    What this does is immediately free up the UI (apply_status flips away
    from "running" right now, so you can retry or move on) and tell
    _generate_entry to discard whatever result eventually comes back
    instead of overwriting this with "completed"/"error" after the fact.
    """
    entry = ENTRIES.get(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found.")
    if entry["apply_status"] != "running":
        raise HTTPException(409, "Nothing running for this entry.")
    entry.update({
        "apply_status": "cancelled",
        "apply_error": "Cancelled by user. Note: the pipeline call already in flight keeps "
                        "running in the background until it finishes — this just stops the UI "
                        "from waiting on it and discards whatever result it eventually returns.",
        "apply_finished_at": _now(),
    })
    entries_store.save(ENTRIES)
    return entry


@app.get("/api/entries/{entry_id}/download/{filename}", dependencies=[Depends(require_auth)])
def download(entry_id: str, filename: str):
    entry = ENTRIES.get(entry_id)
    if not entry or not entry.get("output_folder"):
        raise HTTPException(404, "Entry not found or nothing generated yet.")
    if not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename.")
    output_folder = entry["output_folder"]
    # Whitelist against files actually present in this entry's own folder —
    # never trust the filename alone, even after the character-class check above.
    available = set(os.listdir(output_folder))
    if filename not in available:
        raise HTTPException(404, "File not found for this entry.")
    path = os.path.join(output_folder, filename)
    return FileResponse(path, filename=filename, media_type="application/pdf")


@app.post("/api/scrape", dependencies=[Depends(require_auth)])
def trigger_scrape():
    job_id = uuid.uuid4().hex[:12]
    SCRAPE_RUNS[job_id] = {
        "job_id": job_id, "status": "running", "started_at": _now(),
        "phase": "starting", "phase_detail": "Starting scrape cycle...",
        "jobs_scraped": None, "jobs_total": None, "jobs_scored": 0,
        "high_match_count": 0, "sheet_written": 0,
    }
    thread = threading.Thread(target=_run_scrape_cycle, args=(job_id, {"run_kind": "manual"}), daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "running"}


@app.get("/api/scrape/{job_id}", dependencies=[Depends(require_auth)])
def get_scrape_status(job_id: str):
    job = SCRAPE_RUNS.get(job_id)
    if not job:
        raise HTTPException(404, "Scrape job not found.")
    return job


@app.get("/api/scrape", dependencies=[Depends(require_auth)])
def list_scrape_runs():
    return sorted(SCRAPE_RUNS.values(), key=lambda r: r["started_at"], reverse=True)


class QuickSearchRequest(BaseModel):
    """
    A one-off, location/recency/title-scoped search — e.g. "Chicago jobs
    posted in the last 24 hours" — run on demand without touching the user's
    saved Scraper-tab settings at all (unlike the regular "Run" button, which
    persists whatever's on screen before running). Every field is optional;
    an omitted one falls back to the user's saved standing setting for that
    field only — location/hours_old/daily_cap fall back individually, but an
    omitted `keywords` means "use my saved per-platform keyword lists" rather
    than partially overriding them, since there's no single sensible string
    to fall back to per platform.
    """
    location: str | None = None
    hours_old: int | None = None
    keywords: str | None = None
    # "Top N aligned to your profile" — reuses run_scrape_cycle's existing
    # ranking (same rubric, same ATS/sponsorship tie-breaks) with a small cap
    # instead of the user's full daily_cap, since this is meant to be a quick
    # look, not a full harvesting run.
    top_n: int = 10


@app.post("/api/quick-search", dependencies=[Depends(require_auth)])
def trigger_quick_search(req: QuickSearchRequest):
    if req.top_n < 1 or req.top_n > 50:
        raise HTTPException(422, "top_n must be between 1 and 50.")
    overrides = {
        "location": req.location, "hours_old": req.hours_old,
        "keywords": req.keywords, "daily_cap": req.top_n, "run_kind": "one-off",
    }
    job_id = uuid.uuid4().hex[:12]
    SCRAPE_RUNS[job_id] = {
        "job_id": job_id, "status": "running", "started_at": _now(),
        "phase": "starting", "phase_detail": "Starting quick search...",
        "jobs_scraped": None, "jobs_total": None, "jobs_scored": 0,
        "high_match_count": 0, "sheet_written": 0, "quick_search": True,
    }
    thread = threading.Thread(target=_run_scrape_cycle, args=(job_id, overrides), daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "running"}


# ---------------------------------------------------------------------------
# Per-company email overrides — a company-name substring match (e.g.
# "microsoft") swaps in a dedicated email for every resume/CL generated for
# that employer, applied in engine/graph.py's finalizer_node. Small enough
# to not need its own store module; reuses core/email_overrides.py directly.
# ---------------------------------------------------------------------------
class EmailOverrideRequest(BaseModel):
    pattern: str
    email: str


@app.get("/api/email-overrides", dependencies=[Depends(require_auth)])
def list_email_overrides():
    from core import email_overrides
    return email_overrides.load()


@app.post("/api/email-overrides", dependencies=[Depends(require_auth)])
def add_email_override(req: EmailOverrideRequest):
    from core import email_overrides
    pattern = req.pattern.strip().lower()
    email = req.email.strip()
    if not pattern or not email:
        raise HTTPException(400, "Both pattern and email are required.")
    rules = [r for r in email_overrides.load() if r.get("pattern") != pattern]
    rules.insert(0, {"pattern": pattern, "email": email})  # newest/most-specific wins on match
    email_overrides.save(rules)
    return rules


@app.delete("/api/email-overrides/{pattern}", dependencies=[Depends(require_auth)])
def delete_email_override(pattern: str):
    from core import email_overrides
    rules = email_overrides.load()
    remaining = [r for r in rules if r.get("pattern") != pattern.strip().lower()]
    if len(remaining) == len(rules):
        raise HTTPException(404, "No override with that pattern.")
    email_overrides.save(remaining)
    return remaining


# ---------------------------------------------------------------------------
# ATS target companies — the direct-company-board scrape source (Greenhouse/
# Lever/Ashby/Workday). Curated list since none of these platforms expose a
# global search; add a company here to have its board checked every cycle.
# ---------------------------------------------------------------------------
class AtsCompanyRequest(BaseModel):
    company: str
    ats: str
    identifier: str


@app.get("/api/ats-companies", dependencies=[Depends(require_auth)])
def list_ats_companies():
    from scraper import ats_companies
    return ats_companies.load()


@app.post("/api/ats-companies", dependencies=[Depends(require_auth)])
def add_ats_company(req: AtsCompanyRequest):
    from scraper import ats_companies
    error = ats_companies.validate(req.company, req.ats, req.identifier)
    if error:
        raise HTTPException(400, error)
    companies = [c for c in ats_companies.load() if c.get("company", "").lower() != req.company.strip().lower()]
    companies.append({"company": req.company.strip(), "ats": req.ats, "identifier": req.identifier.strip()})
    ats_companies.save(companies)
    return companies


@app.delete("/api/ats-companies/{company}", dependencies=[Depends(require_auth)])
def delete_ats_company(company: str):
    from scraper import ats_companies
    companies = ats_companies.load()
    remaining = [c for c in companies if c.get("company", "").lower() != company.strip().lower()]
    if len(remaining) == len(companies):
        raise HTTPException(404, "No configured company with that name.")
    ats_companies.save(remaining)
    return remaining


# ---------------------------------------------------------------------------
# LinkedIn company targets — scraper/linkedin_companies.py. A DIFFERENT list
# than ATS companies above: this is for employers that DON'T run a
# Greenhouse/Lever/Ashby/Workday board at all (Amazon, Microsoft, Google,
# Meta, etc. — no ATS entry is possible for them), but do post to LinkedIn.
# Each entry scopes one extra LinkedIn search per cycle, via jobspy's native
# company filter, to just that employer's postings.
# ---------------------------------------------------------------------------
class LinkedInCompanyRequest(BaseModel):
    company: str
    linkedin_id: int


@app.get("/api/linkedin-companies", dependencies=[Depends(require_auth)])
def list_linkedin_companies():
    from scraper import linkedin_companies
    return linkedin_companies.load()


@app.post("/api/linkedin-companies", dependencies=[Depends(require_auth)])
def add_linkedin_company(req: LinkedInCompanyRequest):
    from scraper import linkedin_companies
    error = linkedin_companies.validate(req.company, req.linkedin_id)
    if error:
        raise HTTPException(400, error)
    companies = [c for c in linkedin_companies.load() if c.get("company", "").lower() != req.company.strip().lower()]
    companies.append({"company": req.company.strip(), "linkedin_id": int(req.linkedin_id)})
    linkedin_companies.save(companies)
    return companies


@app.delete("/api/linkedin-companies/{company}", dependencies=[Depends(require_auth)])
def delete_linkedin_company(company: str):
    from scraper import linkedin_companies
    companies = linkedin_companies.load()
    remaining = [c for c in companies if c.get("company", "").lower() != company.strip().lower()]
    if len(remaining) == len(companies):
        raise HTTPException(404, "No configured company with that name.")
    linkedin_companies.save(remaining)
    return remaining


# ---------------------------------------------------------------------------
# Scraper search settings — keywords (select/deselect), location, remote,
# results-per-platform, hours-old, entry-level-only, exclude-agencies. Live-
# editable: scraper/scheduler.py and scraper/filter.py both read this fresh
# on every cycle, so a change here applies next run, no restart needed.
# ---------------------------------------------------------------------------
class ScraperSettingsRequest(BaseModel):
    keywords: list[dict]          # [{"value", "enabled", "focus"?}, ...]
    # Custom-mode per-platform table. Optional: the simple-mode UI may not send
    # it, and an omitted table must keep the saved one (not reset to defaults).
    platforms: dict | None = None
    mode: str | None = None       # "simple" | "custom"
    depth: str | None = None      # "light" | "normal" | "deep"
    sources: dict | None = None   # {"linkedin": bool, ..., "company_boards": bool, "linkedin_company_pages": bool}
    location: str
    remote_only: bool
    hours_old: int
    entry_level_only: bool
    exclude_recruiting_agencies: bool
    daily_cap: int = 25
    # The one apply threshold (see core.should_apply.min_score()). Optional so
    # an older client that doesn't send it doesn't overwrite the env default
    # with a hardcoded number — see update_scraper_settings().
    min_score: int | None = None


@app.get("/api/meta", dependencies=[Depends(require_auth)])
def get_meta():
    """
    Server-side constants the frontend needs to interpret entries correctly.
    rubric_version lets the UI flag entries scored under an older rubric
    (whose numbers aren't comparable to current ones) instead of the
    frontend hardcoding a version it would forget to bump.
    """
    from core.should_apply import RUBRIC_VERSION, min_score
    return {"rubric_version": RUBRIC_VERSION, "min_score": min_score()}


@app.get("/api/scraper-settings", dependencies=[Depends(require_auth)])
def get_scraper_settings():
    from scraper import scraper_settings
    return scraper_settings.load()


@app.post("/api/scraper-settings", dependencies=[Depends(require_auth)])
def update_scraper_settings(req: ScraperSettingsRequest):
    from scraper import scraper_settings
    settings = req.model_dump(exclude_none=True)  # an omitted min_score keeps the env default
    error = scraper_settings.validate(settings)
    if error:
        raise HTTPException(status_code=422, detail=error)
    current = scraper_settings.load()
    for key in ("platforms", "mode", "depth", "sources"):
        settings.setdefault(key, current.get(key))
    scraper_settings.save(settings)
    return scraper_settings.load()  # merged over defaults, so the client sees every key


# ---------------------------------------------------------------------------
# Scraper tab redesign (Oct 2026): plain-English plan, run funnel history, and
# ONE watched-companies list (company boards + LinkedIn company pages) added by
# name. The old /api/ats-companies and /api/linkedin-companies endpoints stay
# for compatibility; these read and write the same two stores.
# ---------------------------------------------------------------------------
@app.get("/api/scraper/plan", dependencies=[Depends(require_auth)])
def get_scraper_plan():
    from scraper import scraper_settings, ats_companies, linkedin_companies, run_history
    plan = scraper_settings.describe_plan(
        scraper_settings.load(),
        n_company_boards=len(ats_companies.load()),
        n_linkedin_pages=len(linkedin_companies.load()),
    )
    plan["last_run"] = run_history.last()
    return plan


@app.get("/api/scrape-history", dependencies=[Depends(require_auth)])
def get_scrape_history(limit: int = 5):
    from scraper import run_history
    return list(reversed(run_history.load()))[: max(1, min(limit, 30))]


_ATS_SOURCES = {"greenhouse", "lever", "ashby", "workday"}


def _watched_companies() -> list[dict]:
    from scraper import ats_companies, linkedin_companies, run_history
    last = run_history.last() or {}
    found = last.get("company_found") or {}
    rows = [{"company": c["company"], "source": c["ats"], "identifier": c["identifier"],
             "found_last_run": found.get(c["company"])} for c in ats_companies.load()]
    rows += [{"company": c["company"], "source": "linkedin", "identifier": c["linkedin_id"],
              "found_last_run": found.get(c["company"])} for c in linkedin_companies.load()]
    return sorted(rows, key=lambda r: r["company"].lower())


class DetectCompanyRequest(BaseModel):
    name: str
    url: str | None = None


class WatchCompanyRequest(BaseModel):
    company: str
    source: str          # greenhouse | lever | ashby | workday | linkedin
    identifier: str | int


@app.get("/api/target-companies", dependencies=[Depends(require_auth)])
def list_target_companies():
    return _watched_companies()


@app.post("/api/target-companies/detect", dependencies=[Depends(require_auth)])
def detect_target_company(req: DetectCompanyRequest):
    from scraper import company_resolver
    if not req.name.strip() and not (req.url or "").strip():
        raise HTTPException(400, "Type a company name (or paste its careers / LinkedIn page URL).")
    return {"candidates": company_resolver.detect(req.name.strip(), (req.url or "").strip() or None)}


@app.post("/api/target-companies", dependencies=[Depends(require_auth)])
def add_target_company(req: WatchCompanyRequest):
    from scraper import ats_companies, linkedin_companies
    name = req.company.strip()
    if req.source in _ATS_SOURCES:
        error = ats_companies.validate(name, req.source, str(req.identifier))
        if error:
            raise HTTPException(400, error)
        rows = [c for c in ats_companies.load() if c.get("company", "").lower() != name.lower()]
        rows.append({"company": name, "ats": req.source, "identifier": str(req.identifier).strip()})
        ats_companies.save(rows)
    elif req.source == "linkedin":
        error = linkedin_companies.validate(name, req.identifier)
        if error:
            raise HTTPException(400, error)
        rows = [c for c in linkedin_companies.load() if c.get("company", "").lower() != name.lower()]
        rows.append({"company": name, "linkedin_id": int(req.identifier)})
        linkedin_companies.save(rows)
    else:
        raise HTTPException(400, "source must be one of greenhouse, lever, ashby, workday, linkedin")
    return _watched_companies()


@app.delete("/api/target-companies/{source}/{company}", dependencies=[Depends(require_auth)])
def remove_target_company(source: str, company: str):
    from scraper import ats_companies, linkedin_companies
    store = linkedin_companies if source == "linkedin" else ats_companies
    rows = store.load()
    remaining = [c for c in rows if c.get("company", "").lower() != company.strip().lower()
                 or (store is ats_companies and c.get("ats") != source)]
    if len(remaining) == len(rows):
        raise HTTPException(404, "Not in your watched companies.")
    store.save(remaining)
    return _watched_companies()


# ---------------------------------------------------------------------------
# Static frontend — mounted last so it never shadows the /api/* routes above.
# Lets the whole app run as a single process locally or on a single Fly.io
# service; in production you can still serve frontend/ from Cloudflare Pages
# instead and point it at this API via window.API_BASE.
# ---------------------------------------------------------------------------
from fastapi.staticfiles import StaticFiles  # noqa: E402


class _NoCacheStaticFiles(StaticFiles):
    """
    Plain StaticFiles has no explicit Cache-Control header, which lets
    browsers apply their own heuristic freshness and serve a stale index.html/
    app.js/style.css for a while even after the file on disk changed —
    exactly the bug that caused "every edit needs a real hard-refresh, and
    sometimes not even that" during development. no-cache forces the browser
    to revalidate against the server's ETag on every load (a cheap 304 when
    unchanged) instead of trusting a local copy blindly.
    """
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


_FRONTEND_DIR = os.path.join(ROOT, "frontend")
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", _NoCacheStaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
