"""
scraper/scheduler.py

APScheduler-based runner:
  - Every SCRAPER_INTERVAL_HOURS (default 2): scrape → filter → process job by job:
        score → if high match: write sheet + generate resume + update sheet + notify
               if below threshold: mark seen and skip (never added to sheet)
  - Every 60 seconds: poll sheet for "triggered" rows and generate cover letters

Flow per job:
    1. Score with Haiku
    2. Below threshold → mark_seen → continue (not added to sheet, won't re-appear)
    3. High match → write to sheet immediately → generate resume →
       update sheet folder → send Telegram (includes resume status)

Crash rules:
  - Never crash the scheduler on a single job failure.
  - All exceptions per job/cycle are caught and logged.
"""

import os
import sys
import json
import time
import traceback

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scraper import scraper_settings
from scraper.scrape import fetch_jobs
from scraper.filter import filter_jobs
from scraper.score import score_single_job
from scraper.sheets import open_sheet_session
from scraper.notify import send_notification
from scraper.seen_jobs import mark_seen, save_job_to_cache
from scraper.sponsorship import lookup_sponsorship
from scraper.preflight import run_preflight
from core.profile_cache import load_profile_cache
from core.should_apply import min_score as core_min_score
from cover_letter.trigger_from_sheet import poll_and_generate

_MIN_JD_LENGTH = int(os.getenv("MIN_JD_LENGTH", "100"))  # chars

_SCRAPER_INTERVAL_HOURS = float(os.getenv("SCRAPER_INTERVAL_HOURS", "2"))
# The apply threshold is read per cycle from scraper settings via
# core.should_apply.min_score() — single source, UI-editable, no restart.

# Search config (keywords/location/remote/results-per-platform) is NOT read
# here as frozen constants — it's read fresh at the start of every
# run_scrape_cycle() call via scraper_settings.load(), so changes made in
# the web UI's Scraper tab apply on the very next cycle without a restart.

# Delay between scorer calls for consecutive below-threshold jobs.
# High-match jobs get natural spacing from the pipeline (~60-90s).
_SCORE_SKIP_DELAY = 3  # seconds


_RESUMES_DIR = os.path.expanduser(os.getenv("OUTPUT_BASE_PATH", "~/Documents/Resumes"))


def _noop_progress(**_fields):
    pass


_ATS_PLATFORMS = {"greenhouse", "lever", "ashby", "workday"}


def _rank_key(job: dict) -> tuple:
    """
    Composite sort key for picking the daily top-N: primarily the should_apply
    score, with a tie-break toward jobs more likely to actually get a
    response — direct-to-company ATS applications (no agency middleman) and
    confirmed H-1B sponsorship history both correlate with hearing back, so
    they win ties against an equally-scored job without those signals.
    """
    ats_bonus = 1 if job.get("platform") in _ATS_PLATFORMS else 0
    sponsor_signal = job.get("sponsorship_signal")
    sponsor_bonus = 2 if sponsor_signal == "confirmed_h1b" else (1 if sponsor_signal == "likely" else 0)
    return (job.get("score", 0), ats_bonus, sponsor_bonus)


_QUICK_SEARCH_RESULTS_PER_PLATFORM = 15  # lightweight, fast — not the user's configured daily-cycle counts


def run_scrape_cycle(on_progress=None, on_new_entry=None, overrides=None):
    """
    Full cycle: scrape → filter → score everything → rank → keep only the
    daily top-N ("daily_cap" in scraper settings) → write/notify.

    Every job that passes the earlier filters gets SCORED (that cost is
    already spent), then ranked by _rank_key() and only the top daily_cap
    survive to be written into entries.json/sheet — this is what makes the
    Applications tab "today's N best" instead of "everything that happened
    to clear a fixed threshold," which could be 3 jobs or 60 depending on
    the day. Every scored job (kept or cut) is still marked seen and cached
    so nothing gets re-scraped (and re-billed) on a future cycle for
    landing outside the cap.

    on_progress: optional callable(**fields) called at each phase boundary
    and after every job is scored, so a caller can surface real progress
    (e.g. "scoring 12/47") instead of a single opaque "running" state for
    the whole cycle, which can otherwise run 20-30+ minutes with zero
    visibility. Defaults to a no-op so this function's standalone/CLI/daemon
    use (which just reads the print() output) is unaffected.

    on_new_entry: optional callable(job: dict) called once per KEPT job,
    already carrying score/reasoning/sub_scores. When provided, this REPLACES
    the Google Sheets write entirely — the web UI's Applications tab is now
    the single place scraped jobs show up, same as manually-added JDs,
    instead of a second parallel system in a spreadsheet. The Sheets session
    (and its network/auth round-trip) isn't even opened in this case. Standalone/
    daemon use that doesn't pass this keeps writing to the sheet as before.

    overrides: optional dict for a ONE-OFF run that must NOT touch the user's
    saved scraper_settings.json — e.g. the web UI's "Quick Search" (a location/
    recency/title-scoped lookup run on demand, separate from the standing
    daily-cycle config). Recognized keys, all optional: "location", "hours_old",
    "keywords" (a single search string applied to every platform + ATS, bypassing
    each platform's own configured keyword list/counts for this run only, at
    _QUICK_SEARCH_RESULTS_PER_PLATFORM depth), "daily_cap" (quick search passes
    a small number, e.g. 10, to get "top N aligned to your profile" using the
    exact same ranking as a normal cycle — same rubric, same tie-breaks, no new
    scoring logic). Every key is applied in-memory only; scraper_settings.load()/
    save() are never touched by this parameter.
    """
    if on_progress is None:
        on_progress = _noop_progress
    overrides = overrides or {}

    settings = scraper_settings.load()
    if overrides.get("keywords"):
        # Quick search: one title string, same depth on every platform,
        # bypassing each platform's own saved keyword list/counts for this
        # run only — nothing here is written back to scraper_settings.json.
        kw = overrides["keywords"]
        platform_keywords = {p: kw for p in ("linkedin", "indeed", "dice", "google_jobs")}
        platform_counts = {p: _QUICK_SEARCH_RESULTS_PER_PLATFORM for p in ("linkedin", "indeed", "dice", "google_jobs")}
        ats_keywords = kw
    else:
        platform_keywords = {
            p: scraper_settings.platform_keywords_string(p, settings)
            for p in ("linkedin", "indeed", "dice", "google_jobs")
        }
        platform_counts = {
            p: (scraper_settings.platform_keyword_counts(p, settings) if p == "linkedin"
                else scraper_settings.platform_count(p, settings))
            for p in ("linkedin", "indeed", "dice", "google_jobs")
        }
        ats_keywords = scraper_settings.enabled_keywords_string(settings)
    location = overrides.get("location") or settings["location"]
    remote_only = settings["remote_only"]
    hours_old = overrides.get("hours_old") or settings["hours_old"]
    daily_cap = overrides.get("daily_cap") or settings.get("daily_cap", 25)
    min_score = int(settings.get("min_score") or core_min_score())

    print("\n" + "=" * 60)
    print("SCRAPE CYCLE STARTING")
    print("=" * 60)
    if not any(platform_keywords.values()) and not ats_keywords:
        print("[scheduler] No keywords enabled anywhere in scraper settings — nothing to search for.")
        on_progress(phase="error", phase_detail="No keywords enabled — enable at least one in the Scraper tab.")
        return
    on_progress(phase="scraping", phase_detail="Fetching from all configured platforms...")

    # 1. Scrape — each platform searches its OWN keyword list/count.
    from scraper import linkedin_companies
    try:
        raw_jobs = fetch_jobs(
            platform_keywords=platform_keywords,
            platform_counts=platform_counts,
            ats_keywords=ats_keywords,
            location=location,
            remote_only=remote_only,
            hours_old=hours_old,
            linkedin_company_ids=linkedin_companies.company_ids(),
        )
    except Exception as e:
        print(f"[scheduler] SCRAPE ERROR: {e}")
        traceback.print_exc()
        on_progress(phase="error", phase_detail=f"Scrape failed: {e}")
        return

    on_progress(phase="filtering", phase_detail=f"Filtering {len(raw_jobs)} scraped jobs...",
                jobs_scraped=len(raw_jobs))

    # 2. Filter
    try:
        accepted, rejected = filter_jobs(raw_jobs)
    except Exception as e:
        print(f"[scheduler] FILTER ERROR: {e}")
        traceback.print_exc()
        on_progress(phase="error", phase_detail=f"Filtering failed: {e}")
        return

    if not accepted:
        print("[scheduler] No jobs passed filters — cycle done.")
        on_progress(phase="done", phase_detail="No jobs passed filters.",
                    jobs_scraped=len(raw_jobs), jobs_total=0, jobs_scored=0)
        return

    print(f"[scheduler] {len(accepted)} jobs to score and rank.")
    on_progress(phase="scoring", phase_detail=f"Scoring {len(accepted)} jobs...",
                jobs_scraped=len(raw_jobs), jobs_total=len(accepted), jobs_scored=0,
                high_match_count=0)

    # 3. Load scoring profile once.
    try:
        profile = load_profile_cache()
    except Exception as e:
        print(f"[scheduler] Could not load profile cache: {e} — aborting cycle.")
        on_progress(phase="error", phase_detail=f"Could not load profile cache: {e}")
        return

    # 4. Score every accepted job. No writing yet — the daily cap needs the
    #    FULL scored batch ranked before deciding who makes the cut.
    scored_candidates = []  # jobs with score >= min_score
    for i, job in enumerate(accepted):
        company = job.get("company", "?")
        title   = job.get("title", "?")
        link    = (job.get("apply_link") or "").strip()

        print(f"\n[scheduler] Job {i+1}/{len(accepted)}: {company} — {title}")
        on_progress(phase="scoring", phase_detail=f"Scoring {i+1}/{len(accepted)}: {company} — {title}",
                    jobs_scraped=len(raw_jobs), jobs_total=len(accepted), jobs_scored=i,
                    high_match_count=len(scored_candidates))

        # --- Pre-score JD length filter ---
        if len(job.get("description", "")) < _MIN_JD_LENGTH:
            print(f"  [scheduler] JD too short (<{_MIN_JD_LENGTH} chars) — marking seen and skipping")
            if link:
                try:
                    mark_seen(link)
                except Exception as e:
                    print(f"  [scheduler] mark_seen failed: {e}")
            continue

        # --- Sponsorship history lookup (opt-in via SPONSORSHIP_LOOKUP_ENABLED=true) ---
        try:
            job["sponsorship_signal"] = lookup_sponsorship(company)
        except Exception as e:
            print(f"  [scheduler] Sponsorship lookup failed: {e}")
            job["sponsorship_signal"] = "neutral"

        # --- Score ---
        try:
            score_single_job(job, profile)
        except Exception as e:
            print(f"  [scheduler] Scoring failed: {e} — skipping job")
            continue

        # --- Cache + mark seen for EVERY scored job, pass or fail — the
        #     LLM cost is already spent, and a job that misses the cap
        #     today must not get re-scraped (and re-billed) tomorrow. ---
        if link:
            try:
                save_job_to_cache(job)
            except Exception as e:
                print(f"  [scheduler] Job cache save failed: {e}")
            try:
                mark_seen(link)
            except Exception as e:
                print(f"  [scheduler] mark_seen failed: {e}")

        score = job.get("score", 0)
        if score >= min_score:
            scored_candidates.append(job)
            print(f"  [scheduler] {score}/100 — candidate ({len(scored_candidates)} so far)")
        else:
            print(f"  [scheduler] {score}/100 — below threshold, discarded")

    # 5. Rank candidates and keep only the daily top-N.
    scored_candidates.sort(key=_rank_key, reverse=True)
    kept = scored_candidates[:daily_cap]
    cut = scored_candidates[daily_cap:]
    if cut:
        # Entries mode: cap-missed candidates are NOT thrown away — they're
        # written as a hidden waitlist (cap_missed=True) so nothing that
        # cleared the bar is ever lost, at zero extra LLM cost (the verdict
        # is already cached). Sheet mode keeps the old discard behavior.
        fate = "waitlisted (hidden in Applications)" if on_new_entry else "discarded"
        print(f"[scheduler] {len(cut)} candidate(s) passed the bar but missed the "
              f"top-{daily_cap} cut — {fate} (already marked seen, won't re-score).")

    on_progress(phase="writing", phase_detail=f"Writing top {len(kept)} of {len(scored_candidates)} candidates...",
                jobs_scraped=len(raw_jobs), jobs_total=len(accepted), jobs_scored=len(accepted),
                high_match_count=len(kept))

    # 6. Open sheet session — skipped entirely when on_new_entry is provided,
    #    since those jobs go to entries.json instead. Retry up to 3 times
    #    with a short delay — transient DNS failures at startup are the
    #    most common cause of session open failures.
    sheet_session = None
    if on_new_entry is None and kept:
        for _attempt in range(3):
            try:
                sheet_session = open_sheet_session()
                break
            except Exception as e:
                print(f"[scheduler] Sheet session attempt {_attempt + 1}/3 failed: {e}")
                if _attempt < 2:
                    time.sleep(5)
        if sheet_session is None:
            print("[scheduler] Sheet unavailable — kept jobs will still be cached; rows will be recovered on next cycle.")

    # 7. Write only the KEPT (top-daily_cap) jobs, and notify.
    written = 0
    for job in kept:
        score = job.get("score", 0)
        if on_new_entry:
            try:
                on_new_entry(job)
                written += 1
            except Exception as e:
                print(f"  [scheduler] on_new_entry failed: {e}")
        elif sheet_session:
            try:
                sheet_session.write_job(job)
                written += 1
            except Exception as e:
                print(f"  [scheduler] Sheet write failed: {e}")

        print(f"  [scheduler] KEPT {score}/100 — {job.get('company')} — {job.get('title')}")
        try:
            send_notification(job, output_folder="", pipeline_failed=False)
        except Exception as e:
            print(f"  [scheduler] Notify failed: {e}")
        time.sleep(_SCORE_SKIP_DELAY)

    # 7b. Waitlist the cap-missed candidates (entries mode only) — no
    #     notification, they're deliberately below the fold.
    waitlisted = 0
    if on_new_entry:
        for job in cut:
            job["cap_missed"] = True
            try:
                on_new_entry(job)
                waitlisted += 1
                print(f"  [scheduler] WAITLISTED {job.get('score', 0)}/100 — {job.get('company')} — {job.get('title')}")
            except Exception as e:
                print(f"  [scheduler] on_new_entry (waitlist) failed: {e}")

    # 8. Sort sheet once at end of cycle (sheet mode only).
    if sheet_session:
        try:
            sheet_session.sort()
        except Exception as e:
            print(f"[scheduler] Sheet sort failed: {e}")

    destination = "Applications tab" if on_new_entry else "sheet"
    print(
        f"\n[scheduler] Cycle complete. "
        f"{written} of {len(scored_candidates)} qualifying jobs kept and added to {destination} "
        f"(daily cap {daily_cap}), {waitlisted} waitlisted, {len(cut) - waitlisted} discarded past the cap."
    )
    waitlist_note = f", {waitlisted} waitlisted" if waitlisted else ""
    on_progress(phase="done",
                phase_detail=f"Done — {written} added to {destination} (top {daily_cap} of "
                             f"{len(scored_candidates)} that passed the bar{waitlist_note}).",
                jobs_scraped=len(raw_jobs), jobs_total=len(accepted), jobs_scored=len(accepted),
                high_match_count=written, sheet_written=written)


def run_cl_poll():
    """Poll sheet for triggered cover letter requests."""
    try:
        poll_and_generate()
    except Exception as e:
        print(f"[scheduler] CL POLL ERROR: {e}")


def main():
    _settings = scraper_settings.load()
    print("[scheduler] Starting AI Resume Agent Scheduler")
    print(f"  Scrape interval: {_SCRAPER_INTERVAL_HOURS}h")
    print(f"  Location:        {_settings['location']}")
    print(f"  Remote only:     {_settings['remote_only']}")
    for p in ("linkedin", "indeed", "dice", "google_jobs"):
        kw = scraper_settings.platform_keywords_string(p, _settings)
        count = scraper_settings.platform_count(p, _settings)
        print(f"  {p:12s}   keywords=[{kw}] count={count}")
    print(f"  ATS keywords:    {scraper_settings.enabled_keywords_string(_settings)}")
    print(f"  Min score:       {_settings.get('min_score') or core_min_score()}")
    print(f"  Daily cap:       {_settings.get('daily_cap', 25)}")
    print()

    if not run_preflight():
        sys.exit(1)

    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        run_scrape_cycle,
        trigger=IntervalTrigger(hours=_SCRAPER_INTERVAL_HOURS),
        id="scrape_cycle",
        name="Scrape + Score + Auto-Resume",
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        run_cl_poll,
        trigger=IntervalTrigger(seconds=60),
        id="cl_poll",
        name="Cover Letter Poll",
        max_instances=1,
        coalesce=True,
    )

    print("[scheduler] Running initial scrape cycle on startup...")
    run_scrape_cycle()

    print("\n[scheduler] Scheduler started. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n[scheduler] Stopped.")


if __name__ == "__main__":
    main()
