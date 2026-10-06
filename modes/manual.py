"""
modes/manual.py — manual entry mode.

You paste a company name and a JD; the same core engine runs:
should_apply → keywords → strategist → writer → editor → finalizer.
"""

import os
import sys
import json

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

_MASTER_DATA_PATH = os.path.join(_REPO_ROOT, "Ankush_Master_Data.json")


def build_initial_state(company: str, title: str, jd: str,
                        generate_resume: bool = True,
                        generate_cover_letter: bool = False,
                        one_page: bool = False) -> dict:
    with open(_MASTER_DATA_PATH) as f:
        master_data = json.load(f)
    return {
        "company_name": company,
        "job_title": title,
        "job_description": jd,
        "master_data": master_data,
        "generate_resume": generate_resume,
        "generate_cover_letter": generate_cover_letter,
        "one_page": one_page,
        "jd_keywords": [],
        "verified_keywords": [],
        "gap_keywords": [],
        "should_apply": None,
        "triage_score": None,
        "triage_reasoning": None,
        "proceed": False,
        "selected_content": None,
        "selection_report": None,
        "trust_report": None,
        "final_resume_data": None,
        "resume_strategy": None,
        "output_folder": "",
        "pipeline_status": "",
    }


def run(company: str, title: str, jd: str,
        generate_resume: bool = True,
        generate_cover_letter: bool = False) -> dict:
    """Run the full engine for one pasted JD. Returns the final graph state."""
    from graph import app  # deferred: heavy import, needs API keys

    state = build_initial_state(company, title, jd, generate_resume, generate_cover_letter)
    result = app.invoke(state)

    if not result.get("proceed"):
        verdict = result.get("should_apply") or {}
        print(f"\nSkipped: should-apply score {verdict.get('score', '?')}/100 "
              f"(stage: {verdict.get('stage', '?')}).")
        if verdict.get("red_flags"):
            print(f"Red flags: {verdict['red_flags']}")
        if verdict.get("reasoning"):
            print(f"Reason: {verdict['reasoning']}")
    elif result.get("pipeline_status") == "completed":
        print(f"\nDone: {result['output_folder']}")
    return result
