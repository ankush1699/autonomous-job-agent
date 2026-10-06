"""
tests/test_pdf_generator_sanitize.py

engine/pdf_generator.sanitize_output_text is the single enforcement point for
"no em dash, no stray [METRIC NEEDED] tags" across every PDF this pipeline
generates. Regression test for the real bug where an em dash sat in hand-
written master data (not just LLM output) and reached a compiled PDF.

Run: pytest tests/test_pdf_generator_sanitize.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from pdf_generator import sanitize_output_text


def test_em_dash_replaced_with_comma():
    assert "—" not in sanitize_output_text("Shipped a feature — on time and on budget.")


def test_metric_needed_tag_stripped():
    result = sanitize_output_text("Improved latency by an unknown amount. [METRIC NEEDED]")
    assert "METRIC NEEDED" not in result


def test_mid_sentence_pipe_removed():
    result = sanitize_output_text("F-1 OPT Eligible | Available July 2026")
    assert " | " not in result


def test_recurses_through_nested_dict_and_list():
    data = {
        "summary": "Built systems — at scale.",
        "achievements": ["Won an award — twice. [METRIC NEEDED]"],
        "experience": [{"bullets": [{"text": "Shipped X — reduced errors."}]}],
    }
    cleaned = sanitize_output_text(data)
    assert "—" not in cleaned["summary"]
    assert "—" not in cleaned["achievements"][0]
    assert "METRIC NEEDED" not in cleaned["achievements"][0]
    assert "—" not in cleaned["experience"][0]["bullets"][0]["text"]


def test_non_string_values_pass_through_unchanged():
    data = {"score": 86, "proceed": True, "tags": None}
    assert sanitize_output_text(data) == data
