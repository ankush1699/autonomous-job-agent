"""
tests/test_is_quota_exhausted.py

Regression test for a live failure: Groq deprecated llama-3.3-70b-versatile
and started returning "Error code: 404 - ... model_not_found ... does not
exist or you do not have access to it" instead of the model responding.
is_quota_exhausted() previously only recognized rate-limit/quota text, so
this 404 fell through to should_apply's plain retry-the-same-provider path
and failed outright after 3 attempts instead of hopping to Ollama/Haiku.

Run: pytest tests/test_is_quota_exhausted.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.llm import is_quota_exhausted


def test_groq_model_not_found_404_triggers_fallback():
    exc = Exception(
        "Error code: 404 - {'error': {'message': 'The model `llama-3.3-70b-versatile` "
        "does not exist or you do not have access to it.', 'type': 'invalid_request_error', "
        "'code': 'model_not_found'}}"
    )
    assert is_quota_exhausted(exc) is True


def test_existing_tpd_quota_message_still_triggers_fallback():
    exc = Exception(
        "Error code: 429 - rate_limit_exceeded: tokens per day (TPD): "
        "Limit 100000, Used 99898. Please try again in 26m59s."
    )
    assert is_quota_exhausted(exc) is True


def test_unrelated_error_does_not_trigger_fallback():
    assert is_quota_exhausted(Exception("Connection timed out")) is False


def test_groq_tool_use_failed_schema_mismatch_triggers_fallback():
    exc = Exception(
        "Error code: 400 - {'error': {'message': 'Tool call validation failed: "
        "tool call validation failed: parameters for tool _SubScores did not match "
        "schema: errors: [`/tech_score`: expected integer, but got number]', "
        "'type': 'invalid_request_error', 'code': 'tool_use_failed'}}"
    )
    assert is_quota_exhausted(exc) is True


def test_groq_tool_choice_not_called_triggers_fallback():
    exc = Exception(
        "Error code: 400 - {'error': {'message': 'Tool choice is required, but "
        "model did not call a tool', 'type': 'invalid_request_error', "
        "'code': 'tool_use_failed', 'failed_generation': ''}}"
    )
    assert is_quota_exhausted(exc) is True
