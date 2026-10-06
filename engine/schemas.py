from pydantic import BaseModel, Field
from typing import List, Optional


# 1. Keyword extraction output (fit scoring moved to core/should_apply.py)
class TriageKeywords(BaseModel):
    keywords: List[str] = Field(
        description="The 12-15 most critical ATS keywords from the job description. Focus on specific technologies, frameworks, and hard skills. Exclude generic terms like 'communication' or 'teamwork'."
    )


# 2. Strategist Agent Output
class SelectedBullet(BaseModel):
    company_name: str = Field(description="The exact name of the company this bullet belongs to.")
    original_text: str = Field(description="The exact original string text of the bullet.")
    relevance_score: int = Field(description="Relevance to this JD on a 1-5 scale (5 = directly addresses a must-have requirement).")
    selection_reason: str = Field(description="One sentence: why this bullet was selected over others for this specific JD.")

class DroppedBullet(BaseModel):
    company_name: str = Field(description="The exact name of the company this bullet belongs to.")
    original_text: str = Field(description="The exact original string text of the bullet that was NOT selected.")
    drop_reason: str = Field(description="One sentence: why this bullet was dropped (e.g., 'low keyword overlap', 'superseded by stronger quantified bullet').")

class StrategistSelection(BaseModel):
    selected_bullets: List[SelectedBullet] = Field(
        description="All selected bullets across all employers. Each employer has a FIXED bullet "
        "count given in the prompt's SELECTION RULES (e.g. 'select exactly 6 bullets') — match that "
        "count exactly for every employer, not a range, and not fewer because the JD looks narrow."
    )
    selected_project_names: List[str] = Field(
        description="The EXACT string names of ALL projects listed under ALL PROJECTS in the prompt, "
        "copied verbatim, ordered by JD relevance (most relevant first). Every project must be "
        "included — this list drives ordering and tailoring emphasis, never inclusion/exclusion."
    )
    dropped_bullets: Optional[List[DroppedBullet]] = Field(
        default=None,
        description="All bullets that were considered but NOT selected, with a drop reason for each. Omit only if token budget is exhausted."
    )
    content_warnings: Optional[List[str]] = Field(
        default=None,
        description="Any bullets the Strategist flags as potentially overstated or unverifiable (e.g., metric seems inflated, claim lacks supporting context in the data)."
    )


# 3. Writer / Editor Agent Output
class RewrittenBullet(BaseModel):
    company_name: str = Field(description="The company this bullet belongs to.")
    rewritten_text: str = Field(description="The rewritten bullet using Action → Context → Result, incorporating JD keywords.")

class TailoredProjectBullet(BaseModel):
    project_name: str = Field(description="Exact project name, copied verbatim from the selected project list.")
    # bullet_1/bullet_2 are Optional, not required, even though the prompt asks
    # for exactly one object per project with BOTH fields populated. This is
    # deliberate: Groq's open-weight models have been observed splitting a
    # project's two bullets into two separate array entries (one with only
    # bullet_1, another with only bullet_2) instead of combining them — and
    # Groq validates tool-call arguments strictly server-side, so a `str`
    # (required) field rejects that split with a 400 before it ever reaches
    # our code, and retrying is useless at temperature=0 (same malformed
    # split reproduces deterministically). Making these Optional lets the
    # response through; graph.py's _merge_project_bullets() then reconciles
    # any split entries in Python, where we can actually see and fix them.
    bullet_1: Optional[str] = Field(
        default=None,
        description="Legacy two-bullet form (prefer `bullets`). First bullet: highlights the technical implementation most relevant to this JD."
    )
    bullet_2: Optional[str] = Field(
        default=None,
        description="Legacy two-bullet form (prefer `bullets`). Second bullet: the outcome or technical result. Keep a real number only if the source bullet has one; never invent a metric."
    )
    # Preferred form (Sep 2026): EVERY bullet of the project, rewritten, in the
    # same count and order as the original — a tailored resume carries the same
    # project depth as the base resume, not a fixed two-bullet cut. bullet_1/2
    # stay for backward compatibility with the Groq-split reconciliation in
    # graph.merge_project_bullets().
    bullets: Optional[List[str]] = Field(
        default=None,
        description="All of this project's bullets, rewritten for this JD, one string per bullet, SAME COUNT AND ORDER as the original project's bullets. A bullet whose source has a real number keeps that exact number; a bullet whose source has no number stays metric-free — never invent one."
    )

class WriterOutput(BaseModel):
    tailored_summary: str = Field(description="A 2-3 sentence professional summary tailored to the job description.")
    tailored_bullets: List[RewrittenBullet]
    tailored_project_bullets: Optional[List[TailoredProjectBullet]] = Field(
        default=None,
        description="Rewritten bullets for each selected project, emphasizing the aspects most relevant to this JD."
    )
    cover_letter_paragraphs: Optional[List[str]] = Field(
        default=None,
        description="A list of 4-5 paragraphs for the cover letter body. Each item is one separate paragraph."
    )
    prioritized_skill_categories: Optional[List[str]] = Field(
        default=None,
        description="Skill category names ordered from most to least relevant to this JD. Use exact names from master data."
    )


# 4. Resume Strategy (generated alongside Writer output, used by cover letter pipeline)
class ResumeStrategy(BaseModel):
    positioning_angle: str = Field(
        description="One sentence: the single strongest angle for positioning this candidate for this specific role. What makes them stand out?"
    )
    key_themes: List[str] = Field(
        description="3-4 thematic threads to weave through the cover letter (e.g., 'full-stack ownership', 'data-driven decision making'). Each is a short phrase."
    )
    keywords_to_emphasize: List[str] = Field(
        description="8-12 ATS keywords from the JD that should be woven naturally into the cover letter."
    )
    company_research_hooks: List[str] = Field(
        description="2-3 specific details from the JD (product, mission, tech challenge) that can open a cover letter paragraph authentically."
    )
    tone_guidance: str = Field(
        description="One sentence: recommended tone and style for the cover letter (e.g., 'Technical and precise with measured enthusiasm — avoid startup clichés')."
    )
    tailored_pitch: str = Field(
        description="2-3 sentence narrative pitch for this specific role. The cover letter should expand on this core idea."
    )
