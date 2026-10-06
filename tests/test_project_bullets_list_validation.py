"""
tests/test_project_bullets_list_validation.py

Sep 2026: project bullets moved from a fixed bullet_1/bullet_2 pair to a
`bullets` list (every bullet of the project, rewritten, same count as the
original). validate_writer_output() must check every string in that list for
invented metrics and banned phrases — the real bug this guards against: a
"100%" that existed nowhere in master data shipped on a real resume because
the old rule told the model bullet_2 MUST end in a number.

Run: pytest tests/test_project_bullets_list_validation.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from graph import validate_writer_output

_MASTER_DATA = {
    "experience": [{"company": "TCS", "bullets": [{"text": "Served 10K+ users.", "tags": []}]}],
    "projects": [
        {
            "name": "Zero-Knowledge Password Authentication",
            "bullets": [
                {"text": "Built a secure authentication system using zero-knowledge proofs."},
                {"text": "Developed a Node.js backend and React frontend with end-to-end tests."},
            ],
        }
    ],
    "personal_info": {},
}

_OK_SUMMARY = "Engineer who served 10K+ users over 4+ years."


def test_invented_metric_inside_bullets_list_is_flagged():
    draft = {
        "tailored_summary": _OK_SUMMARY,
        "tailored_bullets": [],
        "tailored_project_bullets": [{
            "project_name": "Zero-Knowledge Password Authentication",
            "bullets": [
                "Designed a zero-knowledge authentication system with Circom circuits.",
                "Verified resistance to phishing and replay attacks across 100% of defined attack scenarios.",
            ],
        }],
    }
    report = validate_writer_output(draft, _MASTER_DATA)
    flagged = [f for f in report["flagged_bullets"] if f["company"].startswith("[project]")]
    assert len(flagged) == 1
    assert "metric not in master data: '100%'" in flagged[0]["issues"]


def test_metric_free_bullets_list_passes_cleanly():
    draft = {
        "tailored_summary": _OK_SUMMARY,
        "tailored_bullets": [],
        "tailored_project_bullets": [{
            "project_name": "Zero-Knowledge Password Authentication",
            "bullets": [
                "Designed a zero-knowledge authentication system with Circom circuits and Poseidon hashing.",
                "Integrated a Node.js verification pipeline with a React frontend, with end-to-end cryptographic tests against phishing and replay attacks.",
            ],
        }],
    }
    report = validate_writer_output(draft, _MASTER_DATA)
    assert not [f for f in report["flagged_bullets"] if f["company"].startswith("[project]")]


def test_legacy_bullet_1_bullet_2_form_is_still_validated():
    draft = {
        "tailored_summary": _OK_SUMMARY,
        "tailored_bullets": [],
        "tailored_project_bullets": [{
            "project_name": "Zero-Knowledge Password Authentication",
            "bullet_1": "Built it.",
            "bullet_2": "Reached 87% coverage.",   # not in master data for this project
        }],
    }
    report = validate_writer_output(draft, _MASTER_DATA)
    flagged = [f for f in report["flagged_bullets"] if f["company"].startswith("[project]")]
    assert len(flagged) == 1 and "'87%'" in flagged[0]["issues"][0]
