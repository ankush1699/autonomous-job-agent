"""
cover_letter/trigger_from_sheet.py

Polls the Google Sheet "Job Discovery" tab for rows where the user has set
Resume Status to "Resume Only" or "Resume + Cover Letter", then generates
the requested documents and updates the sheet.

Runs every 60 seconds (called by the APScheduler in scraper/scheduler.py).

Trigger values (user sets):
  "Resume Only"           — generate tailored resume PDF, no cover letter
  "Resume + Cover Letter" — generate tailored resume + cover letter PDF

Status values (system sets):
  "Generating..."  — pipeline is running
  "Done ✓"         — completed successfully
  "Failed ✗"       — pipeline error (see Notes column for detail)

Rules:
- Never overwrite columns M or N after the initial row is written.
  Exception: M is updated as the trigger/status column.
- Never crash the scheduler on a single row failure.
- Sequential only — no parallel generation.
"""

import os
import sys
import json

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

from google.oauth2.service_account import Credentials
import gspread
from gspread.exceptions import WorksheetNotFound

import requests as _requests

from cover_letter.cl_writer import generate_and_save
from scraper.pipeline_trigger import trigger_for_jobs
from scraper.seen_jobs import load_job_from_cache

_LINK_CHECK_TIMEOUT = 8
_LINK_CHECK_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _is_link_alive(url: str) -> bool:
    """HEAD request to check the job link is still active. Returns True if reachable."""
    if not url:
        return False
    try:
        resp = _requests.head(
            url,
            headers={"User-Agent": _LINK_CHECK_UA},
            timeout=_LINK_CHECK_TIMEOUT,
            allow_redirects=True,
        )
        # 4xx = expired/removed. 2xx/3xx = alive. 5xx = server error, treat as alive.
        return resp.status_code < 400
    except Exception:
        return True  # network error — assume alive, don't block on uncertainty

_CREDS_PATH = os.path.join(_REPO_ROOT, "google_credentials.json")
_SHEET_NAME = "Job_Application_Tracker"
_TAB_NAME   = "Job Discovery"
_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# Column positions (must match scraper/sheets.py)
_COL_LINK          = 10  # J: Apply Link
_COL_FOLDER        = 11  # K: Output Folder
_COL_RESUME_STATUS = 13  # M: Resume Status
_COL_NOTES         = 14  # N: Notes
_COL_APP_STATUS    = 16  # P: Application Status

_TRIGGER_RESUME_ONLY = "Resume Only"
_TRIGGER_RESUME_CL   = "Resume + Cover Letter"


def _get_worksheet() -> gspread.Worksheet | None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    creds  = Credentials.from_service_account_file(_CREDS_PATH, scopes=_SCOPES)
    client = gspread.authorize(creds)
    try:
        ss = client.open_by_key(spreadsheet_id) if spreadsheet_id else client.open(_SHEET_NAME)
        return ss.worksheet(_TAB_NAME)
    except WorksheetNotFound:
        return None


def _update_folder_cell(ws: gspread.Worksheet, row_idx: int, output_folder: str) -> None:
    try:
        ws.update_cell(row_idx, _COL_FOLDER, output_folder)
    except Exception as e:
        print(f"  [trigger] Warning: could not update output folder cell: {e}")


def _set_app_status_applied(ws: gspread.Worksheet, row_idx: int) -> None:
    """Set Application Status (P) to 'Applied' only if it is still '—' or empty."""
    try:
        current = ws.cell(row_idx, _COL_APP_STATUS).value or ""
        if current.strip() in ("", "—"):
            ws.update_cell(row_idx, _COL_APP_STATUS, "Applied")
    except Exception as e:
        print(f"  [trigger] Warning: could not update application status: {e}")


def poll_and_generate() -> int:
    """
    Check the sheet for rows the user has marked for generation.

    Returns:
        Number of documents successfully generated.
    """
    ws = _get_worksheet()
    if ws is None:
        print("  [trigger] 'Job Discovery' tab not found — skipping poll.")
        return 0

    records    = ws.get_all_records()
    generated  = 0

    for i, row in enumerate(records, start=2):  # row 2 = first data row
        resume_status = str(row.get("Resume Status", "")).strip()
        if resume_status not in (_TRIGGER_RESUME_ONLY, _TRIGGER_RESUME_CL):
            continue

        apply_link    = str(row.get("Apply Link",    "")).strip()
        output_folder = str(row.get("Output Folder", "")).strip()
        company       = str(row.get("Company", f"Row {i}"))
        gen_cl        = (resume_status == _TRIGGER_RESUME_CL)

        print(f"  [trigger] {resume_status} triggered for {company} (row {i})")

        # Check if the job link is still live before running the pipeline
        if apply_link and not _is_link_alive(apply_link):
            print(f"  [trigger] Row {i}: link returned 4xx — job posting likely expired")
            ws.update_cell(i, _COL_RESUME_STATUS, "Link expired ✗")
            try:
                ws.update_cell(i, _COL_NOTES, "Job posting no longer exists (404). Use generic resume.")
            except Exception:
                pass
            continue

        # Load full job dict (description needed by pipeline)
        job = load_job_from_cache(apply_link)
        if job is None:
            print(f"  [trigger] Row {i}: job not in cache — link={apply_link[:80]}")
            ws.update_cell(i, _COL_RESUME_STATUS, "Failed ✗")
            try:
                ws.update_cell(i, _COL_NOTES, "Job not in local cache. Re-scrape to refresh.")
            except Exception:
                pass
            continue

        # Mark as generating so the user sees progress
        ws.update_cell(i, _COL_RESUME_STATUS, "Generating...")

        try:
            # --- Run resume pipeline ---
            results = trigger_for_jobs([job])
            result  = results[0] if results else {}
            output_folder   = result.get("output_folder", "")
            pipeline_ok     = result.get("status", "failed") == "completed"

            if not pipeline_ok or not output_folder:
                error_msg = result.get("error") or "Pipeline returned no output folder"
                print(f"  [trigger] Resume generation failed: {error_msg}")
                ws.update_cell(i, _COL_RESUME_STATUS, "Failed ✗")
                try:
                    ws.update_cell(i, _COL_NOTES, f"Resume failed: {error_msg[:200]}")
                except Exception:
                    pass
                continue

            # Update output folder in sheet
            _update_folder_cell(ws, i, output_folder)

            # --- Optionally generate cover letter ---
            if gen_cl:
                try:
                    cl_ok = generate_and_save(output_folder)
                except Exception as e:
                    cl_ok = False
                    print(f"  [trigger] CL generation error: {e}")

                final_status = "Done ✓" if cl_ok else "Done ✓ (CL failed)"
                ws.update_cell(i, _COL_RESUME_STATUS, final_status)

                if cl_ok:
                    _set_app_status_applied(ws, i)
                    print(f"  [trigger] Resume + CL done: {company}")
                else:
                    print(f"  [trigger] Resume done, CL failed: {company}")
            else:
                ws.update_cell(i, _COL_RESUME_STATUS, "Done ✓")
                print(f"  [trigger] Resume done: {company}")

            generated += 1

        except Exception as e:
            print(f"  [trigger] ERROR for {company}: {e}")
            try:
                ws.update_cell(i, _COL_RESUME_STATUS, "Failed ✗")
                ws.update_cell(i, _COL_NOTES, f"Error: {str(e)[:200]}")
            except Exception:
                pass

    if generated:
        print(f"  [trigger] Generated {generated} document set(s) this poll.")
    return generated


if __name__ == "__main__":
    poll_and_generate()
