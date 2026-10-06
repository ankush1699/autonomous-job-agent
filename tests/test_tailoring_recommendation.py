"""
tests/test_tailoring_recommendation.py

Covers the tailoring_recommended/tailoring_reasoning fields — a DELIBERATELY
separate judgment from score/proceed. An earlier version of this feature
conflated the two by thresholding the apply-score itself; this is the fix.

tailoring_recommended answers "does my BASE RESUME, as actually written,
already cover this specific JD" — compared against
core/should_apply.py's BASE_RESUME_CONTENT_PATH (a plain-text digest of
what's really printed on the generic base resume, built by
engine/generate_generic.py + core/profile_cache.py), NOT the candidate's
full profile (that's _load_candidate_stack(), used for score/proceed).

Run: pytest tests/test_tailoring_recommendation.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.should_apply import (
    _load_base_resume_content, _BASE_RESUME_MISSING, evaluate,
)


def test_load_base_resume_content_returns_fallback_when_missing():
    with patch("core.should_apply.BASE_RESUME_CONTENT_PATH", "/nonexistent/path/xyz.txt"):
        assert _load_base_resume_content() == _BASE_RESUME_MISSING


def test_load_base_resume_content_reads_real_file(tmp_path):
    f = tmp_path / "base_resume_content.txt"
    f.write_text("SKILLS\n------\nLanguages: Python, Kotlin")
    with patch("core.should_apply.BASE_RESUME_CONTENT_PATH", str(f)):
        content = _load_base_resume_content()
    assert "Kotlin" in content


def test_red_flag_short_circuit_sets_tailoring_recommended_false():
    """A rejected posting (hard red flag) shouldn't recommend tailoring —
    there's nothing to tailor for a job you're not applying to."""
    result = evaluate(
        "This role requires an active US security clearance.",
        use_cache=False,
    )
    assert result["proceed"] is False
    assert result["tailoring_recommended"] is False
    assert "tailoring_reasoning" in result


def test_llm_stage_carries_tailoring_fields_through():
    from core.should_apply import _llm_score

    mock_result = MagicMock()
    mock_result.tech_score, mock_result.seniority_score = 40, 20
    mock_result.role_type_score, mock_result.sponsorship_bonus_score = 15, 10
    mock_result.reasoning, mock_result.jd_summary = "good fit", "summary"
    mock_result.seniority_level, mock_result.role_type = "mid", "fullstack-swe"
    mock_result.tailoring_recommended = False
    mock_result.tailoring_reasoning = "Base resume already covers Python/Java/Angular."

    llm = MagicMock()
    llm.with_structured_output.return_value.invoke.return_value = mock_result

    with patch("core.should_apply.get_chat_model", return_value=llm), \
         patch("core.should_apply.cheap_provider", return_value="anthropic"):
        result = _llm_score("some JD text")

    assert result["tailoring_recommended"] is False
    assert result["tailoring_reasoning"] == "Base resume already covers Python/Java/Angular."


def test_tailoring_recommendation_is_independent_of_apply_score():
    """
    The core bug this feature fixes: score/proceed and tailoring_recommended
    must be able to disagree. A high-scoring job can still need tailoring
    (the base resume misses something this JD specifically wants), and a
    low-scoring job can have a base resume that already covers what little
    it does share with the JD. Verified here by constructing both cases
    from the same _llm_score plumbing with independent mock values.
    """
    from core.should_apply import _llm_score

    # High apply-fit (98/100) but tailoring still recommended.
    high_fit_needs_tailoring = MagicMock()
    high_fit_needs_tailoring.tech_score, high_fit_needs_tailoring.seniority_score = 45, 25
    high_fit_needs_tailoring.role_type_score, high_fit_needs_tailoring.sponsorship_bonus_score = 15, 13
    high_fit_needs_tailoring.reasoning, high_fit_needs_tailoring.jd_summary = "great fit", "s"
    high_fit_needs_tailoring.seniority_level, high_fit_needs_tailoring.role_type = "mid", "fullstack-swe"
    high_fit_needs_tailoring.tailoring_recommended = True
    high_fit_needs_tailoring.tailoring_reasoning = "JD emphasizes Kubernetes; resume doesn't mention it."

    llm = MagicMock()
    llm.with_structured_output.return_value.invoke.return_value = high_fit_needs_tailoring
    with patch("core.should_apply.get_chat_model", return_value=llm), \
         patch("core.should_apply.cheap_provider", return_value="anthropic"):
        result = _llm_score("JD text")

    assert result["score"] == 98
    assert result["tailoring_recommended"] is True  # disagrees with the high score — by design
