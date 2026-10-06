"""
tests/test_sponsorship_signal.py

Regression test for a real gap: scraper/sponsorship.py's SerpApi H-1B lookup
was computed and written to the sheet, but never actually influenced
core.should_apply.evaluate()'s score or proceed decision — a company
confirmed via external lookup to not sponsor could still pass the gate just
because its JD text didn't happen to contain red-flag language, and a
company with a confirmed H-1B track record got no credit for it.

evaluate()'s new sponsorship_signal param is a deterministic Python-side
adjustment: "no_sponsorship" is a hard override (score 0, same shape as a
red flag), "confirmed_h1b"/"likely" lift sponsorship_bonus (+6 / +3 under
rubric v2, where a JD silent on sponsorship scores 7 and an explicit
sponsor scores 15), capped at 15. No LLM calls — mocks
core.should_apply._llm_score directly.

Run: pytest tests/test_sponsorship_signal.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import should_apply

# A clean JD with no red-flag language, so the flow reaches the LLM stage.
_CLEAN_JD = "We are hiring a backend software engineer. " + "Great team culture. " * 20


def _fake_llm_score(jd, max_retries=3):
    # A coherent rubric-v2 verdict: sub_scores sum to score, labels match
    # their canonical values, and sponsorship_bonus=7 = "JD silent".
    return {
        "rubric_version": should_apply.RUBRIC_VERSION,
        "score": 72,
        "reasoning": "Solid tech match.",
        "jd_summary": "A backend role.",
        "seniority_level": "mid",
        "role_type": "backend-swe",
        "sub_scores": {"tech": 30, "seniority": 20, "role_type": 15, "sponsorship_bonus": 7},
    }


def test_no_sponsorship_signal_overrides_to_zero_without_calling_llm():
    with patch("core.should_apply._llm_score") as mock_llm:
        verdict = should_apply.evaluate(
            _CLEAN_JD, company="NoSponsorCo", use_cache=False,
            sponsorship_signal="no_sponsorship",
        )
    mock_llm.assert_not_called()
    assert verdict["score"] == 0
    assert verdict["proceed"] is False
    assert verdict["stage"] == "red_flag"
    assert "NoSponsorCo" in verdict["red_flags"][0]


def test_confirmed_h1b_signal_boosts_sponsorship_bonus_and_total_score():
    with patch("core.should_apply._llm_score", side_effect=_fake_llm_score):
        verdict = should_apply.evaluate(
            _CLEAN_JD, company="SponsorsCo", use_cache=False,
            sponsorship_signal="confirmed_h1b",
        )
    assert verdict["sub_scores"]["sponsorship_bonus"] == 13  # 7 (silent) + 6
    assert verdict["score"] == 78  # 72 + 6
    assert "confirmed_h1b" in verdict["reasoning"]


def test_likely_signal_gives_smaller_boost():
    with patch("core.should_apply._llm_score", side_effect=_fake_llm_score):
        verdict = should_apply.evaluate(
            _CLEAN_JD, company="MaybeCo", use_cache=False,
            sponsorship_signal="likely",
        )
    assert verdict["sub_scores"]["sponsorship_bonus"] == 10  # 7 (silent) + 3
    assert verdict["score"] == 75


def test_neutral_or_missing_signal_is_a_no_op():
    with patch("core.should_apply._llm_score", side_effect=_fake_llm_score):
        neutral = should_apply.evaluate(_CLEAN_JD, use_cache=False, sponsorship_signal="neutral")
        omitted = should_apply.evaluate(_CLEAN_JD, use_cache=False)
    assert neutral["score"] == 72
    assert omitted["score"] == 72
    assert neutral["sub_scores"]["sponsorship_bonus"] == 7
    assert omitted["sub_scores"]["sponsorship_bonus"] == 7


def test_bonus_is_capped_at_fifteen():
    def _high_sponsorship(jd, max_retries=3):
        r = _fake_llm_score(jd, max_retries)
        r["sub_scores"]["sponsorship_bonus"] = 14
        return r

    with patch("core.should_apply._llm_score", side_effect=_high_sponsorship):
        verdict = should_apply.evaluate(
            _CLEAN_JD, use_cache=False, sponsorship_signal="confirmed_h1b",
        )
    assert verdict["sub_scores"]["sponsorship_bonus"] == 15  # capped, not 20
    assert verdict["score"] == 73  # only +1 actually applied (72 + 1)
