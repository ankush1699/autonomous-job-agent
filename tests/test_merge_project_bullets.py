"""
tests/test_merge_project_bullets.py

Regression test for a real bug: Groq's editor call split one project's two
bullets into two separate TailoredProjectBullet array entries (one with only
bullet_1, another with only bullet_2, both under the same project_name)
instead of one object with both fields. Groq validates tool-call arguments
strictly server-side, so a required `str` field rejected the whole generation
with a 400 before it ever reached our code — and since llm_editor runs at
temperature=0, blind retries reproduce the same malformed split every time.

Fixed by relaxing bullet_1/bullet_2 to Optional (schemas.py) so the response
is accepted, then reconciling split entries in Python (graph.py's
merge_project_bullets, tested here).

Sep 2026: the preferred field is now `bullets` (every bullet of the project,
rewritten, same count/order as the original — a tailored resume carries the
same project depth as the base resume). It passes through as-is; bullet_1/
bullet_2 remain as the legacy two-bullet form. A project survives merging if
it has a non-empty `bullets` list OR both legacy fields.

Run: pytest tests/test_merge_project_bullets.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from graph import merge_project_bullets


def test_normal_single_entry_passes_through_unchanged():
    """The legacy two-bullet case — one correct object per project."""
    raw = [
        {"project_name": "Project A", "bullet_1": "Did X.", "bullet_2": "Achieved Y."},
    ]
    result = merge_project_bullets(raw)
    assert result == [{"project_name": "Project A", "bullet_1": "Did X.", "bullet_2": "Achieved Y.", "bullets": None}]


def test_bullets_list_passes_through_as_is():
    """Preferred form: the full rewritten bullet list, any length, in order."""
    raw = [{"project_name": "Pipeline", "bullets": ["one", "two", "three", "four"]}]
    result = merge_project_bullets(raw)
    assert len(result) == 1
    assert result[0]["bullets"] == ["one", "two", "three", "four"]


def test_bullets_list_alone_is_enough_to_survive():
    """No bullet_1/bullet_2 at all — a `bullets` list must not get the project dropped."""
    raw = [{"project_name": "Pipeline", "bullets": ["one", "two"]}]
    assert len(merge_project_bullets(raw)) == 1


def test_empty_strings_in_bullets_list_are_dropped():
    raw = [{"project_name": "P", "bullets": ["real", "", None, "also real"]}]
    assert merge_project_bullets(raw)[0]["bullets"] == ["real", "also real"]


def test_split_entries_for_same_project_are_merged():
    """
    Reproduces the exact real failure shape: one project's bullets split
    across two array entries under the same project_name.
    """
    raw = [
        {"project_name": "Multi-Task NLP", "bullet_1": "Built a model."},
        {"project_name": "Multi-Task NLP", "bullet_2": "Hit 87% accuracy."},
    ]
    result = merge_project_bullets(raw)
    assert len(result) == 1
    assert result[0]["project_name"] == "Multi-Task NLP"
    assert result[0]["bullet_1"] == "Built a model."
    assert result[0]["bullet_2"] == "Hit 87% accuracy."


def test_project_still_missing_a_bullet_after_merge_is_dropped():
    """
    If, even after merging every entry sharing a project_name, one legacy
    field is still missing AND there's no `bullets` list, the project is
    dropped entirely rather than passed downstream with a None field — the
    finalizer then keeps that project's original, untailored master-data
    bullets instead.
    """
    raw = [
        {"project_name": "Broken Project", "bullet_1": "Only this exists."},
    ]
    assert merge_project_bullets(raw) == []


def test_multiple_distinct_projects_all_survive():
    raw = [
        {"project_name": "Project A", "bullet_1": "A1", "bullet_2": "A2"},
        {"project_name": "Project B", "bullet_1": "B1"},
        {"project_name": "Project B", "bullet_2": "B2"},
        {"project_name": "Project C", "bullets": ["C1", "C2", "C3"]},
    ]
    result = merge_project_bullets(raw)
    names = {r["project_name"] for r in result}
    assert names == {"Project A", "Project B", "Project C"}


def test_handles_pydantic_model_objects_not_just_plain_dicts():
    """merge_project_bullets is called with real Pydantic model instances in
    production (draft.tailored_project_bullets), not plain dicts."""
    class FakeModel:
        def __init__(self, **kw):
            self._d = kw
        def model_dump(self):
            return self._d

    raw = [FakeModel(project_name="P", bullet_1="one", bullet_2="two")]
    assert merge_project_bullets(raw) == [{"project_name": "P", "bullet_1": "one", "bullet_2": "two", "bullets": None}]


def test_empty_input_returns_empty_list():
    assert merge_project_bullets([]) == []
    assert merge_project_bullets(None) == []
