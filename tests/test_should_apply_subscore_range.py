"""
tests/test_should_apply_subscore_range.py

Regression test for a live scoring bug: Groq's openai/gpt-oss-120b (the
model core/llm.py swapped to after Groq deprecated llama-3.3-70b-versatile)
doesn't reliably follow the 0-45/0-25/0-15/0-15 sub-score ranges given only
in field descriptions — it emits values on a different scale entirely.
_llm_score() used to silently clamp those with min(cap, value), so every
dimension hit its ceiling at once and produced a false "100/100 perfect
match" for JDs the model's own reasoning text called a weak fit. Confirmed
live against a real stored JD (Microsoft Azure PostgreSQL role): raw model
output like tech=60, seniority=90, role=70, sponsorship=100 clamped to
45+25+15+15=100 every time.

_coerce_subscore() now raises instead of clamping, and _llm_score() treats
that the same as quota exhaustion — hop to the next provider rather than
retry the same unreliable model.

Run: pytest tests/test_should_apply_subscore_range.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.should_apply import _coerce_subscore, _SubScoreOutOfRange, _llm_score


def test_in_range_value_passes_through():
    assert _coerce_subscore(30, 45, "tech_score") == 30
    assert _coerce_subscore("30", 45, "tech_score") == 30
    assert _coerce_subscore(0, 45, "tech_score") == 0
    assert _coerce_subscore(45, 45, "tech_score") == 45


def test_out_of_range_value_raises_instead_of_clamping():
    try:
        _coerce_subscore(90, 45, "tech_score")
        assert False, "expected _SubScoreOutOfRange"
    except _SubScoreOutOfRange as e:
        assert "tech_score" in str(e)
        assert "90" in str(e)


def test_negative_value_raises():
    try:
        _coerce_subscore(-1, 45, "tech_score")
        assert False, "expected _SubScoreOutOfRange"
    except _SubScoreOutOfRange:
        pass


def _mock_subscores(tech, sen, role, spons, tailoring_recommended=True, tailoring_reasoning="tr"):
    m = MagicMock()
    m.tech_score, m.seniority_score, m.role_type_score, m.sponsorship_bonus_score = tech, sen, role, spons
    m.reasoning, m.jd_summary = "r", "s"
    m.seniority_level, m.role_type = "mid", "fullstack-swe"
    m.tailoring_recommended, m.tailoring_reasoning = tailoring_recommended, tailoring_reasoning
    return m


def test_out_of_range_scores_from_one_provider_fall_back_to_next_instead_of_a_false_100():
    """
    Reproduces the exact live failure: groq's model returns wildly
    out-of-range sub-scores (would have clamped to a false 100). Instead of
    accepting that, _llm_score should hop to the next provider and return
    its (correct) result.
    """
    bad_llm = MagicMock()
    bad_llm.with_structured_output.return_value.invoke.return_value = _mock_subscores(60, 90, 70, 100)

    good_llm = MagicMock()
    good_llm.with_structured_output.return_value.invoke.return_value = _mock_subscores(20, 20, 13, 4)

    with patch("core.should_apply.cheap_provider", return_value="groq"), \
         patch("core.should_apply.next_cheap_provider", return_value="anthropic") as mock_next, \
         patch("core.should_apply.get_chat_model", side_effect=[bad_llm, good_llm]):
        result = _llm_score("some JD text")

    mock_next.assert_called_once_with("groq")
    assert result["score"] == 20 + 20 + 13 + 4
    assert result["sub_scores"] == {"tech": 20, "seniority": 20, "role_type": 13, "sponsorship_bonus": 4}


def test_out_of_range_with_no_further_provider_eventually_raises():
    bad_llm = MagicMock()
    bad_llm.with_structured_output.return_value.invoke.return_value = _mock_subscores(60, 90, 70, 100)

    with patch("core.should_apply.cheap_provider", return_value="anthropic"), \
         patch("core.should_apply.next_cheap_provider", return_value=None), \
         patch("core.should_apply.get_chat_model", return_value=bad_llm), \
         patch("core.should_apply.time.sleep"):
        try:
            _llm_score("some JD text", max_retries=1)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            # Out-of-range on the only provider: one corrective re-ask, still
            # bad, no fallback in the chain -> raise (no pointless retries).
            assert "should_apply LLM scoring failed" in str(e)
            assert "anthropic" in str(e)
    assert bad_llm.with_structured_output.return_value.invoke.call_count == 2


def test_hop_on_the_final_attempt_still_actually_tries_the_fallback():
    """
    Regression test for a live failure: attempts 1-2 failed on a plain
    transient error (not hop-worthy — consumes real attempt budget via
    normal backoff-retry), attempt 3 hit Groq's real TPD quota limit. That
    IS hop-worthy, but with the old `for attempt in range(max_retries)` loop
    there was no iteration left to actually USE the fallback — it just fell
    through and raised the stale Groq error without ever calling Anthropic.
    A hop must always get a real attempt on the new provider, regardless of
    which retry it lands on.
    """
    quota_error = Exception("Error code: 429 - rate_limit_exceeded: tokens per day (TPD) exceeded")
    transient_error = Exception("Connection reset by peer")

    groq_llm = MagicMock()
    groq_llm.with_structured_output.return_value.invoke.side_effect = [transient_error, transient_error, quota_error]

    anthropic_llm = MagicMock()
    anthropic_llm.with_structured_output.return_value.invoke.return_value = _mock_subscores(20, 20, 13, 4)

    with patch("core.should_apply.cheap_provider", return_value="groq"), \
         patch("core.should_apply.next_cheap_provider", return_value="anthropic"), \
         patch("core.should_apply.get_chat_model", side_effect=[groq_llm, anthropic_llm]), \
         patch("core.should_apply.time.sleep"):
        result = _llm_score("some JD text", max_retries=3)

    assert result["score"] == 20 + 20 + 13 + 4
