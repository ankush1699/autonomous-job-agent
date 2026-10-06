"""
scraper/sheets.py

Writes scraped + scored job results to the "Job Discovery" tab in Google Sheets.

Column schema (A-Q):
  A: Score          B: Company        C: Job Title      D: Platform
  E: Location       F: Remote         G: Date Posted    H: JD Summary
  I: Fit Reasoning  J: Apply Link     K: Output Folder  L: Job Type
  M: Resume Status  N: Notes          O: Salary         P: Application Status
  Q: Date Added (when this pipeline scored/wrote the row — ISO 8601 UTC)

Rules:
  - Dedup by Apply Link (column J). Never write a row that already exists.
  - Never overwrite columns M, N after the initial row is written (user fills these).
  - Application Status (P) is auto-set to "Applied" after CL generation, but
    never overwritten if the user has already changed it.
  - Sort all rows by Score descending after each write batch.
  - GOOGLE_SHEETS_SPREADSHEET_ID env var takes precedence; falls back to name-based open.
"""

import os
import sys
import datetime

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from google.oauth2.service_account import Credentials
import gspread
from gspread.exceptions import WorksheetNotFound

_CREDS_PATH = os.path.join(_REPO_ROOT, "google_credentials.json")
_SHEET_NAME = "Job_Application_Tracker"
_TAB_NAME = "Job Discovery"

_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# Column positions (1-indexed)
_COL_SCORE = 1       # A
_COL_COMPANY = 2     # B
_COL_TITLE = 3       # C
_COL_PLATFORM = 4    # D
_COL_LOCATION = 5    # E
_COL_REMOTE = 6      # F
_COL_DATE = 7        # G
_COL_JD_SUMMARY = 8  # H
_COL_REASONING = 9   # I
_COL_LINK = 10       # J  ← dedup key
_COL_FOLDER = 11     # K
_COL_JOB_TYPE = 12   # L
_COL_RESUME_STATUS = 13  # M  ← user fills, never overwrite
_COL_NOTES = 14          # N  ← user fills, never overwrite
_COL_SALARY = 15         # O  ← auto-extracted from JD
_COL_APP_STATUS = 16     # P  ← auto "Applied" after CL; user updates thereafter
_COL_DATE_ADDED = 17     # Q  ← when THIS pipeline scored/wrote the row (distinct
                         #      from Date Posted, which is the platform's own
                         #      listing date and may be blank/unreliable)

_HEADERS = [
    "Score", "Company", "Job Title", "Platform", "Location", "Remote",
    "Date Posted", "JD Summary", "Fit Reasoning", "Apply Link",
    "Output Folder", "Job Type", "Resume Status", "Notes", "Salary", "Application Status",
    "Date Added",
]

_RESUME_STATUS_OPTIONS = ["—", "Resume Only", "Resume + Cover Letter", "Use Generic", "Generating...", "Done ✓", "Failed ✗", "Skip"]
_APP_STATUS_OPTIONS    = ["—", "Applied", "Phone Screen", "Interview", "Rejected", "Offer"]


def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(_CREDS_PATH, scopes=_SCOPES)
    return gspread.authorize(creds)


def _open_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    if spreadsheet_id:
        return client.open_by_key(spreadsheet_id)
    return client.open(_SHEET_NAME)


def _get_or_create_tab(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(_TAB_NAME)
        _ensure_new_columns(spreadsheet, ws)  # add Salary / Application Status if missing
    except WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=_TAB_NAME, rows=1000, cols=len(_HEADERS))
        ws.append_row(_HEADERS, value_input_option="RAW")
        _apply_all_validations(spreadsheet, ws)
    return ws


def _ensure_new_columns(spreadsheet: gspread.Spreadsheet, ws: gspread.Worksheet) -> None:
    """Append Salary, Application Status, and Date Added columns if not yet in the header row."""
    try:
        header = ws.row_values(1)
        missing = [h for h in ["Salary", "Application Status", "Date Added"] if h not in header]
        if not missing:
            return
        updated = header + missing
        ws.update("A1", [updated])
        print(f"  [sheets] Added new columns: {', '.join(missing)}")
        _apply_all_validations(spreadsheet, ws)
    except Exception as e:
        print(f"  [sheets] Warning: _ensure_new_columns failed: {e}")


def _apply_all_validations(spreadsheet: gspread.Spreadsheet, ws: gspread.Worksheet) -> None:
    """Set dropdown validation on Resume Status (M) and Application Status (P)."""
    try:
        requests = [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "startColumnIndex": _COL_RESUME_STATUS - 1,
                        "endColumnIndex": _COL_RESUME_STATUS,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": v} for v in _RESUME_STATUS_OPTIONS],
                        },
                        "showCustomUi": True,
                        "strict": False,
                    },
                }
            },
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "startColumnIndex": _COL_APP_STATUS - 1,
                        "endColumnIndex": _COL_APP_STATUS,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": v} for v in _APP_STATUS_OPTIONS],
                        },
                        "showCustomUi": True,
                        "strict": False,
                    },
                }
            },
        ]
        ws.spreadsheet.batch_update({"requests": requests})
    except Exception as e:
        print(f"  [sheets] Warning: could not set data validation: {e}")


def _load_existing_links(ws: gspread.Worksheet) -> set[str]:
    """Return set of all Apply Link values already in the sheet (dedup key)."""
    try:
        all_values = ws.col_values(_COL_LINK)
        return set(v.strip() for v in all_values[1:] if v.strip())  # skip header
    except Exception:
        return set()


def _job_to_row(job: dict) -> list:
    """Convert a job dict to a sheet row (17 columns, A–Q)."""
    return [
        job.get("score", 0),
        job.get("company", ""),
        job.get("title", ""),
        job.get("platform", ""),
        job.get("location", ""),
        "Yes" if job.get("is_remote") else "No",
        job.get("date_posted", ""),
        (job.get("jd_summary", "") or "")[:500],   # cap to avoid cell overflow
        (job.get("reasoning", "") or "")[:300],
        job.get("apply_link", ""),
        job.get("output_folder", ""),
        job.get("job_type", ""),
        "pending",   # M: Resume Status — initial value only
        "",          # N: Notes — always blank on creation
        job.get("salary", ""),  # O: Salary — auto-extracted from JD
        "—",         # P: Application Status — initial value
        datetime.datetime.now(datetime.timezone.utc).isoformat(),  # Q: Date Added
    ]


def write_jobs(jobs: list[dict]) -> int:
    """
    Write new jobs to the Job Discovery tab.

    Skips any job whose apply_link already exists in the sheet.
    Sorts all data rows by Score (column A) descending after writing.
    Never overwrites columns M (Resume Status) or N (Notes) for existing rows.

    Returns:
        Number of new rows written.
    """
    if not jobs:
        return 0

    client = _get_client()
    spreadsheet = _open_spreadsheet(client)
    ws = _get_or_create_tab(spreadsheet)

    existing_links = _load_existing_links(ws)

    new_rows = []
    for job in jobs:
        link = (job.get("apply_link") or "").strip()
        if not link:
            continue
        if link in existing_links:
            print(f"  [sheets] Sheet dedup: {link[:80]} already exists in sheet")
            continue
        new_rows.append(_job_to_row(job))
        existing_links.add(link)

    if not new_rows:
        print(f"  [sheets] No new jobs to write (all {len(jobs)} already in sheet).")
        return 0

    ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    print(f"  [sheets] Wrote {len(new_rows)} new rows to '{_TAB_NAME}'.")

    _sort_by_score(ws)
    return len(new_rows)


def _sort_by_score(ws: gspread.Worksheet):
    """Sort data rows by Score column (A) descending, preserving header."""
    try:
        ws.spreadsheet.batch_update({
            "requests": [{
                "sortRange": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 1,  # skip header
                        "startColumnIndex": 0,
                        "endColumnIndex": len(_HEADERS),
                    },
                    "sortSpecs": [{
                        "dimensionIndex": 0,  # column A (Score)
                        "sortOrder": "DESCENDING",
                    }],
                }
            }]
        })
    except Exception as e:
        print(f"  [sheets] Warning: sort failed: {e}")


def update_output_folder(apply_link: str, output_folder: str):
    """Update the Output Folder cell (K) for a row matched by apply_link."""
    try:
        client = _get_client()
        spreadsheet = _open_spreadsheet(client)
        ws = _get_or_create_tab(spreadsheet)
        links = ws.col_values(_COL_LINK)
        for idx, link in enumerate(links):
            if link.strip() == apply_link.strip():
                ws.update_cell(idx + 1, _COL_FOLDER, output_folder)
                return
    except Exception as e:
        print(f"  [sheets] Warning: could not update output_folder: {e}")


# ---------------------------------------------------------------------------
# Session-based API — open once per scrape cycle, write per job, sort at end.
# Avoids re-authenticating and re-loading existing links for every job.
# ---------------------------------------------------------------------------

class SheetSession:
    """
    Holds an open worksheet and an in-memory link cache for one scrape cycle.
    Usage:
        session = open_sheet_session()
        session.write_job(job)          # returns True if written, False if deduped
        session.update_folder(link, folder)
        session.sort()                  # call once at end of cycle
    """

    def __init__(self, ws: "gspread.Worksheet", existing_links: set):
        self._ws = ws
        self._links = existing_links

    def write_job(self, job: dict) -> bool:
        """
        Append one job row. Returns True if written, False if already in sheet.
        Never raises — logs and returns False on error.
        """
        link = (job.get("apply_link") or "").strip()
        if not link:
            return False
        if link in self._links:
            print(f"  [sheets] Sheet dedup: {link[:80]} already exists")
            return False
        try:
            self._ws.append_row(_job_to_row(job), value_input_option="USER_ENTERED")
            self._links.add(link)
            print(f"  [sheets] Written: {job.get('company')} — {job.get('title')} (score={job.get('score')})")
            return True
        except Exception as e:
            print(f"  [sheets] Warning: write_job failed — {e}")
            return False

    def update_folder(self, apply_link: str, output_folder: str) -> None:
        """Update Output Folder cell (K) for a row matched by apply_link."""
        try:
            links = self._ws.col_values(_COL_LINK)
            for idx, link in enumerate(links):
                if link.strip() == apply_link.strip():
                    self._ws.update_cell(idx + 1, _COL_FOLDER, output_folder)
                    return
        except Exception as e:
            print(f"  [sheets] Warning: update_folder failed — {e}")

    def update_application_status(self, apply_link: str, status: str) -> None:
        """
        Set Application Status (P) for a row matched by apply_link.
        Only updates if current value is '—' or empty — never overwrites user edits.
        """
        try:
            links = self._ws.col_values(_COL_LINK)
            for idx, link in enumerate(links):
                if link.strip() == apply_link.strip():
                    current = self._ws.cell(idx + 1, _COL_APP_STATUS).value or ""
                    if current.strip() in ("", "—"):
                        self._ws.update_cell(idx + 1, _COL_APP_STATUS, status)
                    return
        except Exception as e:
            print(f"  [sheets] Warning: update_application_status failed — {e}")

    def sort(self) -> None:
        """Sort all data rows by Score descending. Call once at end of cycle."""
        _sort_by_score(self._ws)


def read_all_rows() -> list[dict]:
    """
    Return every data row in the Job Discovery tab as a dict keyed by header
    name (Score, Company, Job Title, ..., Apply Link, ...). Used one-time by
    server.py's sheet-import endpoint to backfill jobs that were scored
    before the scraper switched to writing directly to entries.json.
    """
    client = _get_client()
    spreadsheet = _open_spreadsheet(client)
    ws = _get_or_create_tab(spreadsheet)
    return ws.get_all_records()


def delete_rows_below_score(min_score: int, skip_applied: bool = True) -> dict:
    """
    Declutter the sheet itself: keep only rows with Score >= min_score, or
    (if skip_applied) any row whose Application Status shows real action
    taken (e.g. "Applied") regardless of score. Implemented as
    read-everything -> filter -> clear body -> rewrite kept rows, rather
    than deleting scattered row indices one at a time — that approach is
    immune to the index-shifting bugs that come from deleting non-contiguous
    rows out of a live sheet.
    """
    client = _get_client()
    spreadsheet = _open_spreadsheet(client)
    ws = _get_or_create_tab(spreadsheet)

    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        return {"deleted_count": 0, "kept_count": 0}

    header, data_rows = all_values[0], all_values[1:]
    score_idx = header.index("Score") if "Score" in header else None
    status_idx = header.index("Application Status") if "Application Status" in header else None

    kept, deleted_count = [], 0
    for row in data_rows:
        score = 0
        if score_idx is not None and score_idx < len(row) and row[score_idx]:
            try:
                score = int(row[score_idx])
            except ValueError:
                score = 0
        applied = (
            skip_applied and status_idx is not None and status_idx < len(row)
            and row[status_idx].strip() not in ("", "—")
        )
        if score >= min_score or applied:
            kept.append(row)
        else:
            deleted_count += 1

    if deleted_count == 0:
        return {"deleted_count": 0, "kept_count": len(kept)}

    last_col_letter = gspread.utils.rowcol_to_a1(1, len(header)).rstrip("1")
    ws.batch_clear([f"A2:{last_col_letter}{len(data_rows) + 1}"])
    if kept:
        ws.update("A2", kept, value_input_option="USER_ENTERED")
    return {"deleted_count": deleted_count, "kept_count": len(kept)}


def open_sheet_session() -> SheetSession:
    """
    Authenticate, open the spreadsheet, and return a SheetSession.
    Call once at the start of a scrape cycle.
    Raises on auth/network failure so the caller can decide whether to proceed without sheets.
    """
    client = _get_client()
    spreadsheet = _open_spreadsheet(client)
    ws = _get_or_create_tab(spreadsheet)
    existing_links = _load_existing_links(ws)
    print(f"  [sheets] Session opened — {len(existing_links)} existing rows cached")
    return SheetSession(ws, existing_links)
