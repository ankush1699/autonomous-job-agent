"""
tests/test_bullet_traceability.py

Deterministic floor on rewrite quality (Sep 2026). On a real run the writer
replaced specific source bullets (Figma, WCAG, SEO, Google Analytics, a
3-month engagement) with generic filler and an invented technology, and the
draft still validated at 95/100 because nothing flags "vague". So every
rewritten bullet is matched to its source; if it kept too little of the
source's content or dropped a number, it is reverted to the source text.

Run: pytest tests/test_bullet_traceability.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from graph import enforce_bullet_traceability

_SRC = [
    {"company_name": "Shifting Waters", "original_text":
        "Built a responsive, mobile-first website implementing WCAG accessibility standards, SEO best "
        "practices, and Google Analytics tracking, delivering a production-ready platform within a 3-month engagement."},
    {"company_name": "Shifting Waters", "original_text":
        "Automated web-form data ingestion into structured spreadsheets for executive reporting, "
        "eliminating manual data entry for program staff."},
    {"company_name": "TCS", "original_text":
        "Automated CI/CD pipelines using GitLab CI and Jenkins across development and staging environments, "
        "accelerating deployment cycles by 40% and eliminating manual release steps."},
]


def test_faithful_rewrite_is_kept():
    rewritten = [{"company_name": "Shifting Waters", "rewritten_text":
        "Shipped a mobile-first, WCAG-accessible website with SEO best practices and Google Analytics "
        "tracking, production-ready within a 3-month engagement."}]
    out, reverts = enforce_bullet_traceability(rewritten, _SRC)
    assert reverts == []
    assert out[0]["rewritten_text"].startswith("Shipped a mobile-first")


def test_generic_filler_is_reverted_to_source():
    rewritten = [{"company_name": "Shifting Waters", "rewritten_text":
        "Defined technical requirements and led implementation planning for new feature work, "
        "coordinating directly with stakeholders to translate product needs into engineering tasks."}]
    out, reverts = enforce_bullet_traceability(rewritten, _SRC)
    assert len(reverts) == 1 and "content" in reverts[0][1]
    assert out[0]["rewritten_text"] in {s["original_text"] for s in _SRC}


def test_dropping_the_sources_number_is_reverted():
    rewritten = [{"company_name": "TCS", "rewritten_text":
        "Automated CI/CD pipelines using GitLab CI and Jenkins across development and staging "
        "environments, eliminating manual release steps and improving release consistency."}]
    out, reverts = enforce_bullet_traceability(rewritten, _SRC)
    assert len(reverts) == 1 and "40%" in reverts[0][1]
    assert "40%" in out[0]["rewritten_text"]


def test_each_source_is_matched_at_most_once():
    # Two rewrites that both resemble source #1: the second must be forced onto
    # source #2 (and then reverted, since it shares nothing with it) — never
    # let two rewrites "claim" the same source and lose one.
    rewritten = [
        {"company_name": "Shifting Waters", "rewritten_text": "Built a WCAG-accessible, SEO-friendly, Google Analytics-tracked mobile-first website in a 3-month engagement."},
        {"company_name": "Shifting Waters", "rewritten_text": "Built a WCAG-accessible mobile-first website with SEO and Google Analytics tracking in a 3-month engagement."},
    ]
    out, reverts = enforce_bullet_traceability(rewritten, _SRC)
    texts = [o["rewritten_text"] for o in out]
    assert any("web-form data ingestion" in t for t in texts)  # source #2 restored, not lost


def test_matching_is_order_independent():
    # A weak bullet listed FIRST must not steal source #1 from the faithful
    # rewrite listed second. Global best-overlap assignment: the faithful one
    # keeps source #1 and survives; the weak one lands on source #2 and reverts.
    weak = {"company_name": "Shifting Waters", "rewritten_text":
            "Built a website within a 3-month engagement, coordinating with stakeholders on requirements."}
    faithful = {"company_name": "Shifting Waters", "rewritten_text":
                "Shipped a mobile-first, WCAG-accessible website with SEO best practices and Google Analytics "
                "tracking, production-ready within a 3-month engagement."}
    out, reverts = enforce_bullet_traceability([weak, faithful], _SRC)
    assert out[1]["rewritten_text"] == faithful["rewritten_text"]   # kept
    assert out[0]["rewritten_text"] == _SRC[1]["original_text"]      # weak -> reverted to source #2
    assert len(reverts) == 1


def test_unknown_company_passes_through_untouched():
    rewritten = [{"company_name": "Nowhere Inc", "rewritten_text": "Anything."}]
    out, reverts = enforce_bullet_traceability(rewritten, _SRC)
    assert out == rewritten and reverts == []
