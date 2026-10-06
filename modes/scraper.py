"""
modes/scraper.py — automated entry mode.

Thin wrapper over the existing scraper subsystem so all three entry modes are
launched from one CLI (apply.py). The scrape cycle itself already routes every
job through the shared scorer (core.should_apply via scraper/score.py), so
scraped and pasted JDs get identical verdicts and share the same JD cache.
"""

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def run_once(on_progress=None, on_new_entry=None, overrides=None):
    """
    One scrape → filter → score → entries/sheet → notify cycle, then exit.

    on_progress: optional callable(**fields) invoked at each phase boundary —
    lets a caller (e.g. server.py, for the web UI's progress display) track
    status without this module knowing anything about HTTP or the frontend.

    on_new_entry: optional callable(job: dict) invoked once per scored job —
    when provided, scored jobs go here instead of the Google Sheet.

    overrides: optional dict passed straight through to run_scrape_cycle() for
    a one-off run (e.g. the web UI's Quick Search) that must not touch the
    user's saved scraper_settings.json. See run_scrape_cycle()'s docstring.
    """
    from scraper.scheduler import run_scrape_cycle
    run_scrape_cycle(on_progress=on_progress, on_new_entry=on_new_entry, overrides=overrides)


def run_daemon():
    """Start the APScheduler loop (scrape every SCRAPER_INTERVAL_HOURS + CL poll)."""
    from scraper.scheduler import main
    main()
