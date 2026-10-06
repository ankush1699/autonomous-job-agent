"""
generate_generic.py
-------------------
Produces a polished, general-purpose resume PDF with no JD and no LLM calls.
Use this when cold-reaching out to employers or sharing with peers directly.

Selection logic (pure Python, deterministic):
  - Experience: up to resume_max_bullets per employer, quantified bullets first
  - Projects:   top N by recency (configurable via TOP_N_PROJECTS below)
  - Skills:     original category order from master data
  - Summary:    master data summary used as-is
  - Achievements: suppressed when 3+ employers are present (one-pager guard)

Run from the project root:
    python src/generate_generic.py
"""

import os
import json
import copy
import datetime
import sys

# Allow running from either project root or src/
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT_DIR, "engine"))

from pdf_generator import generate_pdfs

# ── Config ────────────────────────────────────────────────────────────────────
MASTER_DATA_PATH = os.path.join(ROOT_DIR, "Ankush_Master_Data.json")
TOP_N_PROJECTS   = 5   # how many projects to include (taken in master-data order)
ONE_PAGE         = False  # False = 2-page generic (show achievements); True = 1-page
# Same RESUME_OUTPUT_PATH env var graph.py's finalizer respects — keeps both
# generators writing to the same place by default (still overridable via the
# explicit output_dir param on main()/generate()).
OUTPUT_BASE_DIR = os.getenv(
    "RESUME_OUTPUT_PATH", os.path.join(os.path.expanduser("~"), "Documents", "Resumes")
)

# Skill category display order, per base-resume variant (Oct 2026):
#   fullstack (default, the PDF used for most applications): Frontend/Backend
#     lead right after Languages — the resume is a Software Engineer's first.
#   ai: Generative AI is promoted to second for AI-focused roles.
# "DevOps" (not "Cloud and DevOps") until real cloud experience exists — the
# old label advertised a category with no cloud in it. Rename it back when the
# AWS/GCP project is built and its skills are added.
SKILL_ORDERS = {
    "fullstack": ["Languages", "Frontend", "Backend", "Generative AI",
                  "DevOps", "Databases", "Testing", "Tools"],
    "ai":        ["Languages", "Generative AI", "Backend", "Frontend",
                  "DevOps", "Databases", "Testing", "Tools"],
}
SKILL_ORDER = SKILL_ORDERS["fullstack"]  # kept for callers that import the old name
DEFAULT_VARIANT = "fullstack"
VARIANT_LABELS = {"fullstack": "FullStack_AI_General", "ai": "AI_Systems_General"}
# ──────────────────────────────────────────────────────────────────────────────


def _priority(bullet: dict, variant: str) -> int:
    """`base_priority` may be an int (same for every variant) or a dict keyed by
    variant ({"fullstack": 1, "ai": 2}); missing -> sorts after every prioritized bullet."""
    p = bullet.get("base_priority", 10_000)
    if isinstance(p, dict):
        return p.get(variant, p.get("default", 10_000))
    return p


def select_bullets(bullets: list, max_bullets: int, variant: str = "fullstack") -> list:
    """
    Return up to max_bullets from a bullet list.

    Order: an explicit `base_priority` (lower = earlier; int, or a per-variant
    dict) when the master data sets any, then quantified bullets before
    unquantified, then original master-data order. base_priority exists because
    "quantified first" alone buried a core-stack bullet (Spring Boot / token
    handling) behind weaker quantified ones.
    """
    indexed = list(enumerate(bullets))
    indexed.sort(key=lambda ib: (
        _priority(ib[1], variant),
        0 if ib[1].get("is_quantified") else 1,
        ib[0],
    ))
    return [b for _, b in indexed][:max_bullets]


def build_generic_data(master_data: dict, variant: str = DEFAULT_VARIANT) -> dict:
    """Assemble the final_data dict in the same shape the LaTeX template expects.

    variant: "fullstack" (default) or "ai" — picks the headline, summary and
    skill order for that positioning from master data (`headline_variants`,
    `summary_variants`). Bullets and projects are identical across variants;
    nothing is generated, only chosen from text that already exists.
    """
    if variant not in SKILL_ORDERS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {sorted(SKILL_ORDERS)}")
    data = copy.deepcopy(master_data)
    pi = data.get("personal_info", {})
    pi["headline"] = (pi.get("headline_variants") or {}).get(variant, pi.get("headline", ""))
    data["summary"] = (data.get("summary_variants") or {}).get(variant, data["summary"])

    # ── Experience ────────────────────────────────────────────────────────────
    # Employers marked include_in_generic: false (e.g. thin, low-signal roles
    # like a part-time TA/Grader position) are only worth including on a
    # resume tailored to a JD where that experience is actually relevant — the
    # generic resume has no JD to judge relevance against, so it defaults to
    # excluding them entirely rather than always padding every employer in.
    data["experience"] = [
        exp for exp in data["experience"] if exp.get("include_in_generic", True)
    ]

    for exp in data["experience"]:
        # Per-variant bullet count (e.g. Handshake: 1 on the full-stack resume, 2 on
        # the AI one); falls back to the shared resume_max_bullets, which the tailored
        # pipeline also uses, so the DEFAULT variant stays count-identical to tailored.
        max_b = (exp.get("resume_max_bullets_by_variant") or {}).get(variant, exp.get("resume_max_bullets", 4))
        selected = select_bullets(exp["bullets"], max_b, variant)
        # Normalise to the shape the template uses (text + is_quantified only)
        exp["bullets"] = [
            {"text": b["text"], "is_quantified": b.get("is_quantified", False)}
            for b in selected
        ]

    # ── Projects ─────────────────────────────────────────────────────────────
    data["projects"] = data["projects"][:TOP_N_PROJECTS]
    # Per-variant project order (master order is the full-stack order); any project
    # not named keeps its place after the named ones — nothing is ever dropped.
    wanted = (master_data.get("project_order_by_variant") or {}).get(variant)
    if wanted:
        rank = {n: i for i, n in enumerate(wanted)}
        data["projects"].sort(key=lambda pr: rank.get(pr["name"], len(rank)))
    # Keep original bullet text; strip schema-only fields the template doesn't need
    for proj in data["projects"]:
        proj["bullets"] = [
            {"text": b["text"]}
            for b in proj.get("bullets", [])
        ]

    # ── Descending chronological order (newest employer first) ────────────────
    data["experience"] = list(reversed(data["experience"]))

    # ── Page guard ───────────────────────────────────────────────────────────
    if ONE_PAGE and len(data["experience"]) >= 3:
        data["achievements"] = []
        print("  [ONE-PAGER] 3 employers — achievements suppressed.")

    # ── Achievements suppressed unconditionally on the generic resume ─────────
    # The summary line already says "...recognized with 8 performance awards"
    # (master_data["summary"]), so a dedicated Honors & Achievements section
    # below just repeats the same fact in more words. User call, Sep 2026.
    # Tailored resumes now match this too (engine/graph.py's finalizer_node
    # suppresses achievements unconditionally as well, Sep 2026 — the two
    # resume types are meant to be structurally identical except bullet
    # wording and section ordering).
    data["achievements"] = []

    # ── Skill category ordering ────────────────────────────────────────────────
    original_skills = data["technical_skills"]
    skill_order = SKILL_ORDERS[variant]
    reordered = {cat: original_skills[cat] for cat in skill_order if cat in original_skills}
    for cat in original_skills:
        if cat not in reordered:
            reordered[cat] = original_skills[cat]
    data["technical_skills"] = reordered

    # ── Professional headline ──────────────────────────────────────────────────
    # Lives in master_data["personal_info"]["headline"] now (single source,
    # shared with tailored resumes via finalizer_node's deepcopy of master_data
    # — no separate hardcode needed here anymore). "AI Engineer" not "AI/ML
    # Engineer" — the candidate's actual demonstrated work is LLM application/
    # agentic engineering (LangChain, LangGraph, RAG, prompt engineering), not
    # classical ML engineering (model training, feature pipelines, MLOps),
    # which is a heavier, different specialization than their background
    # supports. Explicit user call, not a guess.

    # ── Fields the template expects ───────────────────────────────────────────
    # company_name is also used as the output filename suffix (see pdf_generator.py:
    # "Ankush_Resume_{company_name}") — labeled here instead of left blank so the
    # generic PDF doesn't end up as "Ankush_Resume_.pdf".
    data["company_name"]           = VARIANT_LABELS[variant]
    data["job_title"]              = ""
    data["cover_letter_paragraphs"] = []

    return data


def build_base_resume_digest(final_data: dict) -> str:
    """
    Plain-text rendering of EXACTLY what the generic PDF prints — the same
    selected bullets, projects, ordered skills, summary and achievements that
    went into the template — for core/should_apply.py's tailoring judgment
    ("does the base resume already cover this JD?"). Built from the selected
    data, not by parsing the PDF, so it can't drift from the PDF and needs
    no PDF-text dependency.

    Replaces the earlier practice of pointing that judgment at the profile
    cache dump, which truncates every bullet to 92 chars + "..." and reflects
    the FULL master data rather than what was actually selected for the page
    — the LLM was judging resume coverage from sentences cut off mid-clause.
    """
    lines = ["BASE RESUME (generic, untailored) — exactly what is printed on the PDF", ""]
    headline = (final_data.get("personal_info") or {}).get("headline")
    if headline:
        lines += [headline, ""]
    if final_data.get("summary"):
        lines += ["PROFESSIONAL SUMMARY", final_data["summary"], ""]

    lines.append("TECHNICAL SKILLS")
    for category, values in (final_data.get("technical_skills") or {}).items():
        lines.append(f"- {category}: {', '.join(values)}")
    lines.append("")

    exps = final_data.get("experience") or []
    for section, heading in (("professional", "PROFESSIONAL EXPERIENCE"), ("internship", "INTERNSHIPS & FELLOWSHIPS")):
        group = [e for e in exps if (e.get("section") == "internship") == (section == "internship")]
        if not group:
            continue
        lines.append(heading)
        for exp in group:
            lines.append(f"{exp.get('title', '')} | {exp.get('company', '')} | "
                         f"{exp.get('location', '')} | {exp.get('duration', '')}")
            if exp.get("tech_stack"):
                lines.append(f"  Tech: {', '.join(exp['tech_stack'])}")
            for b in exp.get("bullets") or []:
                lines.append(f"  • {b['text'] if isinstance(b, dict) else b}")
        lines.append("")

    lines.append("EDUCATION")
    for edu in final_data.get("education") or []:
        gpa = edu.get("gpa") or edu.get("cgpa") or ""
        lines.append(f"{edu.get('degree', '')} | {edu.get('university', '')} | "
                     f"{('GPA ' + gpa) if gpa else ''} | {edu.get('duration', '')}")
        if edu.get("coursework"):
            lines.append(f"  Coursework: {', '.join(edu['coursework'])}")
    lines.append("")

    lines.append("TECHNICAL PROJECTS")
    for proj in final_data.get("projects") or []:
        stack = proj.get("tech_stack") or ""
        stack = ", ".join(stack) if isinstance(stack, list) else stack
        lines.append(f"{proj.get('name', '')} | {stack} | {proj.get('duration', '')}")
        for b in proj.get("bullets") or []:
            lines.append(f"  • {b['text'] if isinstance(b, dict) else b}")

    if final_data.get("achievements"):
        lines += ["", "HONORS & ACHIEVEMENTS"]
        lines += [f"  • {a}" for a in final_data["achievements"]]
    return "\n".join(lines) + "\n"


def write_base_resume_digest(final_data: dict, path: str | None = None) -> str:
    """Write the digest to core/should_apply.py's BASE_RESUME_CONTENT_PATH
    (the file the scorer reads) — every generic regeneration refreshes it."""
    if path is None:
        sys.path.insert(0, ROOT_DIR)
        from core.should_apply import BASE_RESUME_CONTENT_PATH  # lazy: keeps this script light
        path = BASE_RESUME_CONTENT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(build_base_resume_digest(final_data))
    return path


def main(output_dir: str | None = None, variant: str = DEFAULT_VARIANT):
    print("─" * 50)
    print(f"  GENERIC RESUME GENERATOR  (no LLM, no JD) — variant: {variant}")
    print("─" * 50)

    with open(MASTER_DATA_PATH) as f:
        master_data = json.load(f)

    final_data = build_generic_data(master_data, variant=variant)

    # ── Output folder ─────────────────────────────────────────────────────────
    if output_dir is None:
        timestamp   = datetime.datetime.now().strftime("%d_%m_%H_%M")
        folder_name = f"Generic_Resume_{timestamp}" + ("" if variant == DEFAULT_VARIANT else f"_{variant}")
        output_dir  = os.path.join(OUTPUT_BASE_DIR, folder_name)
    os.makedirs(output_dir, exist_ok=True)

    # ── Save JSON snapshot ────────────────────────────────────────────────────
    temp_json = os.path.join(output_dir, "temp_data.json")
    with open(temp_json, "w") as f:
        json.dump(final_data, f, indent=2)

    # ── Print what was selected ───────────────────────────────────────────────
    print()
    for emp in final_data["experience"]:
        print(f"  {emp['company']}  →  {len(emp['bullets'])} bullets")
    print()
    for proj in final_data["projects"]:
        print(f"  Project: {proj['name']}")
    print()

    # ── Generate PDF (resume only — no cover letter) ──────────────────────────
    generate_pdfs(temp_json, output_dir, gen_resume=True, gen_cl=False,
                  resume_template='generic_resume_template.tex')

    # Keep the scorer's picture of "what the base resume actually says" in
    # lockstep with the PDF that was just produced.
    # Only the default (full-stack) variant is "the base resume" the scorer
    # compares JDs against; the AI variant must not overwrite that digest.
    if variant == DEFAULT_VARIANT:
        digest_path = write_base_resume_digest(final_data)
        print(f"  Base-resume digest → {digest_path}")

    print(f"\n  Output → {output_dir}")
    print("─" * 50)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Generate the base (no-LLM) resume.")
    ap.add_argument("--variant", choices=sorted(SKILL_ORDERS), default=DEFAULT_VARIANT,
                    help="fullstack (default) or ai — headline, summary and skill order for that positioning")
    main(variant=ap.parse_args().variant)
