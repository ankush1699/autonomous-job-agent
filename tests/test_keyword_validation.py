"""
tests/test_keyword_validation.py

Regression tests for two real leaks in _validate_keywords() (Sep 2026):
substring matching "verified" ITL via the inside of "tITLe", and a per-word
fallback "verified" Claude Code because "claude" and "code" each appeared
somewhere in master data — after which the writer wrote "including Claude
Code" into a summary as something the candidate had used. A verified keyword
is a claim the resume may make, so it has to exist as a whole phrase in the
candidate's data (word-boundary, plural-tolerant).

Run: pytest tests/test_keyword_validation.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from graph import _validate_keywords

_MASTER = {
    "technical_skills": {"Frontend": ["React", "Angular"], "Backend": ["RESTful APIs", "Node.js"],
                         "Generative AI": ["LangGraph", "Anthropic API"], "Languages": ["C++"]},
    "experience": [{"company": "TCS", "bullets": [
        {"text": "Led a frontend team of 4 engineers, driving code quality through structured code reviews.", "tags": []},
        {"text": "Automated web-form data ingestion into spreadsheets.", "tags": []},
    ]}],
    "projects": [{"name": "P", "tech_stack": ["Python"], "bullets": [
        {"text": "Reserving Claude Sonnet for final resume prose. Built with React.js.", "tags": []},
    ]}],
}


def test_substring_inside_a_word_is_not_verified():
    verified, gaps = _validate_keywords(["ITL"], _MASTER)          # "tITLe"? no — but "title" isn't even here; use "quality"
    assert "ITL" in gaps
    verified, gaps = _validate_keywords(["ualit"], _MASTER)        # inside "quality"
    assert "ualit" in gaps


def test_multiword_keyword_needs_the_whole_phrase():
    verified, gaps = _validate_keywords(["Claude Code"], _MASTER)  # "claude" and "code" both present, phrase absent
    assert "Claude Code" in gaps
    verified, gaps = _validate_keywords(["code reviews"], _MASTER)
    assert "code reviews" in verified


def test_plural_and_punctuation_tolerance():
    verified, _ = _validate_keywords(["RESTful API", "React", "Node.js", "web form", "C++", "LangGraph"], _MASTER)
    assert set(verified) == {"RESTful API", "React", "Node.js", "web form", "C++", "LangGraph"}


def test_absent_technology_is_a_gap():
    verified, gaps = _validate_keywords(["Kubernetes", "Harbor", "TTFT"], _MASTER)
    assert verified == [] and set(gaps) == {"Kubernetes", "Harbor", "TTFT"}
