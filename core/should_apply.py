"""
core/should_apply.py — the single "should I apply?" gate.

Runs BEFORE any tailoring work, in three escalating stages:

  1. Red-flag regex (free, instant): no-sponsorship / clearance / ITAR
     language kills the posting with score 0.
  2. JD cache (free, instant): a posting already scored — from any platform
     or entry mode — returns its cached verdict.
  3. LLM rubric on the cheap tier (Claude Haiku first — see core/llm.py):
     tech 0-45, seniority 0-25, role type 0-15, sponsorship 0-15.
     Sub-scores are range-checked, cross-checked against the model's own
     category labels, and summed in Python — never trusted from the model.

This replaces both of the old scoring paths (triage_node's inline rubric and
shared/scorer.py) so scraper mode, manual mode, and autofill prep all agree
on one number.

Threshold: SHOULD_APPLY_MIN_SCORE (default 75).

RUBRIC_VERSION: bumped whenever the rubric's meaning changes. Cached
verdicts carry the version they were scored under; a mismatch on cache read
is treated as a miss and re-scored, so a rubric change can never silently
serve numbers computed under old rules as if they were comparable to new
ones. v2 (Sep 2026): tech_score gained OR-list handling and a capped
denominator; sponsorship_bonus became a pure sponsorship signal (the
nice-to-have bonus that was double-counting tech moved into tech_score).
"""

import json
import os
import time
from typing import Optional

from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import get_chat_model, cheap_provider, is_quota_exhausted, next_cheap_provider
from core.red_flags import find_red_flags
from core import jd_cache
from core.profile_cache import MASTER_DATA_PATH

def _env_min_score() -> int:
    """
    Env default for the apply threshold. SHOULD_APPLY_MIN_SCORE wins; the
    scraper's older MIN_ALIGNMENT_SCORE name is honored as a fallback so an
    existing .env keeps working. These used to be two independent knobs read
    in four places (here, scraper/score.py, notify.py, scheduler.py) with the
    same meaning — they only agreed because both happened to be 75.
    """
    raw = os.getenv("SHOULD_APPLY_MIN_SCORE") or os.getenv("MIN_ALIGNMENT_SCORE") or "75"
    try:
        return int(raw)
    except ValueError:
        return 75


MIN_SCORE = _env_min_score()


def min_score() -> int:
    """
    The EFFECTIVE apply threshold — the one number every consumer uses: the
    `proceed` flag on verdicts, the scraper's candidate cut, notifications,
    and /api/meta. It's the UI-editable value persisted in
    scraper_settings.json (Scraper tab → "Min score") when present, else the
    env default above. Read fresh on every call (tiny local JSON, not a hot
    path) so a change in the UI applies to the very next scoring/scrape with
    no restart — same pattern as every other scraper setting.
    """
    try:
        from scraper import scraper_settings  # lazy: no core→scraper import at module load
        value = scraper_settings.load().get("min_score")
        if value is not None:
            return int(value)
    except Exception:
        pass
    return MIN_SCORE

# Bump on any change to the rubric's MEANING (field descriptions, tier
# values, formula). See module docstring. Cache reads compare against this.
RUBRIC_VERSION = 2

# Only used if Ankush_Master_Data.json is missing/unreadable — should never
# normally be hit, so it doesn't need to be kept in sync with real data.
_CANDIDATE_STACK_FALLBACK = "Python, JavaScript, TypeScript, Java, SQL, Angular, Spring Boot, AWS, Docker"


def _load_candidate_stack() -> str:
    """
    Builds the CANDIDATE STACK block straight from Ankush_Master_Data.json —
    the SAME file every actual resume is generated from — instead of a
    separately hand-maintained string that silently drifts out of sync.
    Real drift this was written to fix: the old hardcoded block was missing
    skills the real master data already had (RAG, Hugging Face, Prompt
    Engineering, Distributed Systems, Postman, Jira, Figma, Agile/Scrum),
    meaning scoring was silently under-crediting matches on all of them.
    Read fresh on every call (a small local JSON file, not a hot path) so a
    master-data edit takes effect on the very next scoring call — no
    restart, no manual regenerate step.
    """
    try:
        with open(MASTER_DATA_PATH) as f:
            master_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _CANDIDATE_STACK_FALLBACK
    skills = master_data.get("technical_skills", {})
    if not skills:
        return _CANDIDATE_STACK_FALLBACK
    return "\n".join(f"{category}: {', '.join(values)}" for category, values in skills.items())


# Plain-text digest of exactly what's printed on the generic base resume
# (engine/generate_generic.py's output) — experience bullets actually
# selected, projects actually included, skills actually listed. NOT the same
# as _load_candidate_stack(): that's the full master-data skill list (used
# for "should I apply at all"), this is only what a recruiter reading the
# ACTUAL PDF would see (used for "does the base resume already cover this
# JD, or is tailoring worth it"). Two genuinely different questions — see
# the tailoring_recommended field below.
BASE_RESUME_CONTENT_PATH = os.path.join(
    os.path.expanduser("~"), "Documents", "Base Resume", "base_resume_content.txt"
)


_BASE_RESUME_MISSING = (
    "No base resume on file yet — treat as unknown coverage, not as evidence "
    "the resume is weak. Default tailoring_recommended to true in this case."
)


def _load_base_resume_content() -> str:
    try:
        with open(BASE_RESUME_CONTENT_PATH) as f:
            return f.read()
    except OSError:
        return _BASE_RESUME_MISSING


class _SubScores(BaseModel):
    # Sub-score fields are typed `int | str` rather than plain `int`. Groq's
    # open-weight models (unlike Anthropic) sometimes emit these as JSON
    # strings ("15") instead of native integers even though the schema says
    # integer — and Groq validates tool-call arguments strictly server-side,
    # rejecting the whole generation with a 400 before it ever reaches our
    # code. A plain `int` field produces a schema that only accepts integers
    # and triggers this; `int | str` produces a permissive schema, and we
    # coerce to int ourselves in _llm_score() after receiving the response.
    tech_score: int | str = Field(
        description=(
            "Tech Stack Match score 0-45. Follow these steps in order:\n"
            "STEP 1 — List the JD's REQUIRED technologies (must-have / minimum "
            "qualifications only — NOT preferred/nice-to-have). Treat any group joined "
            "by 'or' / 'one of' / 'such as' as ONE requirement that is satisfied if the "
            "candidate has ANY item in it (e.g. 'Python, C++, Java, or JavaScript' is a "
            "single requirement, met by Python alone). If this yields more than 6 "
            "requirements, keep only the 6 most emphasized — long JDs must not be "
            "penalized just for listing more things.\n"
            "STEP 2 — base = round((requirements_met / requirements_total) * 40). "
            "If the JD lists no specific required technologies at all, base = 25.\n"
            "STEP 3 — preferred bonus: +1 for each PREFERRED/nice-to-have technology the "
            "candidate has in the CANDIDATE STACK, max +5.\n"
            "STEP 4 — If the role's PRIMARY language/framework (the one it is built "
            "around, usually first/most frequent/in the title) is NOT in the CANDIDATE "
            "STACK, hard-cap the total at 15.\n"
            "tech_score = min(45, base + bonus), then apply the STEP 4 cap."
        )
    )
    seniority_score: int | str = Field(
        description=(
            "Seniority Alignment score 0-25. Use EXACTLY these values: "
            "25 = New Grad / Entry (0-2 YOE) or Junior/Associate (1-3 YOE). "
            "20 = Mid-level (2-5 YOE). "
            " 8 = Unspecified or unclear level. "
            " 5 = Senior (5+ YOE). "
            " 0 = Lead / Staff / Principal / Director / VP / Executive. "
            "This measures the JD's level ONLY, from its required YOE and title. It is "
            "NOT a fit score: never raise or lower it because the candidate looks too "
            "junior or too senior for the role, lacks US experience, or anything else "
            "about the candidate. seniority_level and seniority_score MUST agree: "
            "entry/junior -> 25, mid -> 20, unspecified -> 8, senior -> 5, lead+ -> 0."
        )
    )
    role_type_score: int | str = Field(
        description=(
            "Role Type Fit score 0-15. Use EXACTLY these values: "
            "15 = Full-stack / Backend / Frontend SWE, or AI / LLM Engineer (building LLM "
            "applications, agentic pipelines, prompt engineering, RAG — the candidate's "
            "actual demonstrated work). "
            "13 = Software Engineer (generic). 10 = Mobile Engineer. "
            " 8 = Data Engineer/Analyst, or classical ML Engineer (model training, feature "
            "pipelines, MLOps, production ML systems — a DIFFERENT and heavier specialization "
            "than the candidate's LLM-application background; do not conflate an 'ML Engineer' "
            "titled/scoped role with the 15-point AI/LLM Engineer bucket above just because "
            "both mention machine learning). "
            "7 = DevOps / SRE / Platform. "
            " 5 = QA / SDET. 3 = Other technical. 0 = Non-technical."
        )
    )
    sponsorship_bonus_score: int | str = Field(
        description=(
            "Visa Sponsorship score 0-15 — about sponsorship ONLY, nothing else. "
            "Use EXACTLY one of these values: "
            "15 = JD explicitly says it sponsors / offers H-1B or visa sponsorship. "
            "11 = JD mentions OPT, CPT, F-1, or 'international candidates welcome' "
            "without an explicit sponsorship promise. "
            "7 = JD says nothing about sponsorship or work authorization (most jobs). "
            "0 = JD says no sponsorship, must be authorized without sponsorship, "
            "citizens/permanent residents only, or requires a clearance. "
            "Do NOT add points for skills or anything unrelated to sponsorship — "
            "tech match is scored separately in tech_score."
        )
    )
    seniority_level: str = Field(
        description="One of: 'entry', 'junior', 'mid', 'unspecified', 'senior', 'lead', 'executive'."
    )
    role_type: str = Field(
        description="Short label, e.g. 'backend-swe', 'fullstack-swe', 'ai-engineer', 'ml-engineer', 'devops', 'mobile', 'qa', 'other'."
    )
    reasoning: str = Field(
        description="One sentence: the single biggest reason this role is or isn't a strong match."
    )
    jd_summary: str = Field(
        description=(
            "Exactly 3 sentences: (1) what the role does day-to-day, "
            "(2) must-have technical requirements, (3) seniority level and visa notes."
        )
    )
    # A DELIBERATELY SEPARATE judgment from the four sub-scores above. Those
    # answer "should I apply to this job at all" (job vs. the candidate's
    # FULL profile/master data). This answers a different question: "does my
    # BASE RESUME, as already written, already read as a strong match for
    # THIS SPECIFIC JD, or would tailoring it actually help." A job can score
    # 95 on fit while the base resume still under-represents something this
    # JD specifically emphasizes (or vice versa) — reusing the apply-score as
    # a tailoring threshold conflates two different questions and was a real
    # bug in an earlier version of this feature.
    tailoring_recommended: bool = Field(
        description=(
            "Judge only REQUIRED/must-have skills the JD explicitly asks for — ignore "
            "nice-to-haves, ignore which of two equivalent technologies is emphasized "
            "(e.g. React vs Angular, Node.js vs Spring Boot are NOT gaps if the resume "
            "shows the other), and ignore emphasis/ordering/positioning differences "
            "entirely. Those do NOT count as reasons to tailor — a human reader would "
            "still see this as a strong match. "
            "True ONLY if a skill/technology the JD lists as REQUIRED is genuinely absent "
            "from the BASE RESUME CONTENT below (e.g. JD requires Kubernetes and the resume "
            "never mentions it) — not merely under-emphasized. "
            "False if the resume's required-skill coverage is already strong, even if it "
            "doesn't happen to highlight every secondary/preferred item the JD mentions. "
            "If BASE RESUME CONTENT says no base resume is on file, default to true."
        )
    )
    tailoring_reasoning: str = Field(
        description=(
            "One sentence. If tailoring_recommended=true: name the specific REQUIRED skill "
            "genuinely absent from the resume (not an emphasis nitpick). If false: say the "
            "required-skill coverage is already strong (may still note a secondary/optional "
            "item that's absent, but make clear it's not a required-skill gap)."
        )
    )


class _SubScoreOutOfRange(ValueError):
    """
    Raised when a sub-score field comes back outside its declared 0-cap
    range. Real failure this guards against: Groq's openai/gpt-oss-120b
    (swapped in after Groq deprecated llama-3.3-70b-versatile) doesn't
    reliably follow the numeric ranges given only in field descriptions —
    it emits values on what looks like a 0-100 scale per field (sometimes
    0-1 fractional) instead of the declared 0-45/0-25/0-15/0-15. Blindly
    clamping those with min(cap, value) makes EVERY dimension hit its
    ceiling at once, producing a false "100/100 perfect match" regardless
    of actual fit (reproduced live: a JD the model's own reasoning called
    "a weak fit" still clamped to a perfect score). Raising instead of
    clamping turns that silent wrong answer into a visible retry/fallback.
    """


def _coerce_subscore(raw, cap: int, name: str) -> int:
    value = int(raw)
    if not (0 <= value <= cap):
        raise _SubScoreOutOfRange(f"{name}={value} is outside its declared 0-{cap} range")
    return value


# Cross-check a category label against the score the same model call paired
# with it. Checked in code because the model doesn't reliably follow the
# rubric just from being told: measured across 218 real cached verdicts,
# role_type/score disagreed 44% of the time and seniority/score 34%. Real
# examples: role_type="ai-engineer" scored 9 (rubric: 15); seniority_level=
# "senior" scored 8 (the rubric's "unspecified" value). Both in-range, so
# _SubScoreOutOfRange never fires.
#
# Each label maps to the SET of scores it may legitimately carry — its own
# canonical value plus genuinely adjacent tiers. Strict equality was tried
# first and false-positived on real JDs that straddle a boundary ("open to
# entry-level to mid-level": either 25 or 20 is defensible), and every false
# positive is a paid hop. So: entry/junior/mid may borrow each other's
# value, a full-credit SWE role may carry the generic-SWE 13, etc. — but
# ai-engineer=9 and fullstack=10 (the actual bugs) are still rejected.
# Labels not in the map are skipped, not rejected: an unknown label isn't
# evidence of a bug the way a known label with the wrong score is.
_ROLE_TYPE_OK_SCORES = {
    "fullstack-swe": {15, 13}, "backend-swe": {15, 13}, "frontend-swe": {15, 13},
    "ai-engineer": {15, 13}, "llm-engineer": {15, 13},
    "swe": {13, 15}, "software-engineer": {13, 15},
    "mobile": {10, 13},
    "data-engineer": {8, 7, 10}, "data-analyst": {8, 7}, "ml-engineer": {8, 7, 10},
    "devops": {7, 8}, "sre": {7, 8}, "platform": {7, 8},
    "qa": {5, 3, 7}, "sdet": {5, 3, 7},
    "other": {3, 5, 0},
}
_SENIORITY_OK_SCORES = {
    "entry": {25, 20}, "junior": {25, 20},
    "mid": {20, 25},
    "unspecified": {8, 5, 20},
    "senior": {5, 8, 0},
    "lead": {0, 5}, "executive": {0, 5},
}


class _SubScoreInconsistent(ValueError):
    """
    Raised when a KNOWN category label (role_type / seniority_level) and its
    paired numeric score fall outside the tolerance set above — both numbers
    individually legal, but contradicting the label the same model call
    produced. Handled the same way as _SubScoreOutOfRange: hop to the next
    provider rather than trust a model that just contradicted itself.
    """


def _check_label_consistency(label: str, score: int, ok_map: dict, field_name: str) -> None:
    label_key = (label or "").strip().lower()
    ok = ok_map.get(label_key)
    if ok is not None and score not in ok:
        raise _SubScoreInconsistent(
            f"{field_name}={label!r} allows scores {sorted(ok, reverse=True)} per the rubric, but got {score}"
        )


def _verdict_is_consistent(verdict: dict) -> bool:
    """
    The same label/score cross-check, applied to an already-built verdict
    (a cached one). Returns False instead of raising so the cache-read path
    can treat a bad entry as a miss. Also False for any verdict scored under
    an older RUBRIC_VERSION, or with no version at all (pre-v2) — those
    numbers aren't comparable to current ones.
    """
    if verdict.get("rubric_version") != RUBRIC_VERSION:
        return False
    sub = verdict.get("sub_scores") or {}
    try:
        _check_label_consistency(verdict.get("role_type", ""), sub.get("role_type"),
                                 _ROLE_TYPE_OK_SCORES, "role_type")
        _check_label_consistency(verdict.get("seniority_level", ""), sub.get("seniority"),
                                 _SENIORITY_OK_SCORES, "seniority_level")
    except _SubScoreInconsistent:
        return False
    return True


def _static_prompt() -> str:
    """
    Everything identical across every scoring call — role, stack, context,
    base resume, instructions. Goes in the system message; only the JD varies
    per call (human message). This split is what makes Anthropic prompt
    caching possible: the static prefix (tool schema + system) carries a
    cache breakpoint, so on Haiku it's billed at 10% after the first call in
    a 5-minute window. Caveat measured live (Sep 2026): Haiku 4.5 only honors
    a breakpoint once the prefix is >= 4,096 tokens — it silently ignores
    smaller ones (cache_creation stays 0) — so this is a free no-op until
    the static content is big enough. Read fresh each call so master-data /
    base-resume edits apply immediately (both are tiny local files).
    """
    return f"""You are a senior technical recruiter scoring a job posting for a specific candidate.

CANDIDATE STACK (use this to count tech matches — do not invent skills):
{_load_candidate_stack()}

CANDIDATE CONTEXT:
- 4 years total experience (all at one Indian IT company — TCS)
- Just completed a US Master's degree — zero US work experience
- F-1 OPT visa — needs H-1B sponsorship for long-term employment
- Target: entry-level, junior, or mid-level SWE roles in the US
- Strong in full-stack (Angular + Spring Boot + Python), growing in AI/LLM tooling

BASE RESUME CONTENT (what's actually printed on the candidate's current
generic resume — a SEPARATE question from job fit above. Judge
tailoring_recommended by comparing THIS against the job description below,
not against the candidate's full profile):
{_load_base_resume_content()}

CALIBRATION EXAMPLES (how the field rules apply — same rules, worked end to end):

Example A — "Senior Backend Engineer, 6+ years. Required: Python, Django, PostgreSQL, Docker. Preferred: AWS." No mention of visas.
  tech_score: required = Python, Django, PostgreSQL, Docker (4); candidate has Python, Docker → base = round(2/4 × 40) = 20; preferred AWS in stack → +1; primary language Python IS in stack → no cap. tech_score = 21.
  seniority_score = 5 (Senior, 5+ YOE); seniority_level = "senior".
  role_type_score = 15 (backend SWE); role_type = "backend-swe".
  sponsorship_bonus_score = 7 (JD silent). Total 48.

Example B — "Software Engineer I (0-2 years). Required: proficiency in one of Java, Python, or C++; React or Angular; REST APIs; SQL. Nice to have: Docker, GraphQL. We sponsor H-1B visas."
  tech_score: "Java, Python, or C++" is ONE requirement (met), "React or Angular" is ONE (met), REST (met), SQL (met) → base = round(4/4 × 40) = 40; preferred: Docker in stack +1, GraphQL not → +1; primary language in stack → no cap. tech_score = 41.
  seniority_score = 25 (Entry, 0-2 YOE); seniority_level = "entry".
  role_type_score = 15 (full-stack SWE); role_type = "fullstack-swe".
  sponsorship_bonus_score = 15 (explicit sponsorship). Total 96.

Example C — "Machine Learning Engineer (3-5 years). Required: Python, PyTorch, MLOps/model deployment, feature pipelines, Spark. OPT/CPT candidates welcome to apply."
  tech_score: required (5): Python met, PyTorch met, MLOps no, feature pipelines no, Spark no → base = round(2/5 × 40) = 16; no preferred list → +0; primary Python in stack → no cap. tech_score = 16.
  seniority_score = 20 (Mid, 2-5 YOE); seniority_level = "mid".
  role_type_score = 8 (classical ML engineering — NOT the 15-point AI/LLM bucket); role_type = "ml-engineer".
  sponsorship_bonus_score = 11 (OPT/CPT mentioned, no explicit sponsorship promise). Total 55.

SCORING INSTRUCTIONS:
Score each dimension independently using only the exact values specified in each field's description.
seniority_score and role_type_score describe the JOB (its level and role type), never how well the candidate fits it — the CANDIDATE CONTEXT above exists for tech matching and sponsorship, not for discounting those two scores. Each label must carry its rubric value (e.g. seniority_level "mid" is always seniority_score 20).
Do NOT round to multiples of 5 or 10. Do NOT average sub-scores. Output all 10 fields."""


_CORRECTION_TEMPLATE = (
    "Your previous scoring of this job description was rejected before use: {problem}. "
    "seniority_score and role_type_score are fixed by the label you choose and describe "
    "the JOB, not the candidate's fit — seniority: entry/junior=25, mid=20, unspecified=8, "
    "senior=5, lead/staff/principal/executive=0; role type: full-stack/backend/frontend "
    "SWE or AI/LLM engineer=15, generic software engineer=13, mobile=10, data or classical "
    "ML=8, devops/SRE/platform=7, QA/SDET=5, other=3. Do not discount either score because "
    "the candidate looks under- or over-qualified. Re-score the same job description from "
    "scratch, all 10 fields, with every label and its score consistent."
)


def _build_messages(provider: str, static_prompt: str, jd_prompt: str,
                    correction: str | None = None) -> list:
    """
    System (static, cacheable) + human (the JD) messages for one scoring
    call. The Anthropic cache_control marker is a content-block attribute,
    so for Anthropic the system content is a one-block list carrying it;
    Groq/Ollama get the same text as a plain string — they'd reject or
    ignore the unknown key, and their APIs have no equivalent.

    `correction` (the text of a _SubScoreInconsistent / _SubScoreOutOfRange
    the same provider just raised) appends one more human turn pointing the
    contradiction out and restating the label -> score table. The system
    prefix is untouched, so the Anthropic cache still hits.
    """
    if provider == "anthropic":
        system_content = [{"type": "text", "text": static_prompt,
                           "cache_control": {"type": "ephemeral"}}]
    else:
        system_content = static_prompt
    messages = [SystemMessage(content=system_content), HumanMessage(content=jd_prompt)]
    if correction:
        messages.append(HumanMessage(content=_CORRECTION_TEMPLATE.format(problem=correction)))
    return messages


def _llm_score(jd: str, max_retries: int = 3) -> dict:
    provider = cheap_provider()
    llm = get_chat_model("cheap", temperature=0.0, max_tokens=1024, provider_override=provider)
    structured = llm.with_structured_output(_SubScores)

    static_prompt = _static_prompt()
    jd_prompt = f"JOB DESCRIPTION:\n{jd[:3000]}"
    messages = _build_messages(provider, static_prompt, jd_prompt)

    last_exc: Optional[Exception] = None
    # A plain `for attempt in range(max_retries)` has a boundary bug: if a
    # hop-worthy failure (quota exhausted / out-of-range scores) lands on
    # the LAST iteration, the code below still reassigns provider/llm to the
    # fallback and calls `continue` — but there's no next iteration left, so
    # the loop just ends and raises the stale error from the provider that
    # was about to be abandoned, without ever trying the fallback. Real
    # failure this reproduced: attempts 1-2 failed on Groq tool-call schema
    # errors (burning retries), attempt 3 hit Groq's real TPD quota limit —
    # correctly detected as hop-worthy, but with no attempts left the hop to
    # Anthropic never actually happened. A manual counter that only
    # increments on a NON-hop failure fixes this: a hop always gets a fresh
    # attempt on the new provider, and only genuine same-provider retries
    # count against max_retries.
    #
    # Two more rules, both from a live failure on Sep 18 2026 (Google
    # "Software Engineer III, Full Stack"): Haiku labeled the JD "mid" but
    # scored seniority 5, reasoning about the CANDIDATE's US experience
    # instead of the job's level -> _SubScoreInconsistent -> hop to Groq ->
    # Groq's model emitted truncated tool-call JSON (tool_use_failed) ->
    # Ollama not running -> no fallback -> the old code fell through to
    # plain retries of Groq, the provider it had just declared a dead end,
    # three times, and the job ended up unscored.
    #   1. An inconsistent/out-of-range answer gets ONE corrective re-ask on
    #      the SAME provider first (the contradiction is fed back verbatim,
    #      system prefix unchanged so the cache still hits, ~$0.003). On the
    #      primary provider that's a far better bet than hopping to a
    #      fallback that is unreliable by design.
    #   2. When a hop-worthy failure hits and the chain has nothing left,
    #      raise immediately, naming every provider's failure. Retrying a
    #      provider already judged a dead end just burns time and quota.
    corrected: set = set()   # providers that already had their corrective re-ask
    failures: list = []      # "provider: error" per provider given up on
    attempt = 0
    while attempt < max_retries:
        try:
            result = structured.invoke(messages)
            tech  = _coerce_subscore(result.tech_score, 45, "tech_score")
            sen   = _coerce_subscore(result.seniority_score, 25, "seniority_score")
            role  = _coerce_subscore(result.role_type_score, 15, "role_type_score")
            spons = _coerce_subscore(result.sponsorship_bonus_score, 15, "sponsorship_bonus_score")
            _check_label_consistency(result.role_type, role, _ROLE_TYPE_OK_SCORES, "role_type")
            _check_label_consistency(result.seniority_level, sen, _SENIORITY_OK_SCORES, "seniority_level")
            return {
                "rubric_version": RUBRIC_VERSION,
                "score": tech + sen + role + spons,
                "reasoning": result.reasoning,
                "jd_summary": result.jd_summary,
                "seniority_level": result.seniority_level,
                "role_type": result.role_type,
                "sub_scores": {"tech": tech, "seniority": sen,
                               "role_type": role, "sponsorship_bonus": spons},
                "tailoring_recommended": bool(result.tailoring_recommended),
                "tailoring_reasoning": result.tailoring_reasoning,
            }
        except Exception as exc:
            last_exc = exc
            bad_scores = isinstance(exc, (_SubScoreOutOfRange, _SubScoreInconsistent))
            # Rule 1: one corrective re-ask on the same provider, with the
            # contradiction spelled out. Free of attempt budget.
            if bad_scores and provider not in corrected:
                corrected.add(provider)
                print(f"  [should_apply] {provider} returned inconsistent scores ({exc}) — re-asking once with the contradiction pointed out")
                messages = _build_messages(provider, static_prompt, jd_prompt, correction=str(exc))
                continue
            # A hard quota exhaustion (e.g. Groq's daily token cap) or a
            # model that can't emit a valid tool call won't clear in seconds
            # — retrying the SAME provider is pointless, so hop to the next
            # one in the chain instead of waiting/retrying. A second
            # inconsistent answer after the corrective re-ask is treated the
            # same way: that model isn't respecting the rubric for this JD.
            if is_quota_exhausted(exc) or bad_scores:
                reason = "quota exhausted / unusable tool calls" if is_quota_exhausted(exc) else "inconsistent scores"
                failures.append(f"{provider}: {reason} — {exc}")
                fallback = next_cheap_provider(provider)
                if fallback:
                    print(f"  [should_apply] {provider} {reason} — falling back to {fallback}")
                    provider = fallback
                    llm = get_chat_model("cheap", temperature=0.0, max_tokens=1024, provider_override=provider)
                    structured = llm.with_structured_output(_SubScores)
                    messages = _build_messages(provider, static_prompt, jd_prompt)  # cache_control is Anthropic-only
                    continue  # hop doesn't consume attempt budget — see comment above the loop
                # Rule 2: nothing left in the chain — stop here rather than
                # retrying a provider already judged a dead end.
                print(f"  [should_apply] {provider} {reason} and no further cheap-tier provider is available — giving up")
                break
            is_rate_limit = "429" in str(exc) or "rate" in str(exc).lower()
            attempt += 1
            if attempt < max_retries:
                wait = 20 * (2 ** attempt) if is_rate_limit else 2 ** attempt
                print(f"  [should_apply] Attempt {attempt} failed ({type(exc).__name__}). Retrying in {wait}s...")
                time.sleep(wait)
    detail = "; ".join(failures) if failures else str(last_exc)
    raise RuntimeError(f"should_apply LLM scoring failed ({detail})")


# External sponsorship signal (scraper/sponsorship.py's SerpApi H-1B lookup)
# applied as a deterministic Python-side adjustment on top of the LLM's own
# rubric score, same "cheap Python repair beats relying on the LLM" pattern
# used throughout this pipeline (bullet top-up, project-bullet merging).
# "no_sponsorship" is treated as a hard red flag — a confirmed external record
# of the company not sponsoring is a stronger signal than the JD simply not
# mentioning sponsorship. "confirmed_h1b"/"likely" lift a JD that is silent
# on sponsorship (7) toward the explicit-sponsor value (15): a company with
# a real H-1B filing record is nearly as good as one that says so in the JD.
_SPONSORSHIP_SIGNAL_BONUS = {"confirmed_h1b": 6, "likely": 3}


def evaluate(jd: str, company: str = "", title: str = "", use_cache: bool = True,
             sponsorship_signal: str = None) -> dict:
    """
    Full should-apply evaluation. Returns:
      {
        "score": int, "proceed": bool, "stage": "red_flag"|"cache"|"llm",
        "red_flags": [..], "reasoning": str, "jd_summary": str,
        "seniority_level": str, "role_type": str, "sub_scores": {..},
        "tailoring_recommended": bool, "tailoring_reasoning": str,
      }
    tailoring_recommended/tailoring_reasoning answer a DIFFERENT question
    than score/proceed — see _SubScores.tailoring_recommended's docstring.
    Verdicts are cached permanently by JD content hash.

    sponsorship_signal: optional result of scraper/sponsorship.py's company
    lookup ("confirmed_h1b" | "likely" | "neutral" | "no_sponsorship").
    Callers without this signal (manual mode, the web UI) simply omit it —
    behavior is identical to before this parameter existed.
    """
    # Stage 0 — external sponsorship signal override (free, no LLM)
    if sponsorship_signal == "no_sponsorship":
        verdict = {
            "rubric_version": RUBRIC_VERSION,
            "score": 0, "proceed": False, "stage": "red_flag",
            "red_flags": [f"External sponsorship lookup found no-sponsorship signal for {company or 'this company'}"],
            "reasoning": "Company sponsorship history lookup (SerpApi H-1B search) found explicit no-sponsorship signals.",
            "jd_summary": "", "seniority_level": "", "role_type": "",
            "sub_scores": {},
            "tailoring_recommended": False,
            "tailoring_reasoning": "Not applying to this posting — tailoring is moot.",
        }
        if use_cache:
            jd_cache.update_entry(jd, company=company, title=title, should_apply=verdict)
        return verdict

    # Stage 1 — deterministic red flags (free)
    flags = find_red_flags(jd)
    if flags:
        verdict = {
            "rubric_version": RUBRIC_VERSION,
            "score": 0, "proceed": False, "stage": "red_flag",
            "red_flags": flags,
            "reasoning": f"Hard red flag in JD: \"{flags[0]}\"",
            "jd_summary": "", "seniority_level": "", "role_type": "",
            "sub_scores": {},
            "tailoring_recommended": False,
            "tailoring_reasoning": "Not applying to this posting — tailoring is moot.",
        }
        if use_cache:
            jd_cache.update_entry(jd, company=company, title=title, should_apply=verdict)
        return verdict

    # Stage 2 — cache (free). A hit is only trusted if it was scored under the
    # CURRENT rubric and passes the same label/score cross-check fresh LLM
    # output must pass. Before this, a bad verdict written once was served
    # forever — "Retry" re-read the poisoned cache and nothing ever
    # re-validated it. Red-flag verdicts have no sub_scores to check and
    # don't depend on the rubric, so they stay trusted as-is.
    if use_cache:
        entry = jd_cache.get_entry(jd)
        cached_verdict = (entry or {}).get("should_apply") or {}
        stage = cached_verdict.get("stage")
        if stage == "red_flag" or (stage == "llm" and _verdict_is_consistent(cached_verdict)):
            cached = dict(cached_verdict)
            cached["stage"] = "cache"
            if stage == "llm":
                # proceed is a function of the threshold in force NOW, not
                # the one that happened to be set when the verdict was cached.
                cached["proceed"] = int(cached.get("score") or 0) >= min_score()
            return cached
        if stage == "llm":
            print("  [should_apply] cached verdict is stale (old rubric) or inconsistent — re-scoring")

    # Stage 3 — LLM rubric on the cheap tier
    result = _llm_score(jd)

    bonus = _SPONSORSHIP_SIGNAL_BONUS.get(sponsorship_signal, 0)
    if bonus:
        sub = result["sub_scores"]
        old_spons = sub["sponsorship_bonus"]
        new_spons = min(15, old_spons + bonus)
        added = new_spons - old_spons
        if added:
            sub["sponsorship_bonus"] = new_spons
            result["score"] = min(100, result["score"] + added)
            result["reasoning"] += f" (+{added} from confirmed external sponsorship signal: {sponsorship_signal})"

    verdict = {
        **result,
        "proceed": result["score"] >= min_score(),
        "stage": "llm",
        "red_flags": [],
    }
    if use_cache:
        jd_cache.update_entry(jd, company=company, title=title, should_apply=verdict)
    return verdict
