"""
tests/test_cross_document_project_bullets_list.py

Regression test for a real crash (Sep 2026): after project bullets moved to a
`bullets` list, the writer leaves the legacy bullet_1/bullet_2 fields as None.
validate_cross_document_consistency() still did `pattern.search(pb.get(field))`
on those — TypeError on None, killing the editor node mid-run AFTER the paid
writer call had already been made. It must iterate the `bullets` list (and
only ever real strings), keep flagging tech that isn't in the project's stack,
and still support the legacy pair.

Run: pytest tests/test_cross_document_project_bullets_list.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from graph import validate_cross_document_consistency

_MASTER_DATA = {
    "experience": [
        {"company": "TCS", "title": "System Engineer",
         "bullets": [{"text": "Built things.", "technologies_used": ["Angular"]}]},
    ],
    "projects": [
        {"name": "Zero-Knowledge Password Authentication",
         "tech_stack": ["Circom", "Node.js", "React"],
         "bullets": [{"text": "a"}, {"text": "b"}]},
    ],
    "personal_info": {},
}


def _draft(project_entry):
    return {
        "tailored_summary": "Engineer with 4+ years of experience.",
        "tailored_bullets": [],
        "tailored_project_bullets": [project_entry],
    }


def test_bullets_list_with_none_legacy_fields_does_not_crash():
    draft = _draft({
        "project_name": "Zero-Knowledge Password Authentication",
        "bullet_1": None, "bullet_2": None,
        "bullets": ["Built it with Circom circuits.", "Served it from a Node.js backend."],
    })
    report = validate_cross_document_consistency(draft, _MASTER_DATA)  # must not raise
    assert not [i for i in report["issues"] if i["type"] == "tech_mismatch_project"]


def test_tech_outside_project_stack_is_flagged_inside_bullets_list():
    draft = _draft({
        "project_name": "Zero-Knowledge Password Authentication",
        "bullet_1": None, "bullet_2": None,
        "bullets": ["Built it with Circom circuits.", "Deployed it on Kubernetes."],
    })
    report = validate_cross_document_consistency(draft, _MASTER_DATA)
    flagged = [i for i in report["issues"] if i["type"] == "tech_mismatch_project"]
    assert flagged and flagged[0]["bullet_field"] == "bullets[1]"
    assert flagged[0]["tech_mentioned"].lower() == "kubernetes"


def test_legacy_pair_still_validated():
    draft = _draft({
        "project_name": "Zero-Knowledge Password Authentication",
        "bullet_1": "Built it with Circom.", "bullet_2": "Deployed it on Kubernetes.",
        "bullets": None,
    })
    report = validate_cross_document_consistency(draft, _MASTER_DATA)
    flagged = [i for i in report["issues"] if i["type"] == "tech_mismatch_project"]
    assert flagged and flagged[0]["bullet_field"] == "bullet_2"
