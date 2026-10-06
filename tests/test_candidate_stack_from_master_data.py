"""
tests/test_candidate_stack_from_master_data.py

Regression test for a real accuracy bug: core/should_apply.py used to score
candidates against a hand-typed, separately-maintained CANDIDATE STACK
string that had drifted out of sync with the real Ankush_Master_Data.json —
missing skills the actual resume already had (RAG, Hugging Face, Prompt
Engineering, Distributed Systems, Postman, Jira, Figma, Agile/Scrum),
silently under-crediting tech matches on all of them.

_load_candidate_stack() now reads technical_skills straight from
Ankush_Master_Data.json on every call (same file every resume is generated
from), so a master-data edit takes effect on the very next scoring call.

Run: pytest tests/test_candidate_stack_from_master_data.py -v
"""
import json
import os
import pytest
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.should_apply import _load_candidate_stack, _CANDIDATE_STACK_FALLBACK


def test_reads_skills_from_master_data(tmp_path):
    master_data = {"technical_skills": {"Languages": ["Python", "Rust"], "Tools": ["Jira"]}}
    fake_path = tmp_path / "master.json"
    fake_path.write_text(json.dumps(master_data))

    with patch("core.should_apply.MASTER_DATA_PATH", str(fake_path)):
        stack = _load_candidate_stack()

    assert "Python" in stack
    assert "Rust" in stack
    assert "Jira" in stack


def test_edit_to_master_data_takes_effect_next_call_no_restart(tmp_path):
    fake_path = tmp_path / "master.json"
    fake_path.write_text(json.dumps({"technical_skills": {"Languages": ["Python"]}}))

    with patch("core.should_apply.MASTER_DATA_PATH", str(fake_path)):
        first = _load_candidate_stack()
        assert "Rust" not in first

        fake_path.write_text(json.dumps({"technical_skills": {"Languages": ["Python", "Rust"]}}))
        second = _load_candidate_stack()
        assert "Rust" in second


def test_falls_back_when_master_data_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.json"
    with patch("core.should_apply.MASTER_DATA_PATH", str(missing_path)):
        assert _load_candidate_stack() == _CANDIDATE_STACK_FALLBACK


def test_falls_back_when_master_data_malformed(tmp_path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not valid json")
    with patch("core.should_apply.MASTER_DATA_PATH", str(bad_path)):
        assert _load_candidate_stack() == _CANDIDATE_STACK_FALLBACK


@pytest.mark.personal_data
def test_real_master_data_has_skills_the_old_hardcoded_block_was_missing():
    """
    Confirms the actual drift that motivated this fix: the real master data
    (used unpatched here — this is the live file) has skills the old
    hand-typed CANDIDATE STACK never had.
    """
    stack = _load_candidate_stack()
    # "RAG" used to be in this list; removed from master data Oct 2026 because no
    # project or role backs it (see docs/RESUME_OVERHAUL_2026-10.md). The point of
    # this test is unchanged: the stack comes from master data, not a typed block.
    for previously_missing_skill in ["Hugging Face", "Jira", "Prompt Engineering"]:
        assert previously_missing_skill in stack
