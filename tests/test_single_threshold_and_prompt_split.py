"""
tests/test_single_threshold_and_prompt_split.py

Two changes from the Sep 2026 cleanup:

1. ONE apply threshold. core.should_apply.min_score() is the single source
   (UI-persisted scraper_settings["min_score"], else env — with
   SHOULD_APPLY_MIN_SCORE winning over the retired MIN_ALIGNMENT_SCORE).
   Every consumer (verdict `proceed`, scraper cut, notify, /api/meta) uses
   it, and a CACHED verdict's `proceed` is recomputed against the current
   threshold instead of the one in force when it was cached.

2. The scoring prompt is split into a static system message (cacheable —
   carries Anthropic's cache_control marker, Anthropic only) and a per-call
   human message holding just the JD.

Also covers engine/generate_generic.py's base-resume digest (full bullet
text, not the profile cache's 92-char truncation).

Run: pytest tests/test_single_threshold_and_prompt_split.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import should_apply
from scraper import scraper_settings, notify, score as score_mod

_CLEAN_JD = "We are hiring a backend software engineer. " + "Great team culture. " * 20


# --- 1. threshold -----------------------------------------------------------

def test_env_default_prefers_should_apply_over_retired_alignment_name():
    with patch.dict(os.environ, {"SHOULD_APPLY_MIN_SCORE": "81", "MIN_ALIGNMENT_SCORE": "60"}):
        assert should_apply._env_min_score() == 81


def test_env_default_falls_back_to_retired_name_then_75():
    with patch.dict(os.environ, {"SHOULD_APPLY_MIN_SCORE": "", "MIN_ALIGNMENT_SCORE": "68"}):
        assert should_apply._env_min_score() == 68
    with patch.dict(os.environ, {"SHOULD_APPLY_MIN_SCORE": "", "MIN_ALIGNMENT_SCORE": ""}):
        assert should_apply._env_min_score() == 75
    with patch.dict(os.environ, {"SHOULD_APPLY_MIN_SCORE": "not-a-number", "MIN_ALIGNMENT_SCORE": ""}):
        assert should_apply._env_min_score() == 75


def test_effective_min_score_comes_from_ui_settings_when_present():
    with patch.object(scraper_settings, "load", return_value={"min_score": 82}):
        assert should_apply.min_score() == 82


def test_effective_min_score_falls_back_to_env_when_settings_unreadable():
    with patch.object(scraper_settings, "load", side_effect=OSError("boom")):
        assert should_apply.min_score() == should_apply.MIN_SCORE


def test_scraper_settings_defaults_carry_min_score():
    with patch.dict(os.environ, {"SHOULD_APPLY_MIN_SCORE": "77"}):
        assert scraper_settings._env_defaults()["min_score"] == 77


def _consistent_llm_verdict(score=78):
    return {
        "rubric_version": should_apply.RUBRIC_VERSION, "stage": "llm", "proceed": True,
        "score": score, "reasoning": "r", "jd_summary": "s", "red_flags": [],
        "seniority_level": "mid", "role_type": "backend-swe",
        "sub_scores": {"tech": 36, "seniority": 20, "role_type": 15, "sponsorship_bonus": 7},
        "tailoring_recommended": False, "tailoring_reasoning": "t",
    }


def test_cached_verdict_proceed_is_recomputed_against_current_threshold():
    """Cached with proceed=True at a 75 bar; the bar is now 80 → proceed must
    flip to False without re-scoring (no LLM call)."""
    with patch.object(should_apply.jd_cache, "get_entry", return_value={"should_apply": _consistent_llm_verdict(78)}), \
         patch.object(should_apply, "min_score", return_value=80), \
         patch.object(should_apply, "_llm_score") as mock_llm:
        verdict = should_apply.evaluate(_CLEAN_JD, use_cache=True)
    mock_llm.assert_not_called()
    assert verdict["stage"] == "cache"
    assert verdict["score"] == 78
    assert verdict["proceed"] is False


def test_fresh_verdict_proceed_uses_effective_threshold():
    def fake_llm(jd, max_retries=3):
        v = _consistent_llm_verdict(78)
        v.pop("stage"); v.pop("proceed"); v.pop("red_flags")
        return v
    with patch.object(should_apply, "_llm_score", side_effect=fake_llm), \
         patch.object(should_apply, "min_score", return_value=70):
        assert should_apply.evaluate(_CLEAN_JD, use_cache=False)["proceed"] is True
    with patch.object(should_apply, "_llm_score", side_effect=fake_llm), \
         patch.object(should_apply, "min_score", return_value=80):
        assert should_apply.evaluate(_CLEAN_JD, use_cache=False)["proceed"] is False


def test_scraper_partition_and_notify_default_to_effective_threshold():
    jobs = [{"score": 79, "company": "A", "title": "t"}, {"score": 81, "company": "B", "title": "t"}]
    # score.py binds the function at import (`min_score as core_min_score`),
    # so patch that name; notify imports lazily at call time, so patching
    # core.should_apply.min_score is what it sees.
    with patch.object(score_mod, "core_min_score", return_value=80):
        high, low = score_mod.partition_by_score(jobs)
    assert [j["score"] for j in high] == [81] and [j["score"] for j in low] == [79]

    with patch.object(should_apply, "min_score", return_value=80), \
         patch.object(notify, "send_notification", return_value=True) as sent:
        assert notify.notify_high_matches(jobs) == 1
    assert sent.call_args.args[0]["score"] == 81


def test_server_settings_post_omits_min_score_when_client_did_not_send_it():
    import server
    base = dict(keywords=[], platforms={}, location="United States", remote_only=False,
                hours_old=24, entry_level_only=True, exclude_recruiting_agencies=False, daily_cap=25)
    with patch.object(scraper_settings, "save") as mock_save, \
         patch.object(scraper_settings, "load", return_value={"min_score": 75, **base}):
        server.update_scraper_settings(server.ScraperSettingsRequest(**base))
    assert "min_score" not in mock_save.call_args.args[0]

    with patch.object(scraper_settings, "save") as mock_save, \
         patch.object(scraper_settings, "load", return_value={"min_score": 80, **base}):
        server.update_scraper_settings(server.ScraperSettingsRequest(min_score=80, **base))
    assert mock_save.call_args.args[0]["min_score"] == 80


def test_server_settings_post_rejects_out_of_range_min_score():
    import server
    from fastapi import HTTPException
    base = dict(keywords=[], platforms={}, location="United States", remote_only=False,
                hours_old=24, entry_level_only=True, exclude_recruiting_agencies=False)
    with patch.object(scraper_settings, "save") as mock_save:
        try:
            server.update_scraper_settings(server.ScraperSettingsRequest(min_score=150, **base))
            assert False, "expected 422"
        except HTTPException as e:
            assert e.status_code == 422
    mock_save.assert_not_called()


def test_meta_reports_effective_threshold():
    import server
    with patch.object(should_apply, "min_score", return_value=83):
        assert server.get_meta()["min_score"] == 83


# --- 2. prompt split --------------------------------------------------------

def test_anthropic_messages_carry_cache_control_on_static_system_block():
    msgs = should_apply._build_messages("anthropic", "STATIC", "JOB DESCRIPTION:\nx")
    assert msgs[0].type == "system" and msgs[1].type == "human"
    block = msgs[0].content[0]
    assert block["text"] == "STATIC" and block["cache_control"] == {"type": "ephemeral"}
    assert msgs[1].content == "JOB DESCRIPTION:\nx"


def test_non_anthropic_messages_are_plain_strings():
    for provider in ("groq", "ollama"):
        msgs = should_apply._build_messages(provider, "STATIC", "JD")
        assert msgs[0].content == "STATIC" and msgs[1].content == "JD"


def test_llm_score_sends_jd_in_human_message_and_static_in_system():
    from unittest.mock import MagicMock
    result = MagicMock()
    result.tech_score, result.seniority_score, result.role_type_score, result.sponsorship_bonus_score = 40, 20, 15, 7
    result.reasoning, result.jd_summary = "r", "s"
    result.seniority_level, result.role_type = "mid", "fullstack-swe"
    result.tailoring_recommended, result.tailoring_reasoning = False, "t"
    llm = MagicMock()
    llm.with_structured_output.return_value.invoke.return_value = result
    with patch.object(should_apply, "get_chat_model", return_value=llm), \
         patch.object(should_apply, "cheap_provider", return_value="anthropic"):
        should_apply._llm_score("THE JD TEXT")
    sent = llm.with_structured_output.return_value.invoke.call_args.args[0]
    assert sent[0].type == "system" and "THE JD TEXT" not in sent[0].content[0]["text"]
    assert "SCORING INSTRUCTIONS" in sent[0].content[0]["text"]
    assert sent[1].type == "human" and sent[1].content.endswith("THE JD TEXT")


# --- 3. base-resume digest --------------------------------------------------

def test_base_resume_digest_has_full_bullets_and_sections(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
    from engine.generate_generic import build_base_resume_digest, write_base_resume_digest
    long_bullet = "Built a production Angular and Ionic hybrid mobile application for a UK-based financial services client, serving 10K+ users."
    data = {
        "personal_info": {"headline": "FULL-STACK ENGINEER"},
        "summary": "Summary text.",
        "technical_skills": {"Languages": ["Python", "Java"]},
        "experience": [{"title": "SWE", "company": "TCS", "location": "Pune", "duration": "2020 - 2024",
                        "bullets": [{"text": long_bullet}]}],
        "education": [{"degree": "B.E.", "university": "U", "cgpa": "8.9/10", "duration": "2017 - 2020", "coursework": ["DSA"]}],
        "projects": [{"name": "P", "tech_stack": ["Flask", "MongoDB"], "duration": "2025", "bullets": [{"text": "Did X."}]}],
        "achievements": ["8 awards"],
    }
    digest = build_base_resume_digest(data)
    for header in ("PROFESSIONAL SUMMARY", "TECHNICAL SKILLS", "PROFESSIONAL EXPERIENCE", "EDUCATION",
                   "TECHNICAL PROJECTS", "HONORS & ACHIEVEMENTS"):
        assert header in digest
    assert long_bullet in digest          # NOT truncated at 92 chars
    assert "..." not in digest
    assert "Flask, MongoDB" in digest and "8 awards" in digest

    out = tmp_path / "digest.txt"
    write_base_resume_digest(data, path=str(out))
    assert out.read_text() == digest
