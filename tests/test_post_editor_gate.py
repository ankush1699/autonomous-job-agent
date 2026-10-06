"""
tests/test_post_editor_gate.py

Sep 2026: the editor LLM's output used to be trusted blindly — every
truthfulness check ran on the writer's draft only. On a real run the editor
introduced a near-duplicate TCS bullet with invented specifics and a
"[METRIC NEEDED]" tag the draft never had, and it shipped. Now the edited
output is validated the same way and reconcile_editor_output() keeps the
draft for any section the editor made worse. Also covers the new first-person
summary check the gate relies on.

Run: pytest tests/test_post_editor_gate.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from graph import reconcile_editor_output, validate_writer_output


def _report(exp_flags=0, proj_flags=0, summary_issues=0):
    return {
        "flagged_bullets": (
            [{"company": "TCS", "bullet_text": "x", "issues": ["metric not in master data"]}] * exp_flags
            + [{"company": "[project] P", "bullet_text": "x", "issues": ["metric not in master data"]}] * proj_flags
        ),
        "summary_issues": ["issue"] * summary_issues,
        "trust_score": 100,
    }


_DRAFT = {
    "tailored_summary": "Draft summary serving 10K+ users.",
    "tailored_bullets": [{"company_name": "TCS", "rewritten_text": "Draft bullet."}],
    "tailored_project_bullets": [{"project_name": "P", "bullets": ["draft p1", "draft p2"]}],
}
_EDITED = {
    "tailored_summary": "Edited summary.",
    "tailored_bullets": [{"company_name": "TCS", "rewritten_text": "Edited bullet."}],
    "tailored_project_bullets": [{"project_name": "P", "bullets": ["edited p1", "edited p2"]}],
    "selected_projects": ["P"],
}


def test_editor_output_kept_when_not_worse():
    out, reasons = reconcile_editor_output(_DRAFT, _EDITED, _report(), _report())
    assert reasons == []
    assert out["tailored_bullets"][0]["rewritten_text"] == "Edited bullet."
    assert out["tailored_summary"] == "Edited summary."


def test_experience_falls_back_when_editor_adds_invented_content():
    out, reasons = reconcile_editor_output(_DRAFT, _EDITED, _report(exp_flags=0), _report(exp_flags=1))
    assert out["tailored_bullets"] == _DRAFT["tailored_bullets"]
    assert out["tailored_project_bullets"] == _EDITED["tailored_project_bullets"]  # untouched section kept
    assert any("experience" in r for r in reasons)


def test_experience_falls_back_when_editor_adds_metric_needed_tag():
    edited = dict(_EDITED, tailored_bullets=[{"company_name": "TCS", "rewritten_text": "New thing. [METRIC NEEDED]"}])
    out, reasons = reconcile_editor_output(_DRAFT, edited, _report(), _report())
    assert out["tailored_bullets"] == _DRAFT["tailored_bullets"]


def test_project_and_summary_fall_back_independently():
    out, reasons = reconcile_editor_output(
        _DRAFT, _EDITED, _report(proj_flags=0, summary_issues=0), _report(proj_flags=1, summary_issues=1)
    )
    assert out["tailored_project_bullets"] == _DRAFT["tailored_project_bullets"]
    assert out["tailored_summary"] == _DRAFT["tailored_summary"]
    assert out["tailored_bullets"] == _EDITED["tailored_bullets"]
    assert len(reasons) == 2


def test_first_person_summary_is_flagged():
    master = {"experience": [{"company": "TCS", "bullets": [{"text": "Served 10K+ users."}]}],
              "projects": [], "personal_info": {}}
    report = validate_writer_output(
        {"tailored_summary": "Engineer with 4+ years. I led delivery for 10K+ users.", "tailored_bullets": []}, master
    )
    assert any("first person" in i for i in report["summary_issues"])
    report = validate_writer_output(
        {"tailored_summary": "Engineer with 4+ years. Led delivery for 10K+ users.", "tailored_bullets": []}, master
    )
    assert not any("first person" in i for i in report["summary_issues"])
