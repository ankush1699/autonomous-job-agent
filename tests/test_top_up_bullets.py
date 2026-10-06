"""
tests/test_top_up_bullets.py

Regression test for a real, two-layer bug: both the writer AND the editor
LLM calls were independently observed dropping a bullet during rewriting,
even when the strategist had correctly selected enough bullets per an
employer's own stated minimum. E.g. a live run: strategist selected 2
Handshake AI bullets (meeting its min=2 constraint), the writer only
produced 1 rewritten bullet for that employer, and separately, on another
run, the editor's own polish pass dropped a bullet the writer HAD produced.

top_up_bullets_by_company() is the single shared fix for both layers —
writer_node calls it against the strategist's selection (fallback: original
bullet text), editor_node calls it against the writer's draft (fallback:
writer's already-tailored text). Tested directly here since it's pure Python
with no LLM calls — fast, free, and covers exactly the failure shape seen
in production without needing to reproduce a live API rate-limit window.

Run: pytest tests/test_top_up_bullets.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from graph import top_up_bullets_by_company


def test_no_shortfall_returns_unchanged_list():
    rewritten = [{"company_name": "TCS", "rewritten_text": "Did X."}]
    target = [{"company_name": "TCS", "text": "Did X (original)."}]
    result = top_up_bullets_by_company(rewritten, target, fallback_key="text")
    assert result == rewritten


def test_reproduces_real_failure_writer_dropped_one_handshake_bullet():
    """
    Exact real shape: strategist selected 2 Handshake AI bullets, writer
    only rewrote 1. Must top up to 2, using the ORIGINAL text for the
    missing one (writer_node's fallback_key="text").
    """
    rewritten = [
        {"company_name": "Handshake AI", "rewritten_text": "Rewrote bullet one."},
    ]
    selected = [
        {"company_name": "Handshake AI", "text": "Original bullet one."},
        {"company_name": "Handshake AI", "text": "Original bullet two."},
    ]
    result = top_up_bullets_by_company(rewritten, selected, fallback_key="text")
    assert len(result) == 2
    assert result[0]["rewritten_text"] == "Rewrote bullet one."
    assert result[1] == {"company_name": "Handshake AI", "rewritten_text": "Original bullet two."}


def test_editor_layer_falls_back_to_writer_draft_not_raw_original():
    """
    editor_node's call uses fallback_key="rewritten_text" against the
    writer's draft — falling back to the writer's already-tailored prose,
    not raw master-data text, since that's higher quality than what
    writer_node's own fallback would have used.
    """
    editor_output = [{"company_name": "TCS", "rewritten_text": "Editor kept this one."}]
    writer_draft = [
        {"company_name": "TCS", "rewritten_text": "Editor kept this one."},
        {"company_name": "TCS", "rewritten_text": "Writer's version of the dropped one."},
    ]
    result = top_up_bullets_by_company(editor_output, writer_draft, fallback_key="rewritten_text")
    assert len(result) == 2
    assert result[1]["rewritten_text"] == "Writer's version of the dropped one."


def test_multiple_companies_each_topped_up_independently():
    rewritten = [
        {"company_name": "A", "rewritten_text": "a1"},
        {"company_name": "B", "rewritten_text": "b1"},
    ]
    target = [
        {"company_name": "A", "text": "a1-orig"},
        {"company_name": "A", "text": "a2-orig"},
        {"company_name": "B", "text": "b1-orig"},
        {"company_name": "B", "text": "b2-orig"},
        {"company_name": "B", "text": "b3-orig"},
    ]
    result = top_up_bullets_by_company(rewritten, target, fallback_key="text")
    a_count = sum(1 for b in result if b["company_name"] == "A")
    b_count = sum(1 for b in result if b["company_name"] == "B")
    assert a_count == 2
    assert b_count == 3


def test_does_not_mutate_input_list():
    rewritten = [{"company_name": "TCS", "rewritten_text": "one"}]
    target = [
        {"company_name": "TCS", "text": "one"},
        {"company_name": "TCS", "text": "two"},
    ]
    original_len = len(rewritten)
    top_up_bullets_by_company(rewritten, target, fallback_key="text")
    assert len(rewritten) == original_len  # caller's list untouched


def test_company_with_zero_rewritten_bullets_gets_all_added():
    """The extreme case: an employer entirely missing from the rewrite, not
    just short by one — e.g. writer skipped it altogether."""
    rewritten = []
    target = [
        {"company_name": "SWLI", "text": "one"},
        {"company_name": "SWLI", "text": "two"},
    ]
    result = top_up_bullets_by_company(rewritten, target, fallback_key="text")
    assert len(result) == 2
    assert all(b["company_name"] == "SWLI" for b in result)


def test_empty_target_selection_is_a_noop():
    rewritten = [{"company_name": "TCS", "rewritten_text": "one"}]
    assert top_up_bullets_by_company(rewritten, [], fallback_key="text") == rewritten
