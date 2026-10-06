"""
tests/test_filler_tail_and_project_traceability.py

Two deterministic guards added Sep 2026 after real runs:

1. strip_filler_tail(): LLMs bolt a participial keyword-echo clause onto a
   finished bullet (", demonstrating hands-on proficiency with agentic systems
   and performance optimization."). It adds no fact and is a recognizable
   generated-resume pattern; the finalizer strips it as the last step before
   render. Real clauses that carry a result (", reducing setup time by 35%.")
   must be untouched.

2. enforce_project_bullet_traceability(): same fidelity floor as experience
   bullets, applied index-by-index to each project's `bullets` list.

Run: pytest tests/test_filler_tail_and_project_traceability.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from graph import strip_filler_tail, enforce_project_bullet_traceability


def test_strips_demonstrating_tail():
    t = ("Built an end-to-end job search system with a 6-node LangGraph pipeline, "
         "demonstrating hands-on proficiency with agentic systems and performance optimization.")
    assert strip_filler_tail(t) == "Built an end-to-end job search system with a 6-node LangGraph pipeline."


def test_strips_bringing_tail_in_summary_sentence():
    t = "Built a 6-node LangGraph agentic pipeline backed by a 219-test suite, bringing a scientific, test-driven approach to agentic system design."
    assert strip_filler_tail(t) == "Built a 6-node LangGraph agentic pipeline backed by a 219-test suite."


def test_strips_grounding_tail_from_summary_sentence():
    t = ("Built a multi-node LangGraph agentic pipeline end to end, complete with a 219-test suite, and "
         "red-teamed LLM systems during a paid AI fellowship, grounding recent AI work in a scientific, "
         "test-backed approach to system design.")
    assert strip_filler_tail(t) == ("Built a multi-node LangGraph agentic pipeline end to end, complete with a "
                                    "219-test suite, and red-teamed LLM systems during a paid AI fellowship.")


def test_result_clause_is_not_stripped():
    t = "Designed a reusable framework covering project structure and base UI components, reducing new project setup time by 35%."
    assert strip_filler_tail(t) == t


def test_mid_sentence_verb_is_not_stripped():
    # "demonstrating" NOT as a sentence-final tail clause -> untouched
    t = "Wrote adversarial prompts demonstrating reasoning failures, then documented them in a shared knowledge base."
    assert strip_filler_tail(t) == t


_MASTER_PROJECTS = [{
    "name": "Autonomous Job Search & Resume Pipeline",
    "bullets": [
        {"text": "Built an end-to-end autonomous job search system combining a multi-platform scraper with a 6-node LangGraph agentic pipeline that automatically tailors resumes and cover letters."},
        {"text": "Backed the pipeline with a 219-test Pytest suite covering scoring logic, cache invalidation, and scraper filters."},
    ],
}]


def test_faithful_project_rewrite_is_kept():
    tailored = [{"project_name": "Autonomous Job Search & Resume Pipeline", "bullets": [
        "Built an autonomous job search system: a multi-platform scraper feeding a 6-node LangGraph agentic pipeline that tailors resumes and cover letters automatically.",
        "Backed it with a 219-test Pytest suite over scoring logic, cache invalidation, and scraper filters.",
    ]}]
    out, reverts = enforce_project_bullet_traceability(tailored, _MASTER_PROJECTS)
    assert reverts == [] and out[0]["bullets"][0].startswith("Built an autonomous")


def test_drifted_or_number_dropping_project_bullet_is_reverted():
    tailored = [{"project_name": "Autonomous Job Search & Resume Pipeline", "bullets": [
        "Delivered an innovative AI solution leveraging cutting-edge techniques for job seekers.",     # drift
        "Backed the pipeline with a comprehensive Pytest suite covering scoring logic, cache invalidation, and scraper filters.",  # dropped 219
    ]}]
    out, reverts = enforce_project_bullet_traceability(tailored, _MASTER_PROJECTS)
    assert len(reverts) == 2
    assert out[0]["bullets"][0] == _MASTER_PROJECTS[0]["bullets"][0]["text"]
    assert "219-test" in out[0]["bullets"][1]


def test_fuzzy_project_name_and_legacy_entries_pass_through():
    tailored = [
        {"project_name": "Autonomous Job Search", "bullets": ["Totally unrelated marketing copy about synergy."]},  # fuzzy match -> revert
        {"project_name": "Something Else", "bullet_1": "a", "bullet_2": "b"},                                        # legacy -> untouched
    ]
    out, reverts = enforce_project_bullet_traceability(tailored, _MASTER_PROJECTS)
    assert len(reverts) == 1 and out[0]["bullets"][0].startswith("Built an end-to-end")
    assert out[1] == tailored[1]
