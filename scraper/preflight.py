"""
scraper/preflight.py

Pre-flight health checks run before the scheduler starts.

Critical failures abort the scheduler entirely — there is no point running
if resumes can't be generated or the sheet can't be written to.

Warnings are logged but execution continues.

Checks:
  Critical:
    1. google_credentials.json — exists and is valid service-account JSON
    2. Google Sheets — can authenticate and open the spreadsheet
    3. Ankush_Master_Data.json — exists and is valid JSON
    4. Output directory — exists (or can be created) and is writable
    5. ANTHROPIC_API_KEY — set in environment
    6. pdflatex — installed and on PATH

  Warnings:
    - SERPAPI_KEY missing → Google Jobs scraping disabled
    - LINKEDIN_COOKIE missing → LinkedIn results may be limited
    - Telegram tokens missing → notifications disabled
    - GOOGLE_SHEETS_SPREADSHEET_ID missing → sheet opened by name (slower)
    - profile_cache.txt missing → will regenerate (adds ~10s on first run)
"""

import json
import os
import subprocess
import sys

_REPO_ROOT        = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CREDS_PATH       = os.path.join(_REPO_ROOT, "google_credentials.json")
_MASTER_DATA_PATH = os.path.join(_REPO_ROOT, "Ankush_Master_Data.json")
_PROFILE_CACHE    = os.path.join(_REPO_ROOT, "profile_cache.txt")
_OUTPUT_BASE      = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
_SHEET_NAME       = "Job_Application_Tracker"

_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def _check(label: str, ok: bool, errors: list, message: str = "") -> None:
    if ok:
        print(f"  ✓  {label}")
    else:
        print(f"  ✗  {label}" + (f": {message}" if message else ""))
        errors.append(f"{label}" + (f": {message}" if message else ""))


def _warn(label: str, warnings: list, message: str = "") -> None:
    print(f"  ⚠  {label}" + (f": {message}" if message else ""))
    warnings.append(label)


def run_preflight() -> bool:
    """
    Run all health checks and print a summary.
    Returns True if all critical checks pass (scheduler may start).
    Returns False if any critical check fails (scheduler should abort).
    """
    errors:   list[str] = []
    warnings: list[str] = []

    print("\n" + "=" * 60)
    print("  PRE-FLIGHT HEALTH CHECKS")
    print("=" * 60)

    # ── 1. ANTHROPIC_API_KEY ──────────────────────────────────────────────────
    _check(
        "ANTHROPIC_API_KEY set",
        bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        errors,
        "Set ANTHROPIC_API_KEY in .env",
    )

    # ── 2. google_credentials.json ────────────────────────────────────────────
    creds_ok = False
    if not os.path.exists(_CREDS_PATH):
        _check("google_credentials.json", False, errors, "file not found")
    else:
        try:
            with open(_CREDS_PATH) as f:
                creds_data = json.load(f)
            if "client_email" not in creds_data or "private_key" not in creds_data:
                _check("google_credentials.json", False, errors, "missing client_email or private_key")
            else:
                _check("google_credentials.json", True, errors)
                creds_ok = True
        except json.JSONDecodeError as e:
            _check("google_credentials.json", False, errors, f"invalid JSON: {e}")

    # ── 3. Google Sheets connection ───────────────────────────────────────────
    if creds_ok:
        try:
            from google.oauth2.service_account import Credentials
            import gspread

            creds  = Credentials.from_service_account_file(_CREDS_PATH, scopes=_SCOPES)
            client = gspread.authorize(creds)
            spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
            ss = client.open_by_key(spreadsheet_id) if spreadsheet_id else client.open(_SHEET_NAME)
            _check(f"Google Sheets connected ('{ss.title}')", True, errors)
        except Exception as e:
            _check("Google Sheets connection", False, errors, str(e))
    else:
        print("  —  Google Sheets: skipped (credentials unavailable)")

    # ── 4. Ankush_Master_Data.json ────────────────────────────────────────────
    if not os.path.exists(_MASTER_DATA_PATH):
        _check("Ankush_Master_Data.json", False, errors, "file not found")
    else:
        try:
            with open(_MASTER_DATA_PATH) as f:
                data = json.load(f)
            required = ("personal_info", "experience", "projects", "technical_skills")
            missing  = [k for k in required if k not in data]
            if missing:
                _check("Ankush_Master_Data.json", False, errors, f"missing keys: {missing}")
            else:
                _check("Ankush_Master_Data.json", True, errors)
        except json.JSONDecodeError as e:
            _check("Ankush_Master_Data.json", False, errors, f"invalid JSON: {e}")

    # ── 5. Output directory ───────────────────────────────────────────────────
    try:
        os.makedirs(_OUTPUT_BASE, exist_ok=True)
        test_path = os.path.join(_OUTPUT_BASE, ".preflight_write_test")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        _check(f"Output directory writable ({_OUTPUT_BASE})", True, errors)
    except Exception as e:
        _check("Output directory", False, errors, str(e))

    # ── 6. pdflatex ──────────────────────────────────────────────────────────
    try:
        result = subprocess.run(
            ["pdflatex", "--version"],
            capture_output=True,
            timeout=10,
        )
        _check("pdflatex installed", result.returncode == 0, errors,
               "pdflatex returned non-zero — check your TeX installation")
    except FileNotFoundError:
        _check("pdflatex installed", False, errors,
               "not found — install TeX Live: 'brew install --cask mactex-no-gui'")
    except subprocess.TimeoutExpired:
        _warn("pdflatex check timed out — may run slowly", warnings)

    # ── Warnings ─────────────────────────────────────────────────────────────
    if not os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip():
        _warn("GOOGLE_SHEETS_SPREADSHEET_ID not set", warnings,
              "sheet opened by name — set this for reliability")

    if not os.getenv("SERPAPI_KEY", "").strip():
        _warn("SERPAPI_KEY not set", warnings,
              "Google Jobs scraping disabled")

    if not os.getenv("LINKEDIN_COOKIE", "").strip():
        _warn("LINKEDIN_COOKIE not set", warnings,
              "LinkedIn results may be empty or rate-limited")

    if not (os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            and os.getenv("TELEGRAM_CHAT_ID", "").strip()):
        _warn("Telegram not configured", warnings,
              "high-match notifications disabled")

    if not os.path.exists(_PROFILE_CACHE):
        _warn("profile_cache.txt not found", warnings,
              "will regenerate on first run (~10s delay)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if not errors:
        status = f"All critical checks passed"
        if warnings:
            status += f" | {len(warnings)} warning(s)"
        print(f"  ✓  {status}")
        print("=" * 60 + "\n")
        return True
    else:
        print(f"  ✗  {len(errors)} critical check(s) failed — scheduler aborted.")
        print("     Fix the errors above and restart.")
        print("=" * 60 + "\n")
        return False
