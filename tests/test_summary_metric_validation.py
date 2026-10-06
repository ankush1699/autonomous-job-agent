"""
tests/test_summary_metric_validation.py

Regression test for a real bug: the writer was observed producing a summary
with no achievement metric at all (a generated Astronomer resume's summary
said "delivered full-stack features and backend APIs serving production
workloads" with zero number, even though real metrics like 10K+ users / 40%
existed and were sitting in that same resume's experience bullets). Fixed by
a deterministic check in validate_writer_output() that also blocks the
"skip the editor" shortcut so the editor gets a chance to fix it.

Sep 2026: the summary is now written from a brief, not a fixed three-sentence
template, so the check is no longer tied to an "At {company}, ..." sentence.
The rule is: after removing the "N+ years" figure (a metric-shaped token that
would make a whole-summary check trivially pass), at least one remaining
number in the summary must actually exist in a master-data experience bullet.
A number that isn't in master data is worse than none — it's fabricated.

Run: pytest tests/test_summary_metric_validation.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from graph import validate_writer_output

_MASTER_DATA = {
    "experience": [
        {
            "company": "Tata Consultancy Services",
            "bullets": [
                {"text": "Served 10K+ active users.", "tags": []},
                {"text": "Cut deployment time by 40%.", "tags": []},
            ],
        }
    ],
    "projects": [],
    "personal_info": {"availability": "F-1 OPT, available July 2026"},
}

_FLAG = "no real, verifiable number"


def test_summary_with_no_metric_is_flagged():
    """
    The exact real bug: "4+ years of experience" is a metric-shaped token, so
    a naive whole-summary check would pass even though the summary asserts
    nothing measurable. The years figure must not count.
    """
    draft = {
        "tailored_summary": (
            "Full-stack engineer with 4+ years of experience. At Tata Consultancy Services, "
            "delivered full-stack features and backend APIs serving production workloads. "
            "Available on F-1 OPT."
        ),
        "tailored_bullets": [],
    }
    report = validate_writer_output(draft, _MASTER_DATA)
    assert any(_FLAG in issue for issue in report["summary_issues"])
    # A summary-only problem must still fail `passed`, or editor_node's
    # "skip the LLM, nothing to fix" shortcut would skip right past it.
    assert report["passed"] is False


def test_summary_with_a_real_metric_is_not_flagged():
    draft = {
        "tailored_summary": (
            "Full-stack engineer with 4+ years of experience. At Tata Consultancy Services, "
            "served 10K+ active users on a production platform."
        ),
        "tailored_bullets": [],
    }
    report = validate_writer_output(draft, _MASTER_DATA)
    assert not any(_FLAG in issue for issue in report["summary_issues"])


def test_real_metric_anywhere_in_summary_satisfies_the_check():
    """
    No "At {company}" sentence required anymore — the summary is written as a
    person, and a real number in the first sentence counts.
    """
    draft = {
        "tailored_summary": (
            "Engineer who shipped a regulated fintech platform to 10K+ users over 4+ years, "
            "and spent the past year building agentic LLM systems."
        ),
        "tailored_bullets": [],
    }
    report = validate_writer_output(draft, _MASTER_DATA)
    assert not any(_FLAG in issue for issue in report["summary_issues"])


def test_fabricated_metric_does_not_satisfy_the_check():
    """
    A number that exists nowhere in master data must not count as evidence —
    the whole point is real, verifiable numbers, and an invented one is the
    worst outcome, not a pass.
    """
    draft = {
        "tailored_summary": (
            "Full-stack engineer with 4+ years of experience who improved reliability by 99% "
            "at Tata Consultancy Services."
        ),
        "tailored_bullets": [],
    }
    report = validate_writer_output(draft, _MASTER_DATA)
    assert any(_FLAG in issue for issue in report["summary_issues"])
