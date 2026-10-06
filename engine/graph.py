import os
import json
import re
import sys
import copy
import time
import datetime
import math
from dotenv import load_dotenv
from pydantic import ValidationError
from langchain_core.exceptions import OutputParserException
from langgraph.graph import StateGraph, END

from schemas import TriageKeywords, StrategistSelection, SelectedBullet, WriterOutput, TailoredProjectBullet, ResumeStrategy
from state import ResumeGraphState
from pdf_generator import generate_pdfs, sanitize_filename_component

load_dotenv()

# Add repo root to path so core/ is importable when graph.py is run from engine/
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from core.profile_cache import load_profile_cache
from core.llm import get_chat_model, describe as describe_llms
from core import jd_cache
from core import should_apply as should_apply_engine

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("ERROR: ANTHROPIC_API_KEY is missing. Add it to your .env file.")
    sys.exit(1)

print(f"[graph] {describe_llms()}")

# Cheap tier (Groq free tier / local Ollama / Haiku fallback) — everything
# except final prose. Instantiated per-role so token budgets stay explicit.
llm_fast       = get_chat_model("cheap", temperature=0.2, max_tokens=1024)   # keyword extraction
llm_strategist = get_chat_model("cheap", temperature=0.2, max_tokens=3072)   # selection + reporting
llm_editor     = get_chat_model("cheap", temperature=0.0, max_tokens=4096)   # surgical QA fixes

# Quality tier — writer only (prose the recruiter actually reads).
# max_tokens is a ceiling, not a cost — you only pay for tokens actually
# generated — so it's set with real headroom: a real failure was traced to
# this call hitting 8192 mid-generation and getting cut off before
# tailored_bullets was written at all (resume + cover letter in one
# response, at temperature=0, can legitimately run long).
llm_quality = get_chat_model("quality", temperature=0.0, max_tokens=16384)

# Where finalizer_node writes tailored resume/CL output folders. Deliberately
# separate from core/*.py's OUTPUT_BASE_PATH (JD cache, seen-jobs, entries.json
# state) — this is user-facing final output, not internal pipeline state, so
# it gets its own env var rather than mixing the two into one folder.
RESUME_OUTPUT_PATH = os.getenv(
    "RESUME_OUTPUT_PATH", os.path.join(os.path.expanduser("~"), "Documents", "Resumes")
)


def education_status_label(edu: dict) -> str:
    """
    Returns 'Completed' if graduation_status is set to 'completed' in master_data,
    or if the education end date has passed. Returns 'Completing' otherwise.
    """
    if edu.get("graduation_status") == "completed":
        return "Completed"
    try:
        end_str = edu.get("duration", "").split(" - ")[-1].strip()
        if end_str.lower() in ("present", "current"):
            return "Completing"
        end = datetime.datetime.strptime(end_str, "%b %Y")
        return "Completed" if datetime.datetime.now() >= end else "Completing"
    except Exception:
        return "Completed"


def compute_years_label(duration_str: str) -> str:
    """
    Converts 'Nov 2020 - Jul 2024' into '4+' for use in resume summaries.
    Rounds up at 0.5 years so 3y8m → '4+', not '3+'.
    """
    try:
        parts = duration_str.split(" - ")
        if len(parts) != 2:
            return ""
        start = datetime.datetime.strptime(parts[0].strip(), "%b %Y")
        end_str = parts[1].strip()
        end = datetime.datetime.now() if end_str.lower() in ("present", "current") else datetime.datetime.strptime(end_str, "%b %Y")
        years = (end - start).days / 365.25
        rounded = math.floor(years) + (1 if (years % 1) >= 0.5 else 0)
        return f"{rounded}+"
    except Exception:
        return ""


def invoke_with_retry(structured_llm, prompt, max_retries: int = 3, *, cheap_rebuild: dict = None):
    """
    Wraps a structured LLM call with retries for:
    - HTTP 429 / RESOURCE_EXHAUSTED: exponential backoff (quota reset)
    - OutputParserException: immediate retry (mid-JSON truncation from hitting max_tokens)
    - pydantic.ValidationError: immediate retry. LangChain's PydanticToolsParser
      raises this as a RAW pydantic error (not wrapped in OutputParserException)
      when the model's tool-call JSON is syntactically valid but missing a
      required field — its own source notes the #1 cause is the response
      hitting max_tokens mid-generation and getting cut off before that field
      was written. Real example this was written for: a writer call whose
      output had tailored_summary but no tailored_bullets at all.
    - Hard quota exhaustion (e.g. Groq's daily token cap): falls back to the
      NEXT cheap-tier provider instead of backing off and retrying the same
      exhausted one — a 429 that says "try again in 27 minutes" won't clear
      within this function's retry budget no matter how long we wait.

    cheap_rebuild: {"temperature": float, "max_tokens": int, "schema": BaseModel}
    — pass this for CHEAP-TIER callers only (triage/strategist/editor). The
    quality-tier writer never passes it: it's always Anthropic, which Groq's
    quota can't affect, so there's nothing to fall back from.
    """
    from core.llm import cheap_provider as _cheap_provider, next_cheap_provider, get_chat_model, is_quota_exhausted

    current_llm = structured_llm
    current_provider = _cheap_provider() if cheap_rebuild else None

    for attempt in range(max_retries):
        try:
            return current_llm.invoke(prompt)
        except (OutputParserException, ValidationError) as e:
            if attempt < max_retries - 1:
                print(f"  [PARSE ERROR] Model output didn't match the schema ({type(e).__name__}, "
                      f"likely truncated by max_tokens). Retrying ({attempt + 1}/{max_retries})...")
                time.sleep(2)
            else:
                raise
        except Exception as e:
            if cheap_rebuild and is_quota_exhausted(e):
                fallback = next_cheap_provider(current_provider)
                if fallback:
                    print(f"  [LLM] {current_provider} quota exhausted — falling back to {fallback}")
                    current_provider = fallback
                    base = get_chat_model("cheap", temperature=cheap_rebuild["temperature"],
                                           max_tokens=cheap_rebuild["max_tokens"],
                                           provider_override=fallback)
                    current_llm = base.with_structured_output(cheap_rebuild["schema"])
                    continue
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = 30 * (2 ** attempt)  # 30s, 60s, 120s
                print(f"  [RATE LIMIT] Quota hit. Waiting {wait}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"LLM call failed after {max_retries} retries.")


# ---------------------------------------------------------------------------
# PURE PYTHON HELPERS (no LLM calls)
# ---------------------------------------------------------------------------

def _build_master_corpus(master_data: dict) -> str:
    """Flatten all searchable text from master data into one lowercased string."""
    parts = []
    for cat, skills in master_data.get("technical_skills", {}).items():
        parts.extend(s.lower() for s in skills)
    for exp in master_data.get("experience", []):
        for bullet in exp.get("bullets", []):
            parts.append(bullet["text"].lower())
            parts.extend(t.lower() for t in bullet.get("tags", []))
    for proj in master_data.get("projects", []):
        parts.extend(t.lower() for t in proj.get("tech_stack", []))
        for bullet in proj.get("bullets", []):
            parts.append(bullet["text"].lower())
            parts.extend(t.lower() for t in bullet.get("tags", []))
    return " ".join(parts)


def _validate_keywords(keywords: list, master_data: dict) -> tuple:
    """
    Split LLM-returned keywords into verified (present in master data) and
    gap (absent) lists. Returns (verified_keywords, gap_keywords).

    Matching is whole-phrase with word boundaries, plural-tolerant per word:
    "React" matches "react.js", "RESTful APIs" matches "restful api",
    "web form" matches "web-form". It is NOT substring matching and there is
    NO per-word fallback for multi-word keywords — both were real leaks (Sep
    2026): "ITL" verified via the inside of "tITLe", and "Claude Code"
    verified because "claude" (Claude Sonnet) and "code" (code reviews) each
    appeared somewhere, after which the writer put "including Claude Code"
    into a summary as a thing the candidate had worked with. A verified
    keyword is a claim the resume is allowed to make; it has to exist as a
    phrase in the candidate's own data.
    """
    corpus = _build_master_corpus(master_data)

    def _phrase_pattern(kw: str):
        words = [re.escape(w.rstrip("s")) + r"s?" for w in kw.lower().split() if w]
        if not words:
            return None
        return re.compile(r"(?<![a-z0-9])" + r"[\s\-/]+".join(words) + r"(?![a-z0-9])")

    verified, gaps = [], []
    for kw in keywords:
        pat = _phrase_pattern(kw)
        (verified if pat and pat.search(corpus) else gaps).append(kw)
    return verified, gaps


# Metric pattern: digits followed by %, K, k, x, X, or +
_METRIC_RE = re.compile(r'\d+(?:\.\d+)?[\+%KkxX]')

BANNED_PHRASES = [
    "spearheaded", "leveraged", "synergized", "passionate about", "results-driven",
    "detail-oriented", "thought leader", "dynamic", "robust", "cutting-edge",
    "game-changing", "best-in-class", "world-class",
]
WEAK_VERBS = [
    "helped", "assisted", "worked on", "was responsible for",
    "participated in", "supported", "contributed to",
]


def merge_project_bullets(raw: list) -> list:
    """
    Reconciles TailoredProjectBullet entries after schemas.py relaxed
    bullet_1/bullet_2 to Optional (see that file's comment for why — Groq
    has been observed splitting one project's two bullets into two separate
    array entries instead of one object with both fields).

    Groups entries by project_name and takes the first non-None bullet_1 and
    first non-None bullet_2 across every entry sharing that name — so a
    correctly-formed single entry (the normal case, always true for the
    Anthropic writer) passes through unchanged, and a Groq-style split
    reassembles into one clean entry.

    Sep 2026: the preferred field is `bullets` (every bullet of the project,
    rewritten, same count/order as the original) — that passes through as-is.
    bullet_1/bullet_2 remain as the legacy two-bullet form.

    A project with neither a non-empty `bullets` list nor both legacy fields
    after merging is DROPPED from the returned list, not passed through with
    None — downstream (finalizer_node's proj_bullet_map), a project not present
    in this list simply keeps its original, untailored master-data bullets
    instead of shipping broken/missing text into a PDF.
    """
    merged: dict = {}
    for item in raw or []:
        d = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        name = d.get("project_name")
        if not name:
            continue
        slot = merged.setdefault(name, {"project_name": name, "bullet_1": None, "bullet_2": None, "bullets": None})
        if not slot["bullets"] and d.get("bullets"):
            slot["bullets"] = [t for t in d["bullets"] if t]
        if slot["bullet_1"] is None and d.get("bullet_1"):
            slot["bullet_1"] = d["bullet_1"]
        if slot["bullet_2"] is None and d.get("bullet_2"):
            slot["bullet_2"] = d["bullet_2"]
    return [v for v in merged.values() if v["bullets"] or (v["bullet_1"] and v["bullet_2"])]


def top_up_bullets_by_company(rewritten: list, target_selection: list, fallback_key: str) -> list:
    """
    Ensures `rewritten` has at least as many bullets per company as
    `target_selection` does, topping up any shortfall using target_selection's
    own text for that company.

    Used at TWO points in this pipeline, both guarding against the same real
    failure mode: an LLM rewrite pass silently dropping a bullet for an
    employer even though the correct count was selected upstream —
    - writer_node: target_selection = the strategist's selected bullets,
      fallback_key="text" (falls back to the ORIGINAL, unrewritten bullet).
    - editor_node: target_selection = the writer's already-complete draft,
      fallback_key="rewritten_text" (falls back to the WRITER's prose, which
      is better quality than the raw original since it's already tailored).

    rewritten:        [{"company_name": ..., "rewritten_text": ...}, ...]
    target_selection:  [{"company_name": ..., <fallback_key>: ...}, ...]

    Returns a NEW list (does not mutate `rewritten`).
    """
    result = list(rewritten)
    current_by_company: dict = {}
    for b in result:
        current_by_company.setdefault(b["company_name"], []).append(b)

    target_by_company: dict = {}
    for b in target_selection:
        target_by_company.setdefault(b["company_name"], []).append(b)

    for company, targets in target_by_company.items():
        current_count = len(current_by_company.get(company, []))
        shortfall = len(targets) - current_count
        if shortfall <= 0:
            continue
        for t in targets[current_count:current_count + shortfall]:
            result.append({"company_name": company, "rewritten_text": t[fallback_key]})
        print(f"  [top-up] {company}: +{shortfall} bullet(s) restored to match target count "
              f"({len(targets)})")
    return result


_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "across", "using", "while",
    "within", "through", "over", "under", "onto", "than", "then", "were", "was", "are", "has",
    "had", "have", "its", "their", "our", "your", "all", "any", "each", "per", "via", "not",
    "but", "also", "both", "more", "most", "new", "one", "two", "three", "full", "end",
}


# Every number is a fact to protect in a rewrite — not just suffixed metrics
# (40%, 10K+) but bare ones too: "219-test suite", "6-node pipeline",
# "4 engineers", "3-month engagement". _METRIC_RE (suffixed only) stays as the
# invented-metric detector's definition; this broader one is for fidelity.
_NUMBER_RE = re.compile(r'\d+(?:\.\d+)?[\+%KkxX]?')


def _numbers(text: str) -> set:
    return {m.lower() for m in _NUMBER_RE.findall(text or "")}


def _content_tokens(text: str) -> set:
    """Lowercased words (3+ letters, non-stopword) plus number tokens — the
    'facts' of a bullet, used to measure how much of a source survived a rewrite."""
    words = {w for w in re.findall(r"[a-z][a-z+#.]{2,}", (text or "").lower()) if w not in _STOPWORDS}
    return words | _numbers(text)


def enforce_bullet_traceability(rewritten: list, source_selection: list, min_overlap: float = 0.40) -> tuple:
    """
    Deterministic floor on rewrite quality (Sep 2026). A real run produced
    experience bullets like "Defined technical requirements and led
    implementation planning… coordinating directly with stakeholders" in place
    of a source that said Figma, WCAG, SEO, Google Analytics — generic filler
    with the specifics gone, plus an invented technology, and the draft still
    scored 95/100 because no regex flags "vague". Prompt rules alone can't
    guarantee this, so: every rewritten bullet is matched (greedily, per
    employer) to the source bullet it shares the most content with. If the
    best match keeps less than `min_overlap` of the source's content tokens,
    OR drops any number the source has, the rewritten text is REPLACED with
    the source text. Worst case, the resume prints the master-data bullet —
    the same text the base resume uses, which reads fine. Tailoring is a
    bonus; fidelity is the floor.

    rewritten:        [{"company_name", "rewritten_text"}, ...]
    source_selection: [{"company_name", "original_text" | "text"}, ...]
    Returns (new_list, reverts) — reverts is a list of (company, reason).
    """
    by_company: dict = {}
    for s in source_selection or []:
        text = s.get("original_text") or s.get("text") or ""
        if text:
            by_company.setdefault(s.get("company_name") or s.get("company"), []).append(text)

    rewritten = list(rewritten or [])
    # Global assignment per employer: pair rewrites to sources by descending
    # overlap across ALL pairs, not in list order — so a good rewrite of source
    # A can't lose its match just because a weaker bullet came earlier in the
    # list and happened to "claim" A first.
    assignment: dict = {}      # rewrite index -> (source text, overlap)
    for company, sources in by_company.items():
        idxs = [i for i, b in enumerate(rewritten) if b.get("company_name") == company]
        pairs = []
        for i in idxs:
            new_tokens = _content_tokens(rewritten[i].get("rewritten_text") or "")
            for j, s in enumerate(sources):
                src_tokens = _content_tokens(s)
                pairs.append((len(new_tokens & src_tokens) / max(1, len(src_tokens)), i, j))
        pairs.sort(reverse=True)
        taken_i, taken_j = set(), set()
        for overlap, i, j in pairs:
            if i in taken_i or j in taken_j:
                continue
            taken_i.add(i); taken_j.add(j)
            assignment[i] = (sources[j], overlap)

    result, reverts = [], []
    for i, b in enumerate(rewritten):
        if i not in assignment:          # unknown employer, or more rewrites than sources
            result.append(b)
            continue
        best_src, best_overlap = assignment[i]
        new_text = b.get("rewritten_text") or ""
        src_metrics = _numbers(best_src)
        new_metrics = _numbers(new_text)
        reason = None
        if best_overlap < min_overlap:
            reason = f"kept only {best_overlap:.0%} of the source's content"
        elif src_metrics - new_metrics:
            reason = f"dropped the source's number(s) {sorted(src_metrics - new_metrics)}"
        if reason:
            reverts.append((b.get("company_name"), reason))
            result.append({**b, "rewritten_text": best_src})
        else:
            result.append(b)
    return result, reverts


_FILLER_TAIL_RE = re.compile(
    r",\s*(?:demonstrating|showcasing|highlighting|reflecting|underscoring|exemplifying|"
    r"bringing|building the discipline for|positioning|reinforcing|illustrating|embodying|"
    r"grounding|signaling|cementing|solidifying|evidencing)"
    r"\b[^.]*\.\s*$",
    re.IGNORECASE,
)


def strip_filler_tail(text: str) -> str:
    """
    Removes the trailing participial clause that LLMs bolt onto a finished
    bullet to echo JD keywords — ", demonstrating hands-on proficiency with
    agentic systems and performance optimization." The bullet before the comma
    already states the outcome; the tail adds no fact and is one of the most
    recognizable generated-resume patterns. Only fires on a sentence-final
    clause opened by one of the listed verbs, so a real clause like
    ", reducing setup time by 35%." is untouched.
    """
    if not text:
        return text
    stripped = _FILLER_TAIL_RE.sub(".", text.rstrip())
    return stripped if stripped != text.rstrip() else text


def enforce_project_bullet_traceability(tailored_projects: list, master_projects: list, min_overlap: float = 0.40) -> tuple:
    """
    Same fidelity floor as enforce_bullet_traceability(), for project bullets:
    each rewritten bullet in a project's `bullets` list is matched by index to
    the source bullet (count and order are mandated to be identical), and is
    reverted to the source text if it kept too little of the source's content
    or dropped one of its numbers. Legacy bullet_1/bullet_2 entries are left
    alone (they're reconciled by count in the finalizer anyway).
    Returns (new_list, reverts) — reverts is a list of (project, reason).
    """
    src_by_name = {p["name"]: [b.get("text", "") if isinstance(b, dict) else str(b) for b in p.get("bullets", [])]
                   for p in master_projects or []}
    result, reverts = [], []
    for entry in tailored_projects or []:
        d = dict(entry)
        name = d.get("project_name", "")
        sources = src_by_name.get(name) or next(
            (v for k, v in src_by_name.items() if name.lower() in k.lower() or k.lower() in name.lower()), None)
        blist = d.get("bullets")
        if not sources or not blist:
            result.append(d)
            continue
        fixed = []
        for i, new_text in enumerate(blist):
            if i >= len(sources) or not new_text:
                fixed.append(new_text)
                continue
            src = sources[i]
            src_tokens = _content_tokens(src)
            overlap = len(_content_tokens(new_text) & src_tokens) / max(1, len(src_tokens))
            src_metrics = _numbers(src)
            new_metrics = _numbers(new_text)
            reason = None
            if overlap < min_overlap:
                reason = f"bullet {i+1} kept only {overlap:.0%} of the source's content"
            elif src_metrics - new_metrics:
                reason = f"bullet {i+1} dropped the source's number(s) {sorted(src_metrics - new_metrics)}"
            if reason:
                reverts.append((name, reason))
                fixed.append(src)
            else:
                fixed.append(new_text)
        d["bullets"] = fixed
        result.append(d)
    return result, reverts


def reconcile_editor_output(draft_data: dict, edited: dict, pre_report: dict, post_report: dict) -> tuple:
    """
    Post-editor gate (Sep 2026). The editor LLM's output used to be trusted
    blindly — every truthfulness check ran on the WRITER's draft only, so the
    editor's own rewrite pass could (and on a real run did) introduce a
    near-duplicate bullet with invented specifics, a "[METRIC NEEDED]" tag the
    draft never had, and JD-echo tails, with nothing downstream to catch it.

    Rule: the editor may only make each section cleaner, never worse. Section
    by section, compare the validator's verdict on the edited output against
    its verdict on the draft; where the edit is worse, keep the draft's text
    for that section (the draft is already count-correct and JD-tailored —
    it just lacks this polish pass).

    Returns (final_data_fields: dict, reasons: list[str]) — reasons are the
    sections that fell back, for logging.
    """
    def _flags(report, is_project):
        return [f for f in report.get("flagged_bullets", [])
                if str(f.get("company", "")).startswith("[project]") == is_project]

    def _metric_needed_count(bullets):
        return sum(1 for b in bullets or [] if "[METRIC NEEDED]" in (b.get("rewritten_text") or ""))

    reasons = []
    out = dict(edited)

    # Experience bullets
    exp_worse = (
        len(_flags(post_report, False)) > len(_flags(pre_report, False))
        or _metric_needed_count(edited.get("tailored_bullets")) > _metric_needed_count(draft_data.get("tailored_bullets"))
    )
    if exp_worse:
        out["tailored_bullets"] = draft_data.get("tailored_bullets", [])
        reasons.append("experience bullets (editor introduced new invented content or [METRIC NEEDED] tags)")

    # Project bullets
    if len(_flags(post_report, True)) > len(_flags(pre_report, True)):
        out["tailored_project_bullets"] = draft_data.get("tailored_project_bullets")
        reasons.append("project bullets (editor introduced new invented content)")

    # Summary
    if len(post_report.get("summary_issues", [])) > len(pre_report.get("summary_issues", [])):
        out["tailored_summary"] = draft_data.get("tailored_summary", edited.get("tailored_summary"))
        reasons.append("summary (editor's version has more validation issues than the draft)")

    return out, reasons


def validate_writer_output(writer_output: dict, master_data: dict) -> dict:
    """
    Pre-editor Python validation — no LLM call.
    Checks experience bullets AND project bullets for: invented metrics,
    banned phrases, weak opening verbs. Returns a ValidationReport dict.
    """
    # Build per-employer metric sets from master data
    employer_metrics: dict = {}
    for exp in master_data.get("experience", []):
        company = exp["company"]
        metrics = set()
        for bullet in exp.get("bullets", []):
            for m in _METRIC_RE.findall(bullet["text"]):
                metrics.add(m.lower())
        employer_metrics[company] = metrics

    # Build per-project metric sets from master data
    project_metrics: dict = {}
    for proj in master_data.get("projects", []):
        name = proj["name"]
        metrics = set()
        for bullet in proj.get("bullets", []):
            for m in _METRIC_RE.findall(bullet["text"]):
                metrics.add(m.lower())
        project_metrics[name] = metrics

    flagged = []
    passed = 0
    total = 0

    for b in writer_output.get("tailored_bullets", []):
        total += 1
        text = b.get("rewritten_text", "")
        company = b.get("company_name", "")
        lower = text.lower()
        issues = []

        # 1. Check every metric in the rewritten bullet exists in master data for that company
        for m in _METRIC_RE.findall(text):
            known = employer_metrics.get(company, set())
            if m.lower() not in known:
                issues.append(f"metric not in master data: '{m}'")

        # 2. Banned phrases
        for phrase in BANNED_PHRASES:
            if phrase in lower:
                issues.append(f"banned phrase: '{phrase}'")

        # 3. Weak opening verb
        for verb in WEAK_VERBS:
            if lower.startswith(verb):
                issues.append(f"weak opening verb: '{verb}'")

        if issues:
            flagged.append({"bullet_text": text, "company": company, "issues": issues})
        else:
            passed += 1

    # Check project bullets for invented metrics and banned phrases
    for pb in writer_output.get("tailored_project_bullets", []) or []:
        project_name = pb.get("project_name", "")
        known_metrics = project_metrics.get(project_name, set())
        # Also try fuzzy project name match
        if not known_metrics:
            for pname, pmet in project_metrics.items():
                if project_name.lower() in pname.lower() or pname.lower() in project_name.lower():
                    known_metrics = pmet
                    break

        # Preferred `bullets` list (every bullet of the project) — fall back to
        # the legacy bullet_1/bullet_2 pair when the list is absent.
        bullet_texts = [t for t in (pb.get("bullets") or []) if t] or [
            pb.get(f) or "" for f in ("bullet_1", "bullet_2")
        ]
        for text in bullet_texts:
            total += 1
            lower = text.lower()
            issues = []

            for m in _METRIC_RE.findall(text):
                if m.lower() not in known_metrics:
                    issues.append(f"metric not in master data: '{m}'")
            for phrase in BANNED_PHRASES:
                if phrase in lower:
                    issues.append(f"banned phrase: '{phrase}'")

            if issues:
                flagged.append({
                    "bullet_text": text,
                    "company": f"[project] {project_name}",
                    "issues": issues,
                })
            else:
                passed += 1

    # Summary checks
    summary = writer_output.get("tailored_summary", "")
    summary_lower = summary.lower()
    summary_issues = []
    # NOTE: no OPT/availability requirement here by design — removed at the
    # user's explicit request. The header contact line already dropped this
    # earlier in this project's history, and now the summary no longer
    # requires it either — visa status appears nowhere on the resume body.
    # The cover letter's closing line (a separate, untouched rule) still
    # mentions availability, since that's standard cover-letter practice.
    # The summary brief (writer_node's RESUME INSTRUCTIONS) requires at least
    # one real, verifiable number from the candidate's experience bullets. The
    # writer has been observed dropping this entirely — a summary with zero
    # evidence even though real metrics (10K+ users, 40%, 35%, 30+ endpoints)
    # are printed in the same resume's bullets. Summary-level counterpart to
    # detect_dropped_metrics() for bullets.
    #
    # Sep 2026: no longer tied to an "At {company}, ..." sentence shape — the
    # summary is written as a person, not a template, so the check is: after
    # removing the "N+ years" figure (a metric-shaped token that would make a
    # whole-summary check trivially pass), does ANY remaining number in the
    # summary actually exist in some master-data experience bullet?
    _known_employer_metrics = set().union(*employer_metrics.values()) if employer_metrics else set()
    _summary_sans_years = re.sub(r'\d+\+?\s*(?:years?|yrs?)\b', '', summary, flags=re.IGNORECASE)
    _summary_metrics = {m.lower() for m in _METRIC_RE.findall(_summary_sans_years)}
    if not (_summary_metrics & _known_employer_metrics):
        summary_issues.append(
            "summary contains no real, verifiable number from the candidate's experience bullets "
            "(beyond the years-of-experience figure) — it must cite at least one real metric such as "
            "10K+ users or 35% faster setup, drawn from the selected bullets, not a generic description"
        )
    # Resume voice is implied first person. "I led..." / "my work..." reads as
    # a cover letter pasted into a resume, and the summary brief forbids it;
    # flag it so the editor is told and the post-editor gate can compare.
    if re.search(r"\b(I|I've|I'm|I'd|my|me)\b", summary):
        summary_issues.append(
            "summary uses first person ('I', 'my', 'me') — resume voice is implied first person "
            "(write 'Led frontend delivery...', not 'I led...')"
        )
    # Check no project-only technology is claimed as expertise in summary
    employer_tech_corpus = " ".join(
        t.lower() for exp in master_data.get("experience", [])
        for bullet in exp.get("bullets", [])
        for t in bullet.get("tags", [])
    )
    project_only_techs = ["react", "node.js", "flask", "mongodb", "pytorch",
                          "tensorflow", "scikit-learn", "circom", "bert"]
    for tech in project_only_techs:
        if tech in summary_lower and tech not in employer_tech_corpus:
            summary_issues.append(
                f"'{tech}' claimed in summary but only in projects/coursework, not employer entries"
            )

    trust_score = int((passed / total) * 100) if total > 0 else 100
    return {
        "trust_score": trust_score,
        "flagged_bullets": flagged,
        "summary_issues": summary_issues,
        # trust_score alone is bullet-only — a summary-only problem (e.g. the
        # missing-metric check above) must also fail this, or editor_node's
        # "all validators passed, skip the LLM" shortcut would skip right
        # over a real issue that only shows up in run_summary.txt afterward,
        # by which point the PDF has already been generated.
        "passed": trust_score >= 85 and not summary_issues,
    }


def detect_dropped_metrics(selected_content: dict, writer_output: dict) -> dict:
    """
    Catches the inverse failure mode from validate_writer_output: instead of an
    invented metric, a REAL metric that existed in the original selected bullet
    silently disappeared during rewriting (writer tagged it [METRIC NEEDED]
    instead of preserving the number that was right there in the source).

    Compares, per employer, the set of metrics present across the ORIGINAL
    selected bullets against the set present across the REWRITTEN bullets.
    Matching is per-company (not per-bullet) because the writer may reorder
    or merge bullets — a per-bullet 1:1 match would false-positive on that.
    """
    original_metrics_by_company: dict = {}
    for b in selected_content.get("experience", []):
        company = b.get("company_name", b.get("company", ""))
        text = b.get("text", b.get("original_text", ""))
        metrics = {m.lower() for m in _METRIC_RE.findall(text)}
        if metrics:
            original_metrics_by_company.setdefault(company, set()).update(metrics)

    rewritten_metrics_by_company: dict = {}
    for b in writer_output.get("tailored_bullets", []):
        company = b.get("company_name", "")
        text = b.get("rewritten_text", "")
        metrics = {m.lower() for m in _METRIC_RE.findall(text)}
        rewritten_metrics_by_company.setdefault(company, set()).update(metrics)

    dropped = []
    for company, orig_metrics in original_metrics_by_company.items():
        survived = rewritten_metrics_by_company.get(company, set())
        missing = orig_metrics - survived
        if missing:
            dropped.append({
                "company": company,
                "dropped_metrics": sorted(missing),
                "fix": (
                    f"The original selected bullet(s) for {company} contained real "
                    f"metrics ({', '.join(sorted(missing))}) that are missing from every "
                    f"rewritten bullet for this employer. Restore the real number into "
                    f"the relevant bullet instead of using [METRIC NEEDED] — it was "
                    f"never actually missing from the source data."
                ),
            })

    return {
        "dropped_metrics": dropped,
        "passed": len(dropped) == 0,
    }


# Known programming languages and frameworks to detect in bullet text.
# Used by validate_cross_document_consistency — extend as needed.
_TECH_VOCAB = {
    "Python", "JavaScript", "TypeScript", "Java", "C++", "C#", "Go", "Rust",
    "Ruby", "Swift", "Kotlin", "Scala", "PHP", "R",
    "React", "Angular", "Vue", "Node.js", "Spring Boot", "Django", "Flask",
    "FastAPI", "Express", "Rails", "Next.js", "Svelte", "Ionic",
    "LangGraph", "LangChain", "PyTorch", "TensorFlow", "Scikit-learn",
    "BERT", "Docker", "Kubernetes", "MongoDB", "MySQL", "PostgreSQL",
    "Redis", "GraphQL", "Circom",
}

# Regex to find "N+ years of <tech>" or "N years of <tech>" in summary text.
_YEARS_OF_RE = re.compile(
    r'(\d+)\+?\s+years?\s+of\s+(?:[\w\s/,+-]+?)\s+(Python|JavaScript|TypeScript|Java'
    r'|Angular|React|Node\.js|Spring Boot|Ionic|Flask|Django|C\+\+)',
    re.IGNORECASE,
)


def validate_cross_document_consistency(draft_data: dict, master_data: dict) -> dict:
    """
    Deterministic QA pass that checks three consistency properties:
    (a) Job titles in generated text match master_data exactly.
    (b) Language/framework mentions in a bullet match that entry's known tech stack.
    (c) Summary years-of-experience claims are backed by employer section, not projects.
    Returns a structured report injected into the Editor prompt.
    """
    issues = []

    # --- Build ground-truth lookups from master_data ---
    # Exact title map: company -> canonical title
    title_map = {e["company"]: e["title"] for e in master_data.get("experience", [])}

    # Employer tech set: company -> all technologies_used across its bullets
    employer_tech: dict[str, set] = {}
    for exp in master_data.get("experience", []):
        techs: set = set()
        for b in exp.get("bullets", []):
            techs.update(t.strip() for t in b.get("technologies_used", []))
            # Also pull from tags if present
            techs.update(t.strip() for t in b.get("tags", []))
        employer_tech[exp["company"]] = techs

    # Project tech set: project name -> tech_stack
    project_tech: dict[str, set] = {
        p["name"]: set(p.get("tech_stack", []))
        for p in master_data.get("projects", [])
    }

    # Technologies that appear ONLY in projects (not in any employer entry)
    employer_tech_flat = set(t for techs in employer_tech.values() for t in techs)
    project_only_tech = set(
        t for techs in project_tech.values() for t in techs
    ) - employer_tech_flat

    # --- (a) Title accuracy ---
    summary_text = draft_data.get("tailored_summary", "")
    for company, canonical_title in title_map.items():
        # Check for known wrong variants in summary and bullet text
        wrong_variants = []
        # Flag only definitively wrong single-word or truncated uses
        if company == "Tata Consultancy Services":
            for wrong in ["System Engineer", "Systems Engineer", "Software Engineer at TCS",
                          "System Engineering"]:
                if wrong.lower() in summary_text.lower():
                    wrong_variants.append(wrong)
            for b in draft_data.get("tailored_bullets", []):
                if b.get("company_name") == company:
                    bt = b.get("rewritten_text", "")
                    for wrong in ["System Engineer", "Systems Engineer"]:
                        if wrong.lower() in bt.lower():
                            wrong_variants.append(f"bullet: '{wrong}'")
        if wrong_variants:
            issues.append({
                "type": "title_mismatch",
                "company": company,
                "canonical_title": canonical_title,
                "found": wrong_variants,
                "fix": f"Replace all occurrences with the exact title: '{canonical_title}'",
            })

    # --- (b) Tech stack consistency in bullets ---
    for b in draft_data.get("tailored_bullets", []):
        company = b.get("company_name", "")
        text = b.get("rewritten_text", "")
        allowed = employer_tech.get(company, set())
        for tech in _TECH_VOCAB:
            # Case-insensitive whole-word-ish check (avoid "React" matching "Reactive")
            pattern = re.compile(r'\b' + re.escape(tech) + r'\b', re.IGNORECASE)
            if pattern.search(text):
                # If the tech is not in this employer's known set AND is not a generic term
                if tech not in allowed and tech.lower() not in {t.lower() for t in allowed}:
                    issues.append({
                        "type": "tech_mismatch_experience",
                        "company": company,
                        "bullet_snippet": text[:80],
                        "tech_mentioned": tech,
                        "allowed_tech": sorted(allowed),
                        "fix": f"Remove '{tech}' from this bullet — it is not in {company}'s tech stack.",
                    })

    # Project bullets tech check
    for pb in draft_data.get("tailored_project_bullets", []) or []:
        proj_name = pb.get("project_name", "")
        allowed_proj = project_tech.get(proj_name, set())
        # Fuzzy match project name
        if not allowed_proj:
            for pname, ptech in project_tech.items():
                if proj_name.lower() in pname.lower() or pname.lower() in proj_name.lower():
                    allowed_proj = ptech
                    proj_name = pname
                    break
        # Preferred `bullets` list (every bullet of the project); legacy
        # bullet_1/bullet_2 fallback. The legacy fields are None when the
        # writer uses the list form — reading them as text crashed a real run
        # (Sep 2026), so only ever iterate real strings here.
        blist = [t for t in (pb.get("bullets") or []) if t]
        labeled = (
            [(f"bullets[{i}]", t) for i, t in enumerate(blist)] if blist
            else [(f, pb.get(f)) for f in ("bullet_1", "bullet_2") if pb.get(f)]
        )
        for field, text in labeled:
            for tech in _TECH_VOCAB:
                pattern = re.compile(r'\b' + re.escape(tech) + r'\b', re.IGNORECASE)
                if pattern.search(text):
                    if tech not in allowed_proj and tech.lower() not in {t.lower() for t in allowed_proj}:
                        issues.append({
                            "type": "tech_mismatch_project",
                            "project": proj_name,
                            "bullet_field": field,
                            "bullet_snippet": text[:80],
                            "tech_mentioned": tech,
                            "allowed_tech": sorted(allowed_proj),
                            "fix": f"Remove '{tech}' from {proj_name} {field} — not in project tech stack.",
                        })

    # --- (c) Summary years-of-experience framing ---
    for match in _YEARS_OF_RE.finditer(summary_text):
        tech_mentioned = match.group(2)
        years_claimed = int(match.group(1))
        if tech_mentioned.lower() in {t.lower() for t in project_only_tech}:
            issues.append({
                "type": "summary_experience_overclaim",
                "summary_snippet": match.group(0),
                "tech": tech_mentioned,
                "fix": (
                    f"'{tech_mentioned}' is project-only, not employer-level experience. "
                    f"Reframe: 'production experience with Angular/Spring Boot, with additional "
                    f"hands-on {tech_mentioned} experience from projects' — do not claim "
                    f"{years_claimed}+ years of {tech_mentioned}."
                ),
            })

    return {
        "title_map": title_map,
        "issues": issues,
        "issue_count": len(issues),
        "passed": len(issues) == 0,
    }


# ---------------------------------------------------------------------------
# NODE 0: SHOULD-APPLY GATE (free path: regex red flags → JD cache → cheap LLM)
# Runs before ANY tailoring work. A bad-fit JD never reaches a paid model.
# Scoring logic lives in core/should_apply.py — shared with scraper mode.
# ---------------------------------------------------------------------------
def should_apply_node(state: ResumeGraphState):
    print("--- [NODE 0] SHOULD-APPLY GATE ---")

    verdict = should_apply_engine.evaluate(
        state["job_description"],
        company=state.get("company_name", ""),
        title=state.get("job_title", ""),
    )

    print(f"  Stage: {verdict['stage']}  Score: {verdict['score']}/100  Proceed: {verdict['proceed']}")
    if verdict["red_flags"]:
        print(f"  Red flags: {verdict['red_flags']}")
    if verdict.get("reasoning"):
        print(f"  Reasoning: {verdict['reasoning']}")

    return {
        "should_apply": verdict,
        "triage_score": verdict["score"],        # kept for metadata/back-compat
        "triage_reasoning": verdict.get("reasoning", ""),
        "proceed": verdict["proceed"],
    }


def route_after_should_apply(state: ResumeGraphState):
    if state["proceed"]:
        print("--- [ROUTER] Should-apply passed. Moving to keyword extraction. ---")
        return "triage"
    print(f"--- [ROUTER] Should-apply score {state['triage_score']}/100 below threshold. Stopping — zero paid tokens spent. ---")
    return END


# ---------------------------------------------------------------------------
# NODE 0.5: KEYWORD EXTRACTION (cheap tier; cached per JD)
# Fit scoring moved to should_apply_node — this only extracts ATS keywords.
# ---------------------------------------------------------------------------
def triage_node(state: ResumeGraphState):
    print("--- [NODE 0.5] RUNNING KEYWORD EXTRACTION ---")

    jd = state["job_description"]
    master_data = state["master_data"]

    # Cache check: same JD already had keywords extracted (any entry mode).
    cached = jd_cache.get_entry(jd)
    if cached and cached.get("keywords"):
        keywords = cached["keywords"]
        print(f"  [cache] Reusing {len(keywords)} keywords extracted earlier")
    else:
        structured_llm = llm_fast.with_structured_output(TriageKeywords)
        prompt = f"""
    You are an ATS keyword analyst. Extract the 12-15 most critical ATS keywords
    from this job description. Focus on specific technologies, frameworks, and
    hard skills. Exclude generic terms like "communication", "teamwork", or
    "fast-paced environment".

    JOB DESCRIPTION:
    {jd}

    Return only the keywords list.
    """
        result = invoke_with_retry(
            structured_llm, prompt,
            cheap_rebuild={"temperature": 0.2, "max_tokens": 1024, "schema": TriageKeywords},
        )
        keywords = result.keywords
        jd_cache.update_entry(jd, keywords=keywords)

    # Cross-reference keywords against master data before passing downstream.
    # Only verified keywords reach the Writer — gap keywords are logged but never injected.
    verified_kw, gap_kw = _validate_keywords(keywords, master_data)

    print(f"  Verified keywords: {verified_kw}")
    print(f"  Gap keywords (excluded): {gap_kw}")

    return {
        "jd_keywords": keywords,           # full list preserved for reference
        "verified_keywords": verified_kw,
        "gap_keywords": gap_kw,
    }


# ---------------------------------------------------------------------------
# NODE A: STRATEGIST
# Selects the best bullets per employer using per-employer constraints
# read directly from master_data — no company names hardcoded in schema.
# ---------------------------------------------------------------------------
def strategist_node(state: ResumeGraphState):
    print("--- [NODE A] RUNNING STRATEGIST AGENT ---")

    jd = state["job_description"]
    master_data = state["master_data"]
    jd_keywords = state.get("verified_keywords") or state.get("jd_keywords", [])

    # Employers marked include_in_generic: false (thin, low-signal roles —
    # currently just the Virginia Tech Grader position) are excluded from the
    # BASE resume by generate_generic.py; the tailored pipeline never checked
    # this and could surface them anyway (real bug, found Sep 2026: the role
    # appeared on a real tailored resume with a bullet the strategist never
    # even selected, purely from this filter's absence). Tailored and base
    # must show the same set of employers — filter here, once, so nothing
    # downstream (the LLM prompt, the top-up/trim safety net) ever sees it.
    tailored_experience = [e for e in master_data["experience"] if e.get("include_in_generic", True)]

    structured_llm = llm_strategist.with_structured_output(StrategistSelection)

    # Build per-employer constraints dynamically from master_data.
    # Adding a new employer only requires updating the JSON, not this code.
    #
    # Bullet count target is resume_max_bullets, not a min-max range — the
    # tailored resume is meant to carry the SAME bullet count per employer as
    # the base resume (same content depth, only wording/ordering differs by
    # JD), so "select fewer because this JD seems narrower" is exactly the
    # behavior being removed here. The deterministic top-up below still tops
    # up to this same max as a guaranteed backstop if the LLM under-selects.
    employer_constraints = []
    total_bullets = 0
    for exp in tailored_experience:
        max_b = exp.get("resume_max_bullets", 4)
        total_bullets += max_b
        employer_constraints.append(
            f"  - {exp['company']}: select exactly {max_b} bullets — the {max_b} MOST relevant to "
            f"this JD, in relevance order, but the count is fixed at {max_b} regardless of how "
            f"narrow or broad the JD looks (tags available per bullet to match keywords)"
        )
    constraints_text = "\n".join(employer_constraints)

    # Surface project names explicitly so the LLM copies them verbatim.
    project_names_list = "\n".join(
        f"  - \"{p['name']}\" (domain: {p['domain']}, stack: {', '.join(p['tech_stack'])})"
        for p in master_data["projects"]
    )

    prompt = f"""
    You are an elite Career Strategist. Select the MOST impactful and relevant
    bullets and projects from the candidate's history for this specific job.

    JOB DESCRIPTION:
    {jd}

    CRITICAL ATS KEYWORDS TO MATCH:
    {jd_keywords}

    CANDIDATE EXPERIENCE (with tags per bullet for relevance matching):
    {json.dumps(tailored_experience, indent=2)}

    ALL PROJECTS — every one goes on the resume, none are dropped:
    {project_names_list}

    SELECTION RULES — follow these exactly:
    {constraints_text}
    Total experience bullets across all employers: {total_bullets}

    STRATEGY:
    - Use the 'tags' on each bullet to match against the JD keywords above.
    - Prioritize bullets that contain quantified achievements (percentages, timeframes, scale).
    - For 'selected_project_names', return ALL project names listed above, in JD-relevance
      order (most relevant first) — this drives which projects get the most prominent
      placement and the sharpest JD-tailored bullet wording, not which ones appear at all.

    REPORTING (required for every bullet):
    - For each SELECTED bullet: assign a relevance_score (1–5, where 5 = directly addresses
      a must-have JD requirement) and a one-sentence selection_reason.
    - For each DROPPED bullet: provide a one-sentence drop_reason explaining why it was
      excluded (e.g., "low keyword overlap", "superseded by stronger quantified bullet").
    - content_warnings: flag any bullet whose metric seems inflated or whose claim
      lacks supporting context visible in the data (leave empty list if none).

    IMPORTANT: For 'selected_project_names', copy the project name string EXACTLY as it
    appears in the Available Projects list above (including all punctuation and capitalization).
    """

    selection = invoke_with_retry(
        structured_llm, prompt,
        cheap_rebuild={"temperature": 0.2, "max_tokens": 3072, "schema": StrategistSelection},
    )

    # Deterministic safety net: the strategist has been observed not landing
    # exactly on an employer's stated resume_max_bullets despite the prompt
    # explicitly stating the constraint — same class of "prompt says X, the
    # cheap-tier model doesn't reliably comply" issue seen elsewhere in this
    # pipeline. Rather than a costly re-invoke that might not even fix it,
    # correct deterministically in Python in both directions:
    #   - shortfall: top up with real bullets (quantified ones first) from
    #     that employer's master data until the count is reached.
    #   - excess: keep only the max_b highest relevance_score bullets, drop
    #     the rest — same bar the LLM was asked to apply itself.
    # This guarantees every tailored resume carries the SAME bullet count per
    # employer as the base resume (Sep 2026 user request: tailored and base
    # should be structurally identical, differing only in wording/ordering),
    # regardless of how well the LLM actually followed the count instruction.
    _selected_by_company: dict = {}
    for b in selection.selected_bullets:
        _selected_by_company.setdefault(b.company_name, []).append(b)
    for exp in tailored_experience:
        company = exp["company"]
        max_b = exp.get("resume_max_bullets", 4)
        current = _selected_by_company.get(company, [])
        shortfall = max_b - len(current)
        if shortfall > 0:
            already_selected_text = {b.original_text for b in current}
            candidates = [b for b in exp["bullets"] if b["text"] not in already_selected_text]
            candidates.sort(key=lambda b: not b.get("is_quantified", False))  # quantified first
            added_bullets = []
            for b in candidates[:shortfall]:
                new_bullet = SelectedBullet(
                    company_name=company,
                    original_text=b["text"],
                    relevance_score=3,
                    selection_reason="Added automatically to reach this employer's fixed bullet count.",
                )
                selection.selected_bullets.append(new_bullet)
                added_bullets.append(new_bullet)
            _selected_by_company[company] = current + added_bullets
            if added_bullets:
                print(f"  [strategist] Topped up {company}: +{len(added_bullets)} bullet(s) to reach {max_b}")
        elif shortfall < 0:
            excess = -shortfall
            current.sort(key=lambda b: b.relevance_score, reverse=True)
            dropped, kept = current[max_b:], current[:max_b]
            for b in dropped:
                selection.selected_bullets.remove(b)
            _selected_by_company[company] = kept
            print(f"  [strategist] Trimmed {company}: -{excess} bullet(s) (lowest relevance_score) to reach {max_b}")

    # Fuzzy-validate returned project names against actual names to prevent
    # silent data loss if the LLM abbreviates or paraphrases.
    all_project_names = [p["name"] for p in master_data["projects"]]
    validated_projects = []
    for returned_name in selection.selected_project_names:
        if returned_name in all_project_names:
            validated_projects.append(returned_name)
        else:
            for real_name in all_project_names:
                if (returned_name.lower() in real_name.lower() or
                        real_name.lower() in returned_name.lower()):
                    validated_projects.append(real_name)
                    break

    # Safety net: ALL projects are always present, in whatever order the LLM
    # didn't already establish — deterministic guarantee, not dependent on the
    # LLM actually returning every name (Sep 2026: tailored resumes carry the
    # same projects as the base resume, always; JD relevance drives ordering/
    # bullet emphasis, not inclusion).
    for name in all_project_names:
        if name not in validated_projects:
            validated_projects.append(name)

    # IMPROVEMENT 2: Build selection_report for audit trail (written to disk in finalizer).
    selection_report = {
        "selected_bullets": [
            {
                "company": b.company_name,
                "text": b.original_text,
                "relevance_score": b.relevance_score,
                "selection_reason": b.selection_reason,
            }
            for b in selection.selected_bullets
        ],
        "dropped_bullets": [
            {
                "company": b.company_name,
                "text": b.original_text,
                "drop_reason": b.drop_reason,
            }
            for b in (selection.dropped_bullets or [])
        ],
        "selected_projects": validated_projects,
        "content_warnings": selection.content_warnings or [],
    }

    return {
        "selected_content": {
            "experience": [b.model_dump() for b in selection.selected_bullets],
            "projects": validated_projects
        },
        "selection_report": selection_report,
    }


# ---------------------------------------------------------------------------
# NODE B: WRITER
# All candidate facts are derived from master_data at runtime.
# Project bullets are also tailored here — not just experience bullets.
# ---------------------------------------------------------------------------
def writer_node(state: ResumeGraphState):
    print("--- [NODE B] RUNNING WRITER AGENT ---")

    jd = state["job_description"]
    company_name = state["company_name"]
    job_title = state["job_title"]
    selected = state["selected_content"]
    jd_keywords = state.get("verified_keywords") or state.get("jd_keywords", [])
    master_data = state["master_data"]

    gen_resume = state.get("generate_resume", True)
    gen_cl = state.get("generate_cover_letter", True)

    structured_llm = llm_quality.with_structured_output(WriterOutput)

    # Same include_in_generic filter as strategist_node — this node's own
    # candidate_background/summary context and cover-letter paragraph
    # instructions must not reference an employer the resume doesn't show.
    tailored_experience = [e for e in master_data["experience"] if e.get("include_in_generic", True)]

    # Build candidate background with computed years so the summary never
    # undersells experience (e.g. "3+" instead of "4+" for 3y8m tenure).
    exp_background = []
    for e in tailored_experience:
        entry = {
            "company": e["company"],
            "title": e["title"],
            "duration": e["duration"],
            "location": e["location"],
            "approx_years": compute_years_label(e["duration"])
        }
        exp_background.append(entry)

    candidate_background = {
        "experience": exp_background,
        "education": [
            {
                "degree": e["degree"],
                "university": e["university"],
                "gpa": e.get("gpa", e.get("cgpa", "N/A")),
                "duration": e["duration"],
                "coursework": e.get("coursework", [])
            }
            for e in master_data["education"]
        ],
        "availability": master_data["personal_info"].get("availability", "")
    }

    # Pull full project data for selected projects so writer can tailor bullets.
    # Strip fields the writer never reads to reduce input tokens.
    _BULLET_KEEP = {"text", "technologies_used"}
    selected_projects_data = [
        {
            "name":       p["name"],
            "tech_stack": p.get("tech_stack", []),
            "duration":   p.get("duration", ""),
            "bullets":    [
                {k: v for k, v in b.items() if k in _BULLET_KEEP}
                for b in p.get("bullets", [])
            ],
        }
        for p in master_data["projects"]
        if p["name"] in selected["projects"]
    ]

    # Strip metadata fields from selected experience bullets — writer only needs
    # text and technologies_used.
    selected_experience_trimmed = [
        {
            "company_name": b.get("company_name", b.get("company", "")),
            "text":         b.get("text", ""),
            "technologies_used": b.get("technologies_used", []),
        }
        for b in selected.get("experience", [])
    ]

    skill_category_names = list(master_data["technical_skills"].keys())

    # Exact title map passed verbatim into the prompt — LLM must not deviate from these.
    title_map_for_prompt = {e["company"]: e["title"] for e in master_data["experience"]}

    # Project tech stacks — only for selected projects (not all 7).
    _selected_project_names = {p["name"] for p in selected_projects_data}
    project_tech_map = {
        p["name"]: p.get("tech_stack", [])
        for p in master_data["projects"]
        if p["name"] in _selected_project_names
    }

    # Technologies that appear only in projects (not in any employer bullets).
    employer_tech_flat = set(
        t for exp in tailored_experience
        for b in exp.get("bullets", [])
        for t in b.get("technologies_used", [])
    )

    prompt = f"""
    You are an elite Technical Resume Writer and Career Coach. Write humanized,
    highly compelling application materials that pass Fortune 500 ATS filters
    while sounding authentic to a human recruiter.

    HONESTY CONSTRAINTS — These override all other instructions.

    1. Every metric in every bullet must exist verbatim or be directly derivable
       from the master data provided. Never round up, interpolate, or invent numbers.
       If a bullet has no metric in master data, write the bullet without one —
       do not fabricate a number to fill the gap. Instead append the flag:
       [METRIC NEEDED] at the end of that bullet.

    2. Never add a technology to a bullet that is not already associated with that
       employer or project in master data. Do not add React to a TCS bullet because
       the JD mentions React. Only use technologies tagged to that specific entry.
       For project bullets, ONLY mention technologies present in that project's tech_stack.
       PROJECT TECH STACKS (source of truth): {json.dumps(project_tech_map)}

    3. Never use these phrases under any circumstances:
       spearheaded, leveraged, synergized, passionate about, results-driven,
       detail-oriented, thought leader, dynamic, robust, cutting-edge,
       game-changing, best-in-class, world-class

    4. Never use weak verbs as the opening word of a bullet:
       helped, assisted, worked on, was responsible for, participated in,
       supported, contributed to

    5. The summary may only claim expertise in technologies that appear in
       employer entries (production experience). Technologies that appear only
       in projects or coursework must use softer framing:
       "hands-on project experience with X" not "expertise in X"

    6. If the JD requires a technology that does not appear anywhere in master data,
       do NOT mention it. Surface it as a gap in the cover letter's closing paragraph
       if appropriate — never in the resume itself.

    7. SHORT-TENURE CONTEXT — do not compress this away: if a selected bullet's source
       text explains WHY a short-duration role was short (e.g. "a paid, project-based
       fellowship", "a 3-month engagement"), that context clause must survive the
       rewrite. A bare 1-2 month role with no explanation reads as an unexplained abrupt
       stint to a recruiter; the framing clause is there specifically to answer that
       question before it gets asked. Trim other parts of the bullet for length before
       ever cutting this clause.

    8. FORMATTING — hard rules, no exceptions:
       - Never use an em dash (—) anywhere in the resume or cover letter.
         Use a comma, period, or semicolon instead.
       - Never use a pipe character (|) inside sentence text. Pipes are only
         for the LaTeX header template and must not appear in any bullet,
         summary sentence, or cover letter paragraph.
       - These two characters (— and |) are reliable signals of AI-generated
         text to a human reviewer. Their absence is non-negotiable.

    9. JOB TITLES — verbatim, no substitution:
       The following are the EXACT canonical titles from the candidate's record.
       Use these titles exactly if you reference a role in the summary or cover letter.
       Do NOT abbreviate, infer, or substitute a different title.
       CANONICAL TITLE MAP: {json.dumps(title_map_for_prompt)}
       ❌ "Systems Engineer at Tata Consultancy Services"
       ✅ "System Engineer at Tata Consultancy Services"

    10. SUMMARY EXPERIENCE FRAMING — non-negotiable:
       When writing "X years of experience with [technology stack]", the technology
       named MUST be present in the employer experience entries, not just projects.
       The candidate's 4+ year production stack is: Angular, Spring Boot, Java, Ionic,
       TypeScript, GitLab CI, JWT. React, Node.js, Flask, MongoDB, PyTorch, and similar
       appear ONLY in project work.
       - If tailoring toward a React/Node JD, write:
         "4+ years of production experience building enterprise applications with Java/Spring
         Boot and Angular, with hands-on React and Node.js experience from personal projects."
       - NEVER write: "4+ years of React/Node experience" or "Full-stack JavaScript engineer
         with 4+ years of enterprise experience" — these are false and inconsistent with the
         experience bullets.
       The summary and experience bullets must describe the SAME candidate.

    JOB DESCRIPTION: {jd[:2500]}
    ATS KEYWORDS (verified against candidate master data — do not add others): {jd_keywords}
    SELECTED EXPERIENCE BULLETS (rewrite these): {json.dumps(selected_experience_trimmed)}
    SELECTED PROJECTS (rewrite bullets for these): {json.dumps(selected_projects_data)}
    CANDIDATE BACKGROUND (use ONLY these facts — never invent): {json.dumps(candidate_background)}
    AVAILABLE SKILL CATEGORIES: {skill_category_names}
    """

    if gen_resume:
        primary_exp = exp_background[0]
        primary_edu = candidate_background["education"][0]
        edu_status = education_status_label(master_data["education"][0])

        prompt += f"""

    RESUME INSTRUCTIONS:

    — SUMMARY (2-3 sentences, 80 words maximum — a brief, not a template):
    Write it the way a person describes themselves, not the way a job posting describes a role —
    but in standard resume voice: IMPLIED first person, never "I", "my", or "me" (write "Led
    frontend delivery…", not "I led…"). Every tailored resume must NOT share one identical
    sentence skeleton; vary the construction. No filler clauses ("grounds hands-on work in
    scientific thinking", "passionate about") — every clause must carry a fact.

    TONE REFERENCE — the candidate's own base-resume summary. Match this voice; you may reuse
    its sentences where they already fit the JD:
    "{master_data['summary']}"

    Must be true of the result:
    - States total production experience as "{primary_exp['approx_years']} years" — never
      inflate it, and never pin all of it to one employer.
    - Contains one to three REAL numbers, every one of them drawn from the SELECTED experience
      bullets for {primary_exp['company']} (e.g. 10K+ users, 35%). A summary with no real number
      is a failure; a summary with a number that isn't in those bullets is a worse one.
    - Names the degree and school naturally — "an M.S. in Computer Engineering at Virginia Tech",
      not the raw strings pasted together. Degree: {primary_edu['degree']}. School:
      {primary_edu['university']} (omit the city). GPA {primary_edu['gpa']} may be included.
    - If the JD is AI/LLM-focused, the FIRST sentence says what the candidate has actually
      built recently with LLMs (an agentic LangGraph pipeline with a test suite; red-teaming
      models in a paid fellowship), framed honestly as recent/project work per rule 10.
      An AI role whose summary never mentions AI work is a failure.
    - The angle of the first sentence relates to the JD's primary requirement.

    Must NOT:
    - Reuse any multi-word phrase from the JOB DESCRIPTION verbatim. If the posting says
      "agentic application development" or "AI-powered developer tooling", those exact words
      may not appear in the summary — recruiters recognize their own posting echoed back
      instantly, and it reads as generated against the listing.
    - Read as a list of tools ("experience with X, Y, Z, and W"). Name at most two
      technologies in the entire summary.
    - Mention the company being applied to, visa status, work authorization, or availability.
    - Use bullet points, pipes, em dashes, or generic filler phrases.

    — KEYWORD PLACEMENT STRATEGY (follow strictly):
    TECHNICAL keywords (Angular, Python, Spring Boot, Docker, GitLab, RESTful APIs, microservices, etc.):
      → Embed in bullets as the TOOL or METHOD used — part of the action or context.
      → Example: "Engineered Spring Boot microservices..." or "Automated CI/CD pipelines using GitLab..."
    CONCEPTUAL keywords (System Reliability, Observability, Monitoring, Operations at scale,
    Software Development, Performance Optimization, Infrastructure, Design Documents):
      → These go ONLY in the professional summary — NEVER appended to bullet endings.
      → Embed 1-2 of the most important conceptual keywords naturally into the summary sentence.
      → DO NOT place conceptual keywords at the end of bullets.

    — EXPERIENCE BULLETS:
    0. ONE-TO-ONE WITH THE SOURCE — this overrides everything below. For each SELECTED bullet,
       output exactly one rewritten bullet for the same employer that a reader could map straight
       back to it: the same facts, the same numbers, the same technologies and named specifics
       (Figma, WCAG, JWT route guards, Poseidon hashing), with the wording tightened or re-angled
       toward this JD. Specifics are what make a bullet credible — keep them; a rewrite that
       replaces them with general phrasing ("coordinated with stakeholders", "maintained code
       quality") has lost the bullet. Each source's framing stays with that source. Keep every
       number the source has, and add the [METRIC NEEDED] tag ONLY when the source itself has
       no number at all. The count is already exact, so there is never a reason to write a
       bullet that has no source. A Python check compares each rewritten bullet with its source
       and reverts any that drifted, so drifting only costs you the tailoring.
    1. Rewrite each selected bullet: "Action verb + [what you did using what tool/method] + [specific measurable result]".
    2. Vary how bullets OPEN. Most start with a plain past-tense verb (Built, Designed, Led,
       Automated, Integrated), but not every one has to — a bullet may open with the system,
       the problem, or the outcome when that reads more naturally. Do NOT cycle through a list
       of "power verbs" so that no two bullets share an opener; that rotation is itself a
       recognizable LLM-resume pattern. Repeating "Built" twice is fine if it's the honest verb.
       "Spearheaded" stays banned (HONESTY CONSTRAINTS above).
    3. BULLET ENDING RULE: the last clause should land on a real, concrete outcome, not trail
       off into a keyword phrase. NEVER end with a bare keyword suffix.
       ❌ "...supporting 10K+ users and enhancing Software Systems."
       ❌ "...implementing JWT flows to enhance Software Development and System Reliability."
       ✅ "...serving 10K+ active users in a regulated UK financial services environment."
       ✅ "...reducing initial project setup time by 35% across all client engagements."
       This does NOT mean every bullet must end in a number. A concrete non-numeric outcome
       (scope delivered, a system stabilized, ownership of a deliverable) is a valid ending
       when that is what the source bullet actually supports — do not force a number where
       master data has none; use [METRIC NEEDED] per the HONESTY CONSTRAINTS instead of
       inventing one, or simply let a genuinely metric-free bullet end on its real outcome.
    4. AVOID UNIFORM AI CADENCE: do not make every bullet the same length and shape. A resume
       where every line follows the identical "Verb + Action + Number%" template, with no
       variation in rhythm or sentence structure, is itself a pattern experienced recruiters
       now recognize as AI-generated — and that recognition costs credibility on the bullets
       that are genuinely true. Some variation in length and construction reads as more
       authentic, not less impressive. Do not sacrifice real numbers to manufacture variation;
       vary sentence shape and rhythm, not the presence of real data.
    5. Keep each bullet to 1-2 lines. Preserve all real numbers and technologies from originals.

    — PROJECT BULLETS:
    For EVERY project in SELECTED PROJECTS, rewrite ALL of its bullets into that project's
    'bullets' list — same count and same order as the original bullets. A tailored resume
    carries the same project depth as the base resume; you are re-angling the wording toward
    this JD, not cutting bullets.
    - Emphasize the implementation details most relevant to THIS JD; embed technical ATS
      keywords only where they fit naturally.
    - A bullet whose source has a real number keeps that exact number. A bullet whose source
      has NO number stays metric-free and ends on its real outcome (what was built, what it
      prevents, what it enables). NEVER invent a metric to make a bullet "end in a number" —
      several projects here genuinely have none, and that is fine.
    - Same rhythm rule as experience bullets: vary length and construction.

    — SKILL CATEGORIES:
    Return 'prioritized_skill_categories' with exact category names from AVAILABLE SKILL CATEGORIES,
    ordered most-to-least relevant to this JD.
    """

    if gen_cl:
        exp_paragraphs = ""
        for i, exp in enumerate(tailored_experience, start=2):
            exp_paragraphs += (
                f"\n    {i}. {exp['company']} ({exp['title']}, {exp['duration']}): "
                f"Highlight the most JD-relevant work, quantified impact, and technical depth. "
                f"Reference technical ATS keywords naturally."
            )

        primary_edu = master_data["education"][0]
        # `availability` is optional in master data (removed Sep 2026 — a start
        # date is meaningless once the candidate is already applying). When
        # absent, the closing simply doesn't state one.
        availability = (master_data["personal_info"].get("availability") or "").strip()
        closing_line = (
            f"End with: {availability}." if availability
            else "End with one confident, specific sentence about why this role, in the candidate's own words — no start date, no availability statement."
        )

        prompt += f"""

    COVER LETTER INSTRUCTIONS:
    Write a tailored, authentic 4-5 paragraph cover letter body (no header, no sign-off)
    for {job_title} at {company_name}.
    Tone: Professional, enthusiastic, human, and confident.
    Avoid: "I am thrilled to apply", "fast-paced environment", "I am writing to express",
    "passionate about", "I would be a great fit".

    STRUCTURE:
    1. Hook: Open with a genuine, specific observation about {company_name} from the JD
       (a product, mission, or technical challenge). Show you did research — not a generic
       compliment. Do NOT start with "I am writing to apply."{exp_paragraphs}
    {len(tailored_experience) + 2}. Education & Closing: {primary_edu['degree']} at
       {primary_edu['university']} (GPA: {primary_edu.get('gpa', 'N/A')}). Map 1-2 specific
       coursework areas to the company's tech requirements. {closing_line}

    RULES:
    - 4-5 distinct paragraphs in 'cover_letter_paragraphs' (one string per paragraph).
    - Weave in technical ATS keywords: {jd_keywords}
    - Each paragraph adds NEW information — no paragraph merely restates the resume.
    - Cover letter body only — no "Dear Hiring Team" or sign-off.
    """

    draft = invoke_with_retry(structured_llm, prompt)

    # Deterministic count repair: the writer has been observed dropping a
    # bullet during rewriting even when the strategist correctly selected
    # enough (e.g. selecting 2 Handshake AI bullets per its min=2 constraint,
    # but the writer only returning 1 rewritten bullet for that employer) —
    # Sonnet doesn't guarantee 1:1 count preservation between "selected" and
    # "rewritten." Falls back to the ORIGINAL (unrewritten but 100% real)
    # bullet text for whichever selected bullets didn't get a rewrite.
    _tailored_bullets = top_up_bullets_by_company(
        [b.model_dump() for b in draft.tailored_bullets],
        selected_experience_trimmed,
        fallback_key="text",
    )
    # Deterministic fidelity floor: any rewrite that drifted from its source
    # (generic filler, dropped numbers, invented specifics) is reverted to the
    # source text. See enforce_bullet_traceability().
    _tailored_bullets, _reverts = enforce_bullet_traceability(_tailored_bullets, selected_experience_trimmed)
    for _company, _reason in _reverts:
        print(f"  [traceability] {_company}: reverted a rewritten bullet to its source — {_reason}")
    _tailored_projects, _preverts = enforce_project_bullet_traceability(
        merge_project_bullets(draft.tailored_project_bullets), master_data["projects"]
    )
    for _pname, _reason in _preverts:
        print(f"  [traceability] {_pname}: reverted a project bullet to its source — {_reason}")

    # Second Haiku call: generate resume strategy object for cover letter pipeline.
    strategy_llm = llm_strategist.with_structured_output(ResumeStrategy)
    strategy_prompt = f"""You are a senior career strategist. Based on the job description and candidate profile below, produce a structured strategy object that will guide the cover letter writer.

CANDIDATE PROFILE:
{load_profile_cache()}

JOB: {job_title} at {company_name}

JOB DESCRIPTION (first 1500 chars):
{jd[:1500]}

VERIFIED KEYWORDS: {jd_keywords}

Produce the strategy object now."""
    strategy = invoke_with_retry(
        strategy_llm, strategy_prompt,
        cheap_rebuild={"temperature": 0.2, "max_tokens": 3072, "schema": ResumeStrategy},
    )

    return {
        "final_resume_data": {
            "tailored_summary": draft.tailored_summary,
            "tailored_bullets": _tailored_bullets,
            "tailored_project_bullets": _tailored_projects or None,
            "selected_projects": selected["projects"],
            "cover_letter_paragraphs": draft.cover_letter_paragraphs,
            "prioritized_skill_categories": draft.prioritized_skill_categories
        },
        "resume_strategy": strategy.model_dump()
    }


# ---------------------------------------------------------------------------
# NODE B.5: EDITOR (ATS + Truthfulness QA)
# ---------------------------------------------------------------------------
def editor_node(state: ResumeGraphState):
    print("--- [NODE B.5] RUNNING ATS & TRUTHFULNESS EDITOR ---")

    jd = state["job_description"]
    master_data = state["master_data"]
    draft_data = state["final_resume_data"]
    jd_keywords = state.get("verified_keywords") or state.get("jd_keywords", [])

    # Run Python validation before the LLM editor sees the draft.
    trust_report = validate_writer_output(draft_data, master_data)
    trust_score = trust_report["trust_score"]
    flagged_count = len(trust_report["flagged_bullets"])

    if not trust_report["passed"]:
        print(f"  ⚠ WARNING: Trust score {trust_score}/100 — {flagged_count} bullet(s) flagged")
        for item in trust_report["flagged_bullets"]:
            print(f"    [{item['company']}] {item['issues']}")
    else:
        print(f"  ✓ Trust score {trust_score}/100 — draft passed pre-editor validation")

    # Cross-document consistency check: titles, tech stacks, summary framing.
    consistency_report = validate_cross_document_consistency(draft_data, master_data)
    if consistency_report["passed"]:
        print("  ✓ Cross-document consistency: no issues found")
    else:
        print(f"  ⚠ Consistency issues ({consistency_report['issue_count']} found):")
        for issue in consistency_report["issues"]:
            print(f"    [{issue['type']}] {issue.get('fix', '')[:100]}")

    # Dropped-metric check: catches the writer discarding a REAL metric that
    # existed in the original selected bullet (tagging [METRIC NEEDED] instead
    # of preserving it), the inverse failure mode from invented metrics.
    dropped_metrics_report = detect_dropped_metrics(state["selected_content"], draft_data)
    if dropped_metrics_report["passed"]:
        print("  ✓ No real metrics were dropped during rewriting")
    else:
        print(f"  ⚠ Dropped metrics detected ({len(dropped_metrics_report['dropped_metrics'])} employer(s)):")
        for item in dropped_metrics_report["dropped_metrics"]:
            print(f"    [{item['company']}] lost: {item['dropped_metrics']}")

    # If ALL THREE deterministic validators pass, skip the LLM entirely: the
    # writer's prose is already clean, an extra rewrite can only degrade it, and
    # the finalizer strips em dashes / pipes deterministically anyway.
    if trust_report["passed"] and consistency_report["passed"] and dropped_metrics_report["passed"]:
        print("  ✓ All validators passed — skipping editor LLM call (prose preserved, 0 tokens)")
        return {
            "trust_report": trust_report,
            "final_resume_data": draft_data,
        }

    structured_llm = llm_editor.with_structured_output(WriterOutput)

    # Surface flagged bullets explicitly so the editor knows exactly where to focus.
    flagged_summary = (
        json.dumps(trust_report["flagged_bullets"], indent=2)
        if trust_report["flagged_bullets"]
        else "None — all bullets passed Python validation."
    )

    # Surface summary-level issues too (e.g. missing metric in sentence 2) —
    # previously only flagged_bullets reached this prompt, so the editor had
    # no way to know a summary-only problem existed even when it ran.
    summary_issues_text = (
        "\n".join(f"- {issue}" for issue in trust_report.get("summary_issues", []))
        if trust_report.get("summary_issues")
        else "None — summary passed Python validation."
    )

    consistency_issues_summary = (
        json.dumps(consistency_report["issues"], indent=2)
        if consistency_report["issues"]
        else "None — title map, tech stacks, and summary framing all passed."
    )

    dropped_metrics_summary = (
        json.dumps(dropped_metrics_report["dropped_metrics"], indent=2)
        if dropped_metrics_report["dropped_metrics"]
        else "None — no real metrics were lost during rewriting."
    )

    # Pass the exact title map so the editor knows the canonical values.
    title_map = consistency_report["title_map"]

    # Strip metadata-only fields from experience bullets — editor only needs
    # text and technologies_used to validate the draft.
    _editor_exp = [
        {
            "company":  e["company"],
            "title":    e["title"],
            "bullets":  [
                {"text": b["text"], "technologies_used": b.get("technologies_used", [])}
                for b in e.get("bullets", [])
            ],
        }
        for e in master_data["experience"]
    ]

    prompt = f"""
    Act as the most advanced ATS used by Fortune 500 companies AND a strict QA Editor.
    Polish this draft resume and cover letter to perfection.

    JOB DESCRIPTION: {jd[:2500]}
    CRITICAL ATS KEYWORDS (every one must appear naturally): {jd_keywords}
    CANDIDATE TRUE MASTER DATA: {json.dumps(_editor_exp)}
    CURRENT DRAFT: {json.dumps(draft_data)}

    PRE-EDITOR PYTHON VALIDATION REPORT (trust score: {trust_score}/100):
    The following bullets were flagged by an automated check BEFORE you see this prompt.
    These are your highest-priority fixes:
    {flagged_summary}

    CROSS-DOCUMENT CONSISTENCY REPORT ({consistency_report['issue_count']} issue(s)):
    These issues were detected by a deterministic Python check comparing the draft against
    master data. Fix ALL of them before returning:
    {consistency_issues_summary}

    DROPPED METRICS REPORT:
    These employers had a REAL metric in the original selected bullet that is now missing
    from every rewritten bullet for that employer (likely replaced with [METRIC NEEDED]).
    The metric was never actually missing — restore it into the correct bullet, then remove
    any [METRIC NEEDED] tag on that bullet:
    {dropped_metrics_summary}

    SUMMARY VALIDATION ISSUES:
    {summary_issues_text}

    YOUR STRICT DIRECTIVES:
    1. TRUTHFULNESS: Cross-reference every claim against Master Data. Delete or revert any
       invented skills, metrics, timeframes, or technologies not in the original data. Every
       bullet must stay traceable one-to-one to a master-data bullet for the SAME employer —
       you may polish wording, but you may not add activities/artifacts the source doesn't
       state, merge or split bullets, move a framing clause between employers, or write a new
       bullet to fill a count. If you cannot fix a bullet without inventing, revert it to the
       draft's text. Your job is to make the draft cleaner and truer, never to re-tailor it.
       If a bullet is tagged [METRIC NEEDED] but the DROPPED METRICS REPORT above shows a
       real metric exists for that employer, restore it and remove the tag — do not leave
       [METRIC NEEDED] in the output when a real number was available.
    2. TAIL-KEYWORD STUFFING — HIGHEST PRIORITY CHECK: Scan every bullet for this exact
       anti-pattern: a real bullet ending followed by a comma/conjunction and then a JD keyword
       phrase as a suffix. Examples to catch and fix:
       ❌ "...supporting 10K+ users and enhancing Software Systems."
       ❌ "...authentication flows to enhance Software Development and System Reliability."
       ❌ "...reducing page load times, thereby improving Performance Optimization of Software Systems."
       ❌ "...deployment cycles by 40% and improving System Reliability and Operations at scale."
       If you find this pattern, DELETE the keyword suffix entirely. The bullet already ends
       with a strong outcome — the suffix adds nothing and screams AI-generated to recruiters.
    3. ATS COVERAGE: Verify each keyword appears at least once across the full resume.
       If a keyword is missing, weave it into the ACTION or CONTEXT part of a bullet where
       it fits naturally, OR into the summary. If it cannot fit naturally anywhere, skip it —
       a missing keyword is better than an obviously stuffed one.
    4. BULLET STRUCTURE: Each bullet must follow "Action → Context → Result".
       The RESULT must be a specific business outcome (number, scale, quality). Never a keyword.
       If a bullet exceeds two lines, condense it to punchy scannable text. A result does NOT
       have to be numeric — do not manufacture a percentage the master data doesn't support.
       Do not homogenize bullets into identical length/shape while editing; a resume where
       every line reads as the same templated sentence is a recognizable AI-generation
       pattern to recruiters in 2026 and undermines the bullets that are genuinely true.
    5. SUMMARY CHECK: Verify the summary states years of experience, names the degree and school
       naturally (not raw strings pasted together, no city), and contains at least one real,
       verifiable metric from the candidate's experience bullets (e.g. "10K+ users", "35% faster
       setup"). If SUMMARY VALIDATION ISSUES above flags a missing metric, ADD a sentence that
       cites one from the candidate's selected bullets (shown in CANDIDATE TRUE MASTER DATA) —
       do not delete or shorten the summary to make the flag go away, and never invent a number.
       Do NOT add visa status, work authorization, or availability; remove them if present.
       The summary must not read as a list of tools and must not echo the job description's own
       multi-word phrasing back at the reader.
    6. TONE: Strip any remaining generic AI language, and remove any multi-word phrase copied
       verbatim from the JOB DESCRIPTION into the summary or bullets — rephrase in the
       candidate's own words. Output must sound like a person, authentic and specific.
    7. COVER LETTER: Each paragraph must have a clear purpose. No paragraph should merely
       restate the resume bullets — it must add context, story, or motivation.
    8. PROJECT BULLETS: Apply the same anti-stuffing, bullet-ending, and truthfulness rules to
       every string in each project's 'bullets' list. Keep the same count and order as the
       draft. A project bullet is NOT required to end in a number: if the original project data
       has no metric, the bullet stays metric-free. If a bullet cites a number that is not in the
       original project data, delete that number (or the clause carrying it) — never keep an
       invented metric and never add one.
    9. SKILL CATEGORIES: Preserve the 'prioritized_skill_categories' order from the draft
       unless you identify a clearly more relevant ordering for this specific JD.
    10. EM DASH AND PIPE REMOVAL — scan every field before returning:
        - Replace every em dash (—) with a comma or semicolon.
        - Remove every pipe (|) that appears inside a sentence, bullet, summary,
          or cover letter paragraph. Pipes belong only in the LaTeX header.
        - These are the single most reliable AI-generation signals to a human
          reviewer. Zero tolerance.
    11. JOB TITLE ACCURACY — non-negotiable:
        Every role title in generated text (summary sentence 2, cover letter references)
        MUST match the canonical title below exactly. Do NOT abbreviate or substitute.
        CANONICAL TITLES: {json.dumps(title_map)}
        The most common failure: writing "System Engineer" for Tata Consultancy Services.
        The correct title is "{title_map.get('Tata Consultancy Services', 'System Engineer')}".
        Check: summary sentence 2, any cover letter paragraph referencing TCS, and every
        bullet's implied role framing.
    12. TECH STACK CONSISTENCY IN PROJECT BULLETS — non-negotiable:
        For each project bullet, the programming languages and frameworks mentioned must
        match that project's listed tech stack. The AI Resume Agent project is Python-based
        (Python, LangGraph, LangChain, Anthropic API) — if any bullet says "TypeScript",
        "JavaScript", or any other language not in the tech stack, correct it to Python.
        Apply this cross-check to every project entry.
    13. SUMMARY EXPERIENCE FRAMING:
        The summary's "years of experience" claim must be consistent with the experience
        section. Production stack (4+ years): Angular, Spring Boot, Java, Ionic, TypeScript.
        React, Node.js, Flask, MongoDB are project-only. If the current summary overstates
        React/Node as primary production skills, reframe it as shown in directive 9 above.
        The resume and summary must describe the same person.

    Return the fully polished version. Leave zero chance of ATS rejection.
    """

    final_polish = invoke_with_retry(
        structured_llm, prompt,
        cheap_rebuild={"temperature": 0.0, "max_tokens": 4096, "schema": WriterOutput},
    )

    # Same count-repair as writer_node, applied again here — the editor's OWN
    # rewrite pass has been observed independently dropping a bullet per
    # employer (e.g. writer correctly produced 2 Handshake AI bullets, editor
    # polished it down to 1), even though draft_data going INTO this node
    # already had the right, complete counts. Falls back to the WRITER's
    # rewritten text (draft_data) rather than the raw original — better
    # quality than writer_node's fallback, since draft_data is already
    # JD-tailored prose, just not this editor pass's polish.
    _final_bullets = top_up_bullets_by_company(
        [b.model_dump() for b in final_polish.tailored_bullets],
        draft_data.get("tailored_bullets", []),
        fallback_key="rewritten_text",
    )

    trust_report["dropped_metrics"] = dropped_metrics_report["dropped_metrics"]

    edited = {
        "tailored_summary": final_polish.tailored_summary,
        "tailored_bullets": _final_bullets,
        "tailored_project_bullets": (
            merge_project_bullets(final_polish.tailored_project_bullets)
            if final_polish.tailored_project_bullets
            else draft_data.get("tailored_project_bullets")
        ) or draft_data.get("tailored_project_bullets"),
        "selected_projects": draft_data["selected_projects"],
        "cover_letter_paragraphs": final_polish.cover_letter_paragraphs,
        "prioritized_skill_categories": (
            final_polish.prioritized_skill_categories
            or draft_data.get("prioritized_skill_categories")
        )
    }

    # Post-editor gate: validate what the editor produced exactly the way the
    # draft was validated, and keep the draft for any section the editor made
    # worse. See reconcile_editor_output() for why this exists.
    post_report = validate_writer_output(edited, master_data)
    reconciled, reasons = reconcile_editor_output(draft_data, edited, trust_report, post_report)
    # Same fidelity floor as writer_node, applied to whatever survived the
    # gate — the editor's polish must not drift a bullet from its source either.
    reconciled["tailored_bullets"], _reverts = enforce_bullet_traceability(
        reconciled.get("tailored_bullets", []), (state.get("selected_content") or {}).get("experience", [])
    )
    for _company, _reason in _reverts:
        print(f"  [traceability] {_company}: reverted a rewritten bullet to its source — {_reason}")
    reconciled["tailored_project_bullets"], _preverts = enforce_project_bullet_traceability(
        reconciled.get("tailored_project_bullets") or [], master_data["projects"]
    )
    for _pname, _reason in _preverts:
        print(f"  [traceability] {_pname}: reverted a project bullet to its source — {_reason}")
    if reasons:
        print(f"  ⚠ Post-editor gate: editor output was worse than the draft — kept draft for: "
              + "; ".join(reasons))
    else:
        print(f"  ✓ Post-editor gate: edited output validated (trust {post_report['trust_score']}/100)")
    trust_report["post_editor_trust_score"] = post_report["trust_score"]
    trust_report["post_editor_fallbacks"] = reasons

    return {
        "trust_report": trust_report,
        "final_resume_data": reconciled,
    }


# ---------------------------------------------------------------------------
# NODE C: FINALIZER
# ---------------------------------------------------------------------------
def finalizer_node(state: ResumeGraphState):
    print("--- [NODE C] RUNNING FINALIZER ---")

    # Checkpoint: dump raw state immediately so a crash below doesn't lose the LLM work.
    # If the finalizer fails, this file survives and can be used to replay without re-running
    # the expensive Strategist/Writer/Editor nodes.
    _checkpoint_dir = os.path.join(RESUME_OUTPUT_PATH, "_checkpoints")
    os.makedirs(_checkpoint_dir, exist_ok=True)
    _checkpoint_name = (
        f"{sanitize_filename_component(state.get('company_name'), 'unknown')}_"
        f"{sanitize_filename_component(state.get('job_title'), 'role')}_"
        f"{datetime.datetime.now().strftime('%d_%m_%H_%M_%S')}.json"
    )
    _checkpoint_path = os.path.join(_checkpoint_dir, _checkpoint_name)
    try:
        with open(_checkpoint_path, "w") as _f:
            json.dump(
                {k: v for k, v in state.items() if k != "master_data"},
                _f, indent=2, default=str
            )
    except Exception as _e:
        print(f"  [finalizer] Warning: checkpoint write failed — {_e}")

    final_data = copy.deepcopy(state["master_data"])

    # Text hygiene (em dash removal, stray [METRIC NEEDED] tags, mid-sentence
    # pipes) is handled once, universally, by pdf_generator.sanitize_output_text
    # at render time — no need to duplicate that here. This section only
    # handles business logic specific to this candidate's data (title fixes).

    # Title normalization: correct known LLM substitutions to the canonical form from master data.
    # Build this dynamically from master_data so it stays in sync when titles change.
    canonical_titles = {e["company"]: e["title"] for e in state["master_data"]["experience"]}
    # Known wrong variants the LLM tends to generate (add entries as new patterns emerge).
    _title_corrections = {
        "Systems Engineer": canonical_titles.get("Tata Consultancy Services", "System Engineer"),
    }

    def _normalize_titles(text: str) -> str:
        for wrong, canonical in _title_corrections.items():
            text = text.replace(wrong, canonical)
        return text

    raw_summary = state["final_resume_data"]["tailored_summary"]
    final_data["summary"] = _normalize_titles(raw_summary)

    # Deterministic backstop for the summary's evidence (Sep 2026): on a real
    # run the writer produced a summary with no achievement metric at all, the
    # validator flagged it, and the editor's fix didn't stick — the resume
    # shipped with a summary that asserted nothing measurable. Same pattern as
    # the bullet-count safety nets: if, after stripping the "N+ years" figure,
    # the summary shares no metric token with the strategist's selected
    # bullets, insert the base-resume summary's own achievement sentence (real,
    # already verified, written in the candidate's voice) right after the
    # first sentence. A slightly redundant true sentence beats an evidence-free
    # summary, and this can never silently regress regardless of the LLM.
    _summary_sans_years = re.sub(r'\d+\+?\s*(?:years?|yrs?)\b', '', final_data["summary"], flags=re.IGNORECASE)
    _summary_metrics = {m.lower() for m in _METRIC_RE.findall(_summary_sans_years)}
    _selected_metrics = {
        m.lower()
        for b in (state.get("selected_content") or {}).get("experience", [])
        for m in _METRIC_RE.findall(b.get("original_text") or b.get("text") or "")
    }
    if not (_summary_metrics & _selected_metrics):
        _base_sentences = re.split(r'(?<=[.!?])\s+', (state["master_data"].get("summary") or "").strip())
        _anchor = next((s for s in _base_sentences if _METRIC_RE.findall(s)), None)
        if _anchor:
            _parts = re.split(r'(?<=[.!?])\s+', final_data["summary"].strip())
            _parts.insert(1, _anchor)
            final_data["summary"] = " ".join(p.strip() for p in _parts if p.strip())
            print("  [finalizer] summary carried no real metric — inserted the base-resume achievement sentence")
    raw_cl = state["final_resume_data"]["cover_letter_paragraphs"] or []
    final_data["cover_letter_paragraphs"] = [_normalize_titles(p) for p in raw_cl]
    final_data["company_name"] = state.get("company_name", "Unknown Company")
    final_data["job_title"] = state.get("job_title", "Software Engineer")

    # Per-company email override (e.g. a dedicated alias for a specific
    # employer) — configured via core/email_overrides.py, matched
    # case-insensitively as a substring of the company name. Master data
    # itself is untouched; this only affects this one rendered resume/CL.
    from core.email_overrides import get_email_override
    email_override = get_email_override(final_data["company_name"])
    if email_override:
        final_data["personal_info"]["email"] = email_override
        final_data["personal_info"]["hrefemail"] = f"mailto:{email_override}"
        print(f"  [finalizer] Email override for '{final_data['company_name']}': {email_override}")

    # 1. Reorder skills by JD relevance
    prioritized_cats = state["final_resume_data"].get("prioritized_skill_categories")
    if prioritized_cats:
        original_skills = final_data["technical_skills"]
        reordered = {cat: original_skills[cat] for cat in prioritized_cats if cat in original_skills}
        for cat in original_skills:
            if cat not in reordered:
                reordered[cat] = original_skills[cat]
        final_data["technical_skills"] = reordered

    # 2. Map tailored bullets back to their companies
    #
    # Final guarantee, independent of whether strategist_node/writer_node
    # behaved correctly upstream (Sep 2026 — a real bug on a live run: an
    # include_in_generic:false employer surfaced on a tailored resume with a
    # bullet the strategist never selected, and a separate employer got one
    # MORE bullet than its resume_max_bullets, from a writer-side top-up that
    # only guards against under-counts, not over-counts). Two explicit checks
    # here close both regardless of root cause upstream:
    #   - skip any employer marked include_in_generic: false entirely
    #   - cap each employer's final bullet list at its own resume_max_bullets,
    #     keeping the first N (already relevance-ordered coming out of the
    #     strategist/writer) rather than trusting upstream counts blindly
    new_experience = []
    for exp in final_data["experience"]:
        if not exp.get("include_in_generic", True):
            continue
        company = exp["company"]
        company_bullets = [
            {"text": b["rewritten_text"], "is_quantified": True}
            for b in state["final_resume_data"]["tailored_bullets"]
            if b["company_name"] == company
        ]
        max_b = exp.get("resume_max_bullets")
        if max_b and len(company_bullets) > max_b:
            print(f"  [finalizer] {company}: {len(company_bullets)} bullets exceeds max_bullets="
                  f"{max_b} — trimming to {max_b}")
            company_bullets = company_bullets[:max_b]
        if company_bullets:
            exp["bullets"] = company_bullets
            new_experience.append(exp)
    # Reverse to descending chronological order (newest employer first).
    # Master data is kept oldest-first for strategist selection logic.
    final_data["experience"] = list(reversed(new_experience))

    # Achievements suppressed unconditionally, matching the base resume exactly
    # (Sep 2026 user request — tailored and base are meant to be structurally
    # identical). The summary already states the award count
    # ("...recognized with 8 performance awards"), so a dedicated section
    # repeats the same fact. The old one_page-gated suppression is retired —
    # in practice on_new_entry callers never passed one_page=True, so this
    # was already a no-op for every real resume generated through the web UI;
    # now it's explicit and unconditional instead of accidentally-always-off.
    final_data["achievements"] = []

    # 3. Apply tailored bullets to every project — ALL projects are always
    # included (Sep 2026 user request: same content as the base resume,
    # every time; the strategist's own safety net already guarantees
    # `selected_projects` covers every project name, so this is no longer a
    # filter, just documentation that nothing gets dropped here).
    assert len(final_data["projects"]) == len(state["master_data"]["projects"]), (
        "finalizer_node: project count changed unexpectedly — every project "
        "must always be included (see strategist_node's project safety net)"
    )

    tailored_proj = state["final_resume_data"].get("tailored_project_bullets")
    if tailored_proj:
        # .get() rather than direct indexing — data has already passed through
        # merge_project_bullets() by this point so both fields should always be
        # real strings, but this is the last line standing between a malformed
        # entry and a crash, so it degrades (skips the project, keeps its
        # original master-data bullets) instead of raising.
        proj_bullet_map = {}
        for p in tailored_proj:
            name = p.get("project_name")
            if not name:
                continue
            blist = [t for t in (p.get("bullets") or []) if t]
            if blist:
                proj_bullet_map[name] = [{"text": t} for t in blist]
            elif p.get("bullet_1") and p.get("bullet_2"):  # legacy two-bullet form
                proj_bullet_map[name] = [{"text": p["bullet_1"]}, {"text": p["bullet_2"]}]
        for proj in final_data["projects"]:
            # Fuzzy match project name in case of minor LLM paraphrasing
            match_key = next(
                (k for k in proj_bullet_map if k == proj["name"] or
                 proj["name"].lower() in k.lower() or k.lower() in proj["name"].lower()),
                None
            )
            if match_key:
                # Same bullet COUNT as the base resume, always: tailored text fills
                # the first N slots; if the writer returned fewer than the original
                # count, the remaining slots keep their original bullets; if it
                # returned more, the extras are dropped.
                originals = [{"text": b["text"]} for b in proj.get("bullets", [])]
                tailored = proj_bullet_map[match_key]
                n = len(originals)
                if len(tailored) != n:
                    print(f"  [finalizer] {proj['name']}: writer returned {len(tailored)} bullets, "
                          f"original has {n} — keeping original text for the difference")
                proj["bullets"] = tailored[:n] + originals[len(tailored):n]

    # 3b. Strip participial keyword tails everywhere (", demonstrating hands-on
    # proficiency with…") — deterministic, last thing before render, so no
    # LLM pass can reintroduce the pattern. See strip_filler_tail().
    _tails_stripped = 0
    for exp in final_data["experience"]:
        for b in exp.get("bullets", []):
            new = strip_filler_tail(b["text"])
            _tails_stripped += new != b["text"]
            b["text"] = new
    for proj in final_data["projects"]:
        for b in proj.get("bullets", []):
            new = strip_filler_tail(b["text"])
            _tails_stripped += new != b["text"]
            b["text"] = new
    _summary_sentences = re.split(r'(?<=[.!?])\s+', final_data["summary"].strip())
    _new_summary = " ".join(strip_filler_tail(s) for s in _summary_sentences)
    _tails_stripped += _new_summary != final_data["summary"]
    final_data["summary"] = _new_summary
    if _tails_stripped:
        print(f"  [finalizer] stripped {_tails_stripped} filler keyword tail(s)")

    # 4. Create timestamped output folder — company AND title, so multiple
    # applications to the same company for different roles land in
    # distinguishable folders instead of all being "{Company}_{timestamp}"
    # (this was a real problem: 4 same-day applications to one company were
    # impossible to tell apart afterward).
    company_safe = sanitize_filename_component(state.get("company_name"), "Unknown_Company")
    title_safe = sanitize_filename_component(state.get("job_title"), "Role")
    timestamp = datetime.datetime.now().strftime("%d_%m_%H_%M")
    folder_name = f"{company_safe}_{title_safe}_{timestamp}"
    if state.get("generate_cover_letter"):
        folder_name += "_CL"

    output_folder = os.path.join(RESUME_OUTPUT_PATH, folder_name)
    os.makedirs(output_folder, exist_ok=True)

    # 5. Save JSON snapshot (with warning header if trust check failed)
    trust_report = state.get("trust_report") or {}
    if trust_report and not trust_report.get("passed", True):
        final_data["_VALIDATION_WARNING"] = {
            "trust_score": trust_report["trust_score"],
            "flagged_bullet_count": len(trust_report.get("flagged_bullets", [])),
            "message": "Pre-editor validation failed. Review trust_report.json before sending."
        }

    temp_json_path = os.path.join(output_folder, "temp_data.json")
    with open(temp_json_path, "w") as f:
        json.dump(final_data, f, indent=2)

    # 6. IMPROVEMENT 1: keyword_report.json
    keyword_report = {
        "verified_keywords": state.get("verified_keywords", []),
        "gap_keywords": state.get("gap_keywords", []),
    }
    with open(os.path.join(output_folder, "keyword_report.json"), "w") as f:
        json.dump(keyword_report, f, indent=2)

    # 7. IMPROVEMENT 2: selection_report.json
    selection_report = state.get("selection_report") or {}
    with open(os.path.join(output_folder, "selection_report.json"), "w") as f:
        json.dump(selection_report, f, indent=2)

    # 8. IMPROVEMENT 4: trust_report.json
    with open(os.path.join(output_folder, "trust_report.json"), "w") as f:
        json.dump(trust_report, f, indent=2)

    # strategy.json — used by cover letter pipeline
    resume_strategy = state.get("resume_strategy") or {}
    with open(os.path.join(output_folder, "strategy.json"), "w") as f:
        json.dump(resume_strategy, f, indent=2)

    # metadata.json — per-run status tracking for crash recovery and sheet integration
    metadata = {
        "run_id": folder_name,
        "company": state["company_name"],
        "job_title": state["job_title"],
        "triage_score": state.get("triage_score", 0),
        "output_folder": output_folder,
        "status": "completed",
        "resume_generated": state.get("generate_resume", True),
        "cover_letter_generated": state.get("generate_cover_letter", True),
        "created_at": datetime.datetime.now().isoformat(),
        "sheet_row_written": False,
        "notification_sent": False
    }
    with open(os.path.join(output_folder, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    # 9. IMPROVEMENT 6: run_summary.txt
    verified_kw = keyword_report["verified_keywords"]
    gap_kw = keyword_report["gap_keywords"]
    sel_bullets = selection_report.get("selected_bullets", [])
    dropped_bullets = selection_report.get("dropped_bullets", [])
    sel_projects = selection_report.get("selected_projects", [])
    content_warnings = selection_report.get("content_warnings", [])
    flagged_bullets = trust_report.get("flagged_bullets", [])
    summary_issues = trust_report.get("summary_issues", [])

    # Collect [METRIC NEEDED] flags from final bullets
    metric_needed_flags = [
        b.get("rewritten_text", "") for b in state["final_resume_data"].get("tailored_bullets", [])
        if "[METRIC NEEDED]" in b.get("rewritten_text", "")
    ]

    action_items = (
        [f"[METRIC NEEDED] {t}" for t in metric_needed_flags]
        + [f"[TRUST FLAG] {b['issues']} — \"{b['bullet_text'][:80]}...\"" for b in flagged_bullets]
        + [f"[SUMMARY] {issue}" for issue in summary_issues]
        + [f"[STRATEGIST WARNING] {w}" for w in content_warnings]
    )

    employers_in_selection = sorted({b["company"] for b in sel_bullets})

    lines = [
        "RESUME RUN SUMMARY",
        "==================",
        f"Company:          {state.get('company_name', 'Unknown')}",
        f"Role:             {state.get('job_title', 'Unknown')}",
        f"Date:             {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Match Score:      {state.get('triage_score', 'N/A')}/100",
        f"Trust Score:      {trust_report.get('trust_score', 'N/A')}/100",
        "",
        "KEYWORD COVERAGE",
        "----------------",
        f"Verified (included):  {', '.join(verified_kw) or 'none'}",
        f"Gaps (excluded):      {', '.join(gap_kw) or 'none'}",
        "",
        "CONTENT SELECTION",
        "-----------------",
        f"Bullets included:  {len(sel_bullets)} from {', '.join(employers_in_selection)}",
        f"Projects included: {', '.join(sel_projects)}",
        f"Bullets dropped:   {len(dropped_bullets)}",
        "",
        "FLAGS FOR REVIEW",
        "----------------",
    ]
    if action_items:
        lines.extend(action_items)
    else:
        lines.append("None — clean run.")

    lines += [
        "",
        "ACTION NEEDED BEFORE SENDING",
        "-----------------------------",
    ]
    if action_items:
        lines.extend(f"  • {item}" for item in action_items)
    else:
        lines.append("  None.")

    with open(os.path.join(output_folder, "run_summary.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")

    generate_pdfs(
        temp_json_path,
        output_folder,
        gen_resume=state.get("generate_resume", True),
        gen_cl=state.get("generate_cover_letter", True)
    )

    # Clean up checkpoint — run succeeded, no longer needed
    try:
        os.remove(_checkpoint_path)
    except Exception:
        pass

    print(f"--- PIPELINE COMPLETE: {output_folder} ---")
    return {
        "output_folder": output_folder,
        "pipeline_status": "completed"
    }


# ---------------------------------------------------------------------------
# GRAPH CONSTRUCTION
# Pipeline: should_apply → triage(keywords) → strategist → writer → editor → finalizer
# Paid (Sonnet) calls: writer only. Everything else runs on the cheap tier
# (Groq free tier / local Ollama), and the editor LLM is skipped entirely when
# the deterministic validators pass.
# ---------------------------------------------------------------------------
workflow = StateGraph(ResumeGraphState)

workflow.add_node("should_apply", should_apply_node)
workflow.add_node("triage", triage_node)
workflow.add_node("strategist", strategist_node)
workflow.add_node("writer", writer_node)
workflow.add_node("editor", editor_node)
workflow.add_node("finalizer", finalizer_node)

workflow.set_entry_point("should_apply")

workflow.add_conditional_edges(
    "should_apply",
    route_after_should_apply,
    {
        "triage": "triage",
        END: END
    }
)

workflow.add_edge("triage", "strategist")
workflow.add_edge("strategist", "writer")
workflow.add_edge("writer", "editor")
workflow.add_edge("editor", "finalizer")
workflow.add_edge("finalizer", END)

app = workflow.compile()


# ---------------------------------------------------------------------------
# TEST BLOCK
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    master_data_path = os.path.join(ROOT_DIR, "Ankush_Master_Data.json")

    with open(master_data_path, "r") as f:
        master_data = json.load(f)

    test_jd = """
    Looking for a Full Stack Software Engineer.
    Must have 3+ years of experience with Python, Angular, and RESTful APIs.
    Experience with CI/CD pipelines (GitLab) and mobile development is a huge plus.
    """

    initial_state = {
        "company_name": "NVIDIA",
        "job_title": "Software Engineer",
        "job_description": test_jd,
        "master_data": master_data,
        "generate_resume": True,
        "generate_cover_letter": True,
        "jd_keywords": [],
        "verified_keywords": [],
        "gap_keywords": [],
        "should_apply": None,
        "triage_score": None,
        "triage_reasoning": None,
        "proceed": False,
        "selected_content": None,
        "selection_report": None,
        "trust_report": None,
        "final_resume_data": None,
        "resume_strategy": None,
        "output_folder": "",
        "pipeline_status": ""
    }

    print("--- STARTING AUTO-RESUME PIPELINE ---")
    result = app.invoke(initial_state)
    print(f"Status: {result.get('pipeline_status')}")
    print(f"Output: {result.get('output_folder')}")
