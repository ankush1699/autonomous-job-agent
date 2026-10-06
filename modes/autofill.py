"""
modes/autofill.py — Playwright application form filler.

Given an application URL and a completed pipeline run folder, this opens a
visible browser, fills the fields it can confidently map from master data,
uploads the tailored resume (and cover letter if present), then STOPS.

Hard lines, by design:
  * It NEVER clicks submit — you review and submit yourself.
  * It does not bypass or interact with CAPTCHAs. If one is detected it tells
    you and leaves it entirely to you.
  * It does not auto-answer legal, EEO, or work-authorization questions —
    those are listed for you to answer by hand.
  * No stealth plugins, no fingerprint spoofing, no bot-detection evasion.

Usage:
    python apply.py autofill --url <application url> --folder <run folder>
"""

import os
import re
import sys
import glob
import json

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MASTER_DATA_PATH = os.path.join(_REPO_ROOT, "Ankush_Master_Data.json")

# Questions a human must answer — never auto-filled.
_NEVER_FILL = re.compile(
    r"sponsor|authoriz|visa|citizen|gender|race|ethnic|hispanic|veteran"
    r"|disabilit|salary|compensation|criminal|felony|18 years|referr",
    re.IGNORECASE,
)

_CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']", ".g-recaptcha",
    "iframe[src*='hcaptcha']", ".h-captcha",
    "iframe[src*='turnstile']", "[class*='captcha']",
]


def _load_answers() -> list[tuple[re.Pattern, str]]:
    """Ordered (pattern, value) pairs built from master data. First match wins."""
    with open(_MASTER_DATA_PATH) as f:
        p = json.load(f)["personal_info"]

    first, *rest = p["name"].split()
    last = rest[-1] if rest else ""

    def rx(s):
        return re.compile(s, re.IGNORECASE)

    return [
        (rx(r"first[\s_-]*name|given[\s_-]*name"), first),
        (rx(r"last[\s_-]*name|family[\s_-]*name|surname"), last),
        (rx(r"full[\s_-]*name|your[\s_-]*name|^name$|\bname\b"), p["name"]),
        (rx(r"e-?mail"), p["email"]),
        (rx(r"phone|mobile|tel"), p["phone"]),
        (rx(r"linked[\s_-]*in"), p["hreflinkedin"]),
        (rx(r"github"), p["hrefgithub"]),
        (rx(r"portfolio|website|personal[\s_-]*site|url"), p["hrefportfolio"]),
        (rx(r"city|location|address"), p["location"]),
    ]


def _find_pdf(folder: str, kind: str) -> str | None:
    """kind: 'Resume' or 'Cover_Letter' — matches the finalizer's file naming."""
    hits = sorted(glob.glob(os.path.join(folder, f"*{kind}*.pdf")))
    return hits[0] if hits else None


def _descriptor(el) -> str:
    """Human-readable identity of a form control: label, aria, placeholder, name, id."""
    parts = []
    try:
        el_id = el.get_attribute("id")
        if el_id:
            label = el.page.query_selector(f"label[for='{el_id}']")
            if label:
                parts.append(label.inner_text().strip())
            parts.append(el_id)
    except Exception:
        pass
    for attr in ("aria-label", "placeholder", "name"):
        try:
            v = el.get_attribute(attr)
            if v:
                parts.append(v)
        except Exception:
            pass
    return " | ".join(parts)


def run(url: str, folder: str | None = None) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run:")
        print("  ./venv/bin/pip install playwright && ./venv/bin/playwright install chromium")
        sys.exit(1)

    answers = _load_answers()
    resume_pdf = _find_pdf(folder, "Resume") if folder else None
    cl_pdf = _find_pdf(folder, "Cover_Letter") if folder else None

    if folder and not resume_pdf:
        print(f"WARNING: no resume PDF found in {folder} — file uploads will be skipped.")

    filled, skipped, manual = [], [], []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()
        print(f"[autofill] Opening {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)  # let client-side forms render

        # CAPTCHA detection — inform only, never interact.
        for sel in _CAPTCHA_SELECTORS:
            if page.query_selector(sel):
                print("[autofill] CAPTCHA detected on this page. I will not interact "
                      "with it — solve it yourself before submitting.")
                break

        # Text inputs and textareas
        for el in page.query_selector_all(
            "input[type='text'], input[type='email'], input[type='tel'], "
            "input[type='url'], input:not([type]), textarea"
        ):
            try:
                if not el.is_visible() or not el.is_editable():
                    continue
                desc = _descriptor(el)
                if not desc:
                    continue
                if _NEVER_FILL.search(desc):
                    manual.append(desc)
                    continue
                if (el.input_value() or "").strip():
                    continue  # already has content — don't clobber
                value = next((v for pat, v in answers if pat.search(desc)), None)
                if value:
                    el.fill(value)
                    filled.append(f"{desc}  ←  {value}")
                else:
                    skipped.append(desc)
            except Exception:
                continue

        # File uploads
        for el in page.query_selector_all("input[type='file']"):
            try:
                desc = _descriptor(el) or "file upload"
                if cl_pdf and re.search(r"cover", desc, re.IGNORECASE):
                    el.set_input_files(cl_pdf)
                    filled.append(f"{desc}  ←  {os.path.basename(cl_pdf)}")
                elif resume_pdf and re.search(r"resume|cv|attach", desc, re.IGNORECASE):
                    el.set_input_files(resume_pdf)
                    filled.append(f"{desc}  ←  {os.path.basename(resume_pdf)}")
                else:
                    skipped.append(f"[file] {desc}")
            except Exception:
                continue

        # Dropdowns / radios / checkboxes are always yours to answer.
        n_selects = len(page.query_selector_all("select"))
        n_radios = len(page.query_selector_all("input[type='radio'], input[type='checkbox']"))

        print("\n" + "=" * 60)
        print("AUTOFILL REPORT — nothing has been submitted")
        print("=" * 60)
        print(f"\nFilled ({len(filled)}):")
        for f_ in filled:
            print(f"  ✓ {f_}")
        if manual:
            print(f"\nLeft for YOU (legal/visa/EEO/salary — never auto-filled) ({len(manual)}):")
            for m in manual:
                print(f"  ⚠ {m}")
        if skipped:
            print(f"\nUnrecognized, left blank ({len(skipped)}):")
            for s in skipped[:15]:
                print(f"  · {s}")
        if n_selects or n_radios:
            print(f"\nAlso review: {n_selects} dropdown(s), {n_radios} radio/checkbox item(s).")
        print("\nReview everything, answer the remaining questions, and click "
              "submit YOURSELF. Press Enter here when done to close the browser.")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
        browser.close()
