"""
tests/test_company_resolver.py

"Type a company name, we work out how to watch it" (scraper/company_resolver.py).
All network probes are replaced with fakes: no request leaves the machine.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper import company_resolver as cr


def test_slug_candidates_drop_legal_suffixes_and_dedupe():
    assert cr.slug_candidates("Scale AI, Inc.") == ["scaleai", "scale-ai", "scale"]
    assert cr.slug_candidates("Stripe") == ["stripe"]
    assert cr.slug_candidates("   ") == []


def test_parse_url_recognises_each_board_and_linkedin():
    assert cr.parse_url("https://boards.greenhouse.io/stripe/jobs/123") == {"source": "greenhouse", "identifier": "stripe"}
    assert cr.parse_url("https://job-boards.greenhouse.io/anthropic") == {"source": "greenhouse", "identifier": "anthropic"}
    assert cr.parse_url("https://jobs.lever.co/palantir") == {"source": "lever", "identifier": "palantir"}
    assert cr.parse_url("https://jobs.ashbyhq.com/openai") == {"source": "ashby", "identifier": "openai"}
    assert cr.parse_url("https://www.linkedin.com/company/microsoft/") == {"source": "linkedin", "identifier": "microsoft"}
    wd = cr.parse_url("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite")
    assert wd["source"] == "workday" and wd["identifier"].startswith("https://nvidia.wd5")
    assert cr.parse_url("https://example.com/careers") is None


def _fake_probes(monkeypatch, found):
    def make(src):
        return lambda slug: found.get((src, slug))
    monkeypatch.setattr(cr, "_PROBES", {src: make(src) for src in ("greenhouse", "lever", "ashby", "linkedin")})


def test_detect_prefers_the_company_board_and_flags_name_mismatch(monkeypatch):
    _fake_probes(monkeypatch, {
        ("greenhouse", "stripe"): {"source": "greenhouse", "identifier": "stripe", "label": "Stripe", "open_jobs": 50},
        ("lever", "stripe"): {"source": "lever", "identifier": "stripe", "label": "stripe", "open_jobs": 2},
        ("linkedin", "stripe"): {"source": "linkedin", "identifier": 2135371, "label": "Stripe", "open_jobs": None},
    })
    out = cr.detect("Stripe")
    assert [c["source"] for c in out] == ["greenhouse", "lever", "linkedin"]   # boards first, most jobs first
    assert out[0]["name_matches"] is True


def test_detect_falls_back_to_linkedin_for_big_employers(monkeypatch):
    _fake_probes(monkeypatch, {("linkedin", "amazon"): {"source": "linkedin", "identifier": 1586, "label": "Amazon", "open_jobs": None}})
    out = cr.detect("Amazon")
    assert len(out) == 1 and out[0]["identifier"] == 1586 and out[0]["name_matches"] is True


def test_detect_marks_an_unrelated_board_with_the_same_slug(monkeypatch):
    _fake_probes(monkeypatch, {("greenhouse", "meta"): {"source": "greenhouse", "identifier": "meta", "label": "Meta Materials Inc", "open_jobs": 3}})
    out = cr.detect("Meta")
    assert out[0]["name_matches"] is False      # "Meta Materials Inc" is not Meta: the UI warns before watching
    assert cr.detect("Facebook") == []          # nothing probed for that slug


def test_legal_suffixes_do_not_break_a_real_match(monkeypatch):
    _fake_probes(monkeypatch, {("greenhouse", "stripe"): {"source": "greenhouse", "identifier": "stripe", "label": "Stripe, Inc.", "open_jobs": 9}})
    assert cr.detect("Stripe")[0]["name_matches"] is True


def test_detect_with_a_pasted_url_probes_only_that(monkeypatch):
    _fake_probes(monkeypatch, {("ashby", "openai"): {"source": "ashby", "identifier": "openai", "label": "openai", "open_jobs": 100}})
    out = cr.detect("OpenAI", url="https://jobs.ashbyhq.com/openai")
    assert len(out) == 1 and out[0]["source"] == "ashby"


def test_workday_url_needs_no_probe(monkeypatch):
    _fake_probes(monkeypatch, {})
    out = cr.detect("NVIDIA", url="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite")
    assert out[0]["source"] == "workday" and out[0]["label"] == "NVIDIA"


def test_nothing_found_returns_empty(monkeypatch):
    _fake_probes(monkeypatch, {})
    assert cr.detect("Some Tiny Startup") == []
