"""
tests/test_invoke_with_retry_fallback.py

Regression test for the live failure the user hit: Groq's daily token quota
(100K TPD) exhausted mid-run, surfaced as a 429 with "tokens per day" in the
message. invoke_with_retry's old behavior was to back off and retry the SAME
provider — pointless against a quota that resets in tens of minutes. It should
instead rebuild the structured LLM on the next cheap-tier provider (via
cheap_rebuild) and continue immediately, no wait.

Mocks core.llm's provider-resolution functions so this runs with no network
calls and no real API keys.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from pydantic import BaseModel, ValidationError
from graph import invoke_with_retry


class _DummySchema:
    pass


class _RealSchema(BaseModel):
    required_field: str


def test_quota_exhaustion_falls_back_without_waiting():
    quota_error = Exception(
        "Error code: 429 - rate_limit_exceeded: tokens per day (TPD): "
        "Limit 100000, Used 99898. Please try again in 26m59s."
    )

    exhausted_llm = MagicMock()
    exhausted_llm.invoke.side_effect = quota_error

    fallback_llm = MagicMock()
    fallback_llm.invoke.return_value = "success"

    rebuilt_base = MagicMock()
    rebuilt_base.with_structured_output.return_value = fallback_llm

    with patch("core.llm.cheap_provider", return_value="groq"), \
         patch("core.llm.next_cheap_provider", return_value="ollama") as mock_next, \
         patch("core.llm.get_chat_model", return_value=rebuilt_base) as mock_get_model, \
         patch("graph.time.sleep") as mock_sleep:
        result = invoke_with_retry(
            exhausted_llm, "prompt",
            cheap_rebuild={"temperature": 0.2, "max_tokens": 1024, "schema": _DummySchema},
        )

    assert result == "success"
    mock_next.assert_called_once_with("groq")
    mock_get_model.assert_called_once_with(
        "cheap", temperature=0.2, max_tokens=1024, provider_override="ollama"
    )
    mock_sleep.assert_not_called()  # no exponential backoff — hopped providers instead


def test_quota_exhaustion_with_no_further_provider_falls_through_to_normal_429_handling():
    # Real shape from the live 429: "Error code: 429 - ... tokens per day (TPD)..."
    quota_error = Exception("Error code: 429 - tokens per day (TPD) exceeded")
    always_fails = MagicMock()
    always_fails.invoke.side_effect = quota_error

    with patch("core.llm.cheap_provider", return_value="anthropic"), \
         patch("core.llm.next_cheap_provider", return_value=None), \
         patch("graph.time.sleep"):
        try:
            invoke_with_retry(
                always_fails, "prompt", max_retries=1,
                cheap_rebuild={"temperature": 0.0, "max_tokens": 1024, "schema": _DummySchema},
            )
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "failed after 1 retries" in str(e)


def test_validation_error_from_missing_field_retries_instead_of_failing_immediately():
    """
    Regression test for a real failure: a writer call returned tool-call JSON
    missing the required tailored_bullets field entirely (LangChain's own
    source says the #1 cause is the response getting cut off by max_tokens
    mid-generation). This raises a raw pydantic.ValidationError, NOT
    OutputParserException — before this fix, invoke_with_retry's except
    clause didn't catch it at all, so it propagated on the very first
    attempt with zero retries.
    """
    def _missing_field_error():
        try:
            _RealSchema()  # missing required_field -> raises ValidationError
        except ValidationError as e:
            return e

    truncated_then_ok = MagicMock()
    truncated_then_ok.invoke.side_effect = [_missing_field_error(), "ok"]

    with patch("graph.time.sleep") as mock_sleep:
        result = invoke_with_retry(truncated_then_ok, "prompt", max_retries=2)

    assert result == "ok"
    assert truncated_then_ok.invoke.call_count == 2
    mock_sleep.assert_called_once()


def test_validation_error_still_raises_after_exhausting_retries():
    def _missing_field_error():
        try:
            _RealSchema()
        except ValidationError as e:
            return e

    always_truncated = MagicMock()
    always_truncated.invoke.side_effect = lambda *a, **k: (_ for _ in ()).throw(_missing_field_error())

    with patch("graph.time.sleep"):
        try:
            invoke_with_retry(always_truncated, "prompt", max_retries=2)
            assert False, "expected ValidationError to propagate"
        except ValidationError:
            pass
    assert always_truncated.invoke.call_count == 2


def test_no_cheap_rebuild_leaves_normal_429_backoff_behavior_unchanged():
    """Writer's main draft call (Anthropic/quality-tier) never passes
    cheap_rebuild — a 429 there should still hit the old backoff-and-retry
    path, not attempt any provider fallback."""
    rate_limited_then_ok = MagicMock()
    rate_limited_then_ok.invoke.side_effect = [Exception("429 rate limited"), "ok"]

    with patch("graph.time.sleep") as mock_sleep:
        result = invoke_with_retry(rate_limited_then_ok, "prompt", max_retries=2)

    assert result == "ok"
    mock_sleep.assert_called_once()
