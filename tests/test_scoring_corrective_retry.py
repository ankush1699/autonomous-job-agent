"""
tests/test_scoring_corrective_retry.py

Regression tests for the live scoring failure of Sep 18 2026 (Google
"Software Engineer III, Full Stack", entry c6f8b8d6f4ab):

  Haiku labeled the JD seniority_level="mid" but scored seniority 5 — its
  reasoning discounted the CANDIDATE ("zero US work experience") instead of
  rating the JOB's level. _SubScoreInconsistent fired (correctly), the loop
  hopped to Groq, Groq's model emitted truncated tool-call JSON
  (tool_use_failed), Ollama wasn't running, and with no fallback left the
  old loop fell through to plain retries of Groq — the provider it had just
  declared a dead end — three times. The job ended up unscored.

New behaviour under test:
  1. An inconsistent answer gets ONE corrective re-ask on the SAME provider
     (contradiction fed back as an extra human turn; system prefix untouched
     so the prompt cache still hits) before any hop.
  2. A hop-worthy failure with nothing left in the chain raises immediately,
     naming each provider's failure — no dead-end retries, no sleeping.

Run: pytest tests/test_scoring_corrective_retry.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from langchain_core.messages import HumanMessage, SystemMessage

from core.should_apply import _build_messages, _llm_score, _CORRECTION_TEMPLATE


def _scores(tech, sen, role, spons, *, seniority_level="mid", role_type="fullstack-swe"):
    m = MagicMock()
    m.tech_score, m.seniority_score, m.role_type_score, m.sponsorship_bonus_score = tech, sen, role, spons
    m.seniority_level, m.role_type = seniority_level, role_type
    m.reasoning, m.jd_summary = "r", "s"
    m.tailoring_recommended, m.tailoring_reasoning = False, "t"
    return m


# The exact live shape: label "mid" (rubric: 20) paired with the senior-tier 5.
_LIVE_INCONSISTENT = _scores(42, 5, 15, 7, seniority_level="mid")
_LIVE_CORRECTED = _scores(42, 20, 15, 7, seniority_level="mid")

_GROQ_TOOL_USE_FAILED = Exception(
    "Error code: 400 - {'error': {'message': 'Failed to parse tool call arguments as JSON', "
    "'type': 'invalid_request_error', 'code': 'tool_use_failed', 'failed_generation': '{...'}}"
)


def test_inconsistent_answer_is_corrected_on_the_same_provider_before_any_hop():
    haiku = MagicMock()
    invoke = haiku.with_structured_output.return_value.invoke
    invoke.side_effect = [_LIVE_INCONSISTENT, _LIVE_CORRECTED]

    with patch("core.should_apply.cheap_provider", return_value="anthropic"), \
         patch("core.should_apply.next_cheap_provider") as mock_next, \
         patch("core.should_apply.get_chat_model", return_value=haiku), \
         patch("core.should_apply.time.sleep") as mock_sleep:
        result = _llm_score("some JD text")

    assert result["sub_scores"]["seniority"] == 20
    assert result["score"] == 42 + 20 + 15 + 7
    assert invoke.call_count == 2
    mock_next.assert_not_called()   # never left Haiku
    mock_sleep.assert_not_called()  # the re-ask is immediate

    # The second call carried the contradiction back to the model, after an
    # unchanged system + JD prefix (cache-friendly).
    second_messages = invoke.call_args_list[1].args[0]
    first_messages = invoke.call_args_list[0].args[0]
    assert second_messages[:2] == first_messages[:2]
    assert len(second_messages) == 3
    assert "seniority_level='mid'" in second_messages[2].content
    assert "got 5" in second_messages[2].content


def test_second_inconsistent_answer_hops_and_a_dead_chain_raises_immediately():
    """The full live sequence with the fix: Haiku wrong twice -> hop to Groq
    -> tool_use_failed -> nothing left -> raise at once, Groq called exactly
    once, no backoff sleeps, both failures named in the error."""
    haiku = MagicMock()
    haiku.with_structured_output.return_value.invoke.side_effect = [_LIVE_INCONSISTENT, _LIVE_INCONSISTENT]
    groq = MagicMock()
    groq.with_structured_output.return_value.invoke.side_effect = _GROQ_TOOL_USE_FAILED

    with patch("core.should_apply.cheap_provider", return_value="anthropic"), \
         patch("core.should_apply.next_cheap_provider", side_effect=lambda p: {"anthropic": "groq"}.get(p)), \
         patch("core.should_apply.get_chat_model", side_effect=[haiku, groq]), \
         patch("core.should_apply.time.sleep") as mock_sleep:
        try:
            _llm_score("some JD text", max_retries=3)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            msg = str(e)

    assert groq.with_structured_output.return_value.invoke.call_count == 1
    assert haiku.with_structured_output.return_value.invoke.call_count == 2
    mock_sleep.assert_not_called()
    assert "anthropic: inconsistent scores" in msg
    assert "groq: quota exhausted / unusable tool calls" in msg
    assert "tool_use_failed" in msg


def test_transient_errors_still_use_the_normal_retry_budget():
    """A plain transient failure is not hop-worthy and not a score problem:
    it must still back off and retry the same provider as before."""
    haiku = MagicMock()
    haiku.with_structured_output.return_value.invoke.side_effect = [
        Exception("Connection reset by peer"), _LIVE_CORRECTED,
    ]
    with patch("core.should_apply.cheap_provider", return_value="anthropic"), \
         patch("core.should_apply.next_cheap_provider", return_value=None), \
         patch("core.should_apply.get_chat_model", return_value=haiku), \
         patch("core.should_apply.time.sleep") as mock_sleep:
        result = _llm_score("some JD text", max_retries=3)
    assert result["score"] == 42 + 20 + 15 + 7
    mock_sleep.assert_called_once()


def test_build_messages_correction_turn_keeps_cached_prefix_intact():
    base = _build_messages("anthropic", "STATIC", "JOB DESCRIPTION:\nx")
    corrected = _build_messages("anthropic", "STATIC", "JOB DESCRIPTION:\nx",
                                correction="seniority_level='mid' allows scores [25, 20] per the rubric, but got 5")
    assert corrected[:2] == base[:2]
    assert isinstance(corrected[0], SystemMessage)
    assert corrected[0].content[0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(corrected[2], HumanMessage)
    assert corrected[2].content == _CORRECTION_TEMPLATE.format(
        problem="seniority_level='mid' allows scores [25, 20] per the rubric, but got 5")
    assert "describe the JOB, not the candidate's fit" in corrected[2].content
