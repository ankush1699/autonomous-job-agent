"""
tests/test_strategist_matches_base_resume.py

Sep 2026 user request: a tailored resume should carry the SAME content depth
as the base resume — same bullet count per employer, all projects included,
no Honors & Achievements — differing only in bullet wording/ordering. Covers
strategist_node's deterministic safety nets (the LLM output is mocked; these
tests exist specifically because the LLM is not trusted to hit an exact count
on its own — same "prompt says X, cheap-tier model doesn't reliably comply"
class of bug this pipeline has hit before):

1. Bullet top-up: fewer than resume_max_bullets selected -> topped up to
   exactly max_bullets (real bullets, quantified-first).
2. Bullet trim: more than resume_max_bullets selected -> trimmed to the
   max_bullets highest relevance_score, lowest-relevance dropped.
3. Exact count selected -> left alone.
4. Project safety net: LLM returns 0, 1, or a fuzzy/paraphrased subset of
   project names -> selected_content["projects"] always ends up as ALL
   project names from master data, regardless.

Run: pytest tests/test_strategist_matches_base_resume.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import graph
from graph import strategist_node, SelectedBullet, StrategistSelection


def _master_data():
    return {
        "experience": [
            {
                "company": "Acme", "title": "Engineer",
                "resume_min_bullets": 2, "resume_max_bullets": 3,
                "bullets": [
                    {"text": "Bullet A (quantified)", "is_quantified": True, "tags": []},
                    {"text": "Bullet B (quantified)", "is_quantified": True, "tags": []},
                    {"text": "Bullet C (quantified)", "is_quantified": True, "tags": []},
                    {"text": "Bullet D (not quantified)", "is_quantified": False, "tags": []},
                ],
            },
        ],
        "projects": [
            {"name": "Project One", "domain": "Web", "tech_stack": ["Python"]},
            {"name": "Project Two", "domain": "AI", "tech_stack": ["LangGraph"]},
            {"name": "Project Three", "domain": "Security", "tech_stack": ["Circom"]},
            {"name": "Project Four", "domain": "Data", "tech_stack": ["Flask"]},
        ],
    }


def _run_strategist(fake_selection: StrategistSelection, master_data=None):
    state = {"job_description": "Some JD text", "master_data": master_data or _master_data()}
    with patch.object(graph, "invoke_with_retry", return_value=fake_selection):
        return strategist_node(state)


def test_undershooting_the_llm_tops_up_to_exact_max():
    fake = StrategistSelection(
        selected_bullets=[SelectedBullet(company_name="Acme", original_text="Bullet A (quantified)",
                                          relevance_score=5, selection_reason="r")],
        selected_project_names=["Project One", "Project Two", "Project Three", "Project Four"],
    )
    result = _run_strategist(fake)
    acme_bullets = [b["original_text"] for b in result["selected_content"]["experience"]]
    assert len(acme_bullets) == 3  # resume_max_bullets, not the 1 the LLM picked
    assert "Bullet A (quantified)" in acme_bullets  # the LLM's real pick is kept


def test_overshooting_the_llm_trims_to_exact_max_keeping_highest_relevance():
    fake = StrategistSelection(
        selected_bullets=[
            SelectedBullet(company_name="Acme", original_text="Bullet A (quantified)", relevance_score=2, selection_reason="r"),
            SelectedBullet(company_name="Acme", original_text="Bullet B (quantified)", relevance_score=5, selection_reason="r"),
            SelectedBullet(company_name="Acme", original_text="Bullet C (quantified)", relevance_score=4, selection_reason="r"),
            SelectedBullet(company_name="Acme", original_text="Bullet D (not quantified)", relevance_score=3, selection_reason="r"),
        ],
        selected_project_names=["Project One", "Project Two", "Project Three", "Project Four"],
    )
    result = _run_strategist(fake)
    acme_bullets = {b["original_text"] for b in result["selected_content"]["experience"]}
    assert len(acme_bullets) == 3
    assert acme_bullets == {"Bullet B (quantified)", "Bullet C (quantified)", "Bullet D (not quantified)"}
    assert "Bullet A (quantified)" not in acme_bullets  # lowest relevance_score (2), correctly dropped


def test_exact_count_is_left_alone():
    fake = StrategistSelection(
        selected_bullets=[
            SelectedBullet(company_name="Acme", original_text="Bullet A (quantified)", relevance_score=5, selection_reason="r"),
            SelectedBullet(company_name="Acme", original_text="Bullet C (quantified)", relevance_score=4, selection_reason="r"),
            SelectedBullet(company_name="Acme", original_text="Bullet D (not quantified)", relevance_score=3, selection_reason="r"),
        ],
        selected_project_names=["Project One", "Project Two", "Project Three", "Project Four"],
    )
    result = _run_strategist(fake)
    acme_bullets = {b["original_text"] for b in result["selected_content"]["experience"]}
    assert acme_bullets == {"Bullet A (quantified)", "Bullet C (quantified)", "Bullet D (not quantified)"}


def test_llm_returning_zero_projects_still_yields_all_four():
    fake = StrategistSelection(
        selected_bullets=[SelectedBullet(company_name="Acme", original_text="Bullet A (quantified)",
                                          relevance_score=5, selection_reason="r")],
        selected_project_names=[],
    )
    result = _run_strategist(fake)
    assert set(result["selected_content"]["projects"]) == {"Project One", "Project Two", "Project Three", "Project Four"}


def test_include_in_generic_false_employer_is_excluded_entirely():
    """
    Real bug, found on a live run (Sep 2026): the Virginia Tech Grader role
    (include_in_generic: false — excluded from the base resume as a thin,
    low-signal role) surfaced on a tailored resume anyway, because
    strategist_node never checked this flag, only generate_generic.py did.
    """
    master_data = _master_data()
    master_data["experience"].append({
        "company": "VT Grader", "title": "Grader", "include_in_generic": False,
        "resume_min_bullets": 0, "resume_max_bullets": 1,
        "bullets": [{"text": "Graded assignments.", "is_quantified": False, "tags": []}],
    })
    fake = StrategistSelection(
        selected_bullets=[SelectedBullet(company_name="Acme", original_text="Bullet A (quantified)",
                                          relevance_score=5, selection_reason="r")],
        selected_project_names=["Project One", "Project Two", "Project Three", "Project Four"],
    )
    result = _run_strategist(fake, master_data=master_data)
    companies = {b["company_name"] for b in result["selected_content"]["experience"]}
    assert "VT Grader" not in companies
    assert "Acme" in companies


def test_llm_returning_a_fuzzy_subset_still_yields_all_four_in_llm_order_first():
    fake = StrategistSelection(
        selected_bullets=[SelectedBullet(company_name="Acme", original_text="Bullet A (quantified)",
                                          relevance_score=5, selection_reason="r")],
        # A realistic LLM paraphrase (extra trailing description) — real_name.lower()
        # is a substring of returned_name.lower(), so this must fuzzy-match "Project Two".
        selected_project_names=["Project Two - AI Platform"],
    )
    result = _run_strategist(fake)
    projects = result["selected_content"]["projects"]
    assert set(projects) == {"Project One", "Project Two", "Project Three", "Project Four"}
    assert projects[0] == "Project Two"  # the LLM's real (fuzzy-matched) choice stays first
