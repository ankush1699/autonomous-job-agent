"""
generate_role_family.py
------------------------
Like generate_generic.py (deterministic, no LLM, no fabrication — every bullet
is verbatim master data), but for a *role family* rather than fully generic:
a small set of surgical, human-specified overrides (summary phrasing, skill
emphasis, headline) layered on top, when several postings share enough DNA
that one resume genuinely covers them without pretending to be more generic
than it is.

Reuses build_generic_data() from generate_generic.py for everything that
doesn't change (bullet selection, project inclusion, Grader exclusion) so the
two generators can't silently drift apart on shared logic.

Run from the project root:
    python src/generate_role_family.py
"""

import os
import sys
import json
import copy
import datetime

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT_DIR, "engine"))

from generate_generic import build_generic_data, MASTER_DATA_PATH
from pdf_generator import generate_pdfs


def build_role_family_data(
    master_data: dict,
    *,
    headline: str,
    summary_insert_after: str | None = None,
    summary_insert_text: str | None = None,
    summary_override: str | None = None,
    skill_reorders: dict[str, list[str]] | None = None,
    email_override: str | None = None,
    company_label: str,
) -> dict:
    """
    headline               — replaces the generic headline.
    email_override          — use this email instead of master data's default,
                              scoped to this one generation call only (e.g. a
                              company-specific tracking alias). Updates both
                              the display email and its mailto: link; master
                              data itself is left untouched.
    summary_insert_after / summary_insert_text
                            — surgical splice mode: summary_insert_text is
                              inserted immediately after summary_insert_after
                              (which must already exist in the summary — raises
                              if not, so an upstream edit can't silently no-op
                              this). Use for small phrasing/emphasis additions.
    summary_override        — full-replacement mode: use exactly this text
                              instead of splicing. Use when the ask is a
                              rewrite/shortening, not an addition — e.g.
                              dropping award names or a clause, which a splice
                              can't express. Facts must still be verifiable
                              against master data; this function doesn't
                              re-derive them, so double-check before calling.
    skill_reorders         — {category: [items in desired order]}. Every item in
                              the override list must already exist in that
                              category (checked below) — this reorders for
                              emphasis, it never adds a skill.
    """
    data = build_generic_data(master_data)  # verbatim bullets, Grader exclusion, etc.

    # ── Summary ──────────────────────────────────────────────────────────────
    if summary_override is not None:
        data["summary"] = summary_override
    elif summary_insert_after is not None:
        if summary_insert_after not in data["summary"]:
            raise ValueError(
                f"summary_insert_after text not found in current summary — it may "
                f"have changed upstream. Looked for: {summary_insert_after!r}"
            )
        data["summary"] = data["summary"].replace(
            summary_insert_after, summary_insert_after + summary_insert_text, 1
        )

    # ── Skill reordering: emphasis only, never adds a skill not already present ──
    for category, order in (skill_reorders or {}).items():
        current = data["technical_skills"].get(category, [])
        missing = [s for s in order if s not in current]
        if missing:
            raise ValueError(f"skill_reorders[{category!r}] references skills not "
                              f"in master data: {missing} — refusing to fabricate them.")
        extra = [s for s in current if s not in order]  # anything not explicitly placed
        data["technical_skills"][category] = order + extra

    # ── Contact info override (scoped to this resume only) ─────────────────────
    if email_override is not None:
        data["personal_info"]["email"] = email_override
        data["personal_info"]["hrefemail"] = f"mailto:{email_override}"

    data["personal_info"]["headline"] = headline
    data["company_name"] = company_label

    return data


def generate(output_dir: str, **overrides) -> str:
    with open(MASTER_DATA_PATH) as f:
        master_data = json.load(f)

    final_data = build_role_family_data(master_data, **overrides)
    os.makedirs(output_dir, exist_ok=True)

    temp_json = os.path.join(output_dir, "temp_data.json")
    with open(temp_json, "w") as f:
        json.dump(final_data, f, indent=2)

    print(f"  Summary: {final_data['summary']}\n")
    for cat, items in final_data["technical_skills"].items():
        print(f"  {cat}: {items}")
    print()
    for emp in final_data["experience"]:
        print(f"  {emp['company']}  →  {len(emp['bullets'])} bullets")
    print()
    for proj in final_data["projects"]:
        print(f"  Project: {proj['name']}")

    generate_pdfs(temp_json, output_dir, gen_resume=True, gen_cl=False,
                  resume_template='generic_resume_template.tex')
    return output_dir
