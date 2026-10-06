"""
cover_letter/cl_writer.py

Standalone cover letter writer. Reads strategy.json + job.json from a run folder
and generates a cover letter using Claude Sonnet, then saves it as a .tex file.

Uses the existing LaTeX cover letter template from the pipeline.
Intentionally does NOT re-run the full resume pipeline.
"""

import os
import sys
import json
import time
from typing import Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_ENGINE_DIR = os.path.join(_REPO_ROOT, "engine")
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field
from core.profile_cache import load_profile_cache


class CoverLetterOutput(BaseModel):
    paragraphs: list[str] = Field(
        description="4-5 cover letter body paragraphs. Each item is one paragraph. No header, no sign-off."
    )


_llm = ChatAnthropic(
    model="claude-sonnet-4-6",
    temperature=0.3,
    max_tokens=4096,
)


def _load_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def generate_cover_letter(run_folder: str) -> Optional[list[str]]:
    """
    Generate cover letter paragraphs for a completed pipeline run.

    Reads strategy.json and job.json from run_folder.
    Returns list of paragraph strings, or None on failure.
    """
    strategy = _load_json(os.path.join(run_folder, "strategy.json"))
    job = _load_json(os.path.join(run_folder, "job.json"))
    temp_data = _load_json(os.path.join(run_folder, "temp_data.json"))

    if not strategy or not job:
        print(f"  [cl_writer] Missing strategy.json or job.json in {run_folder}")
        return None

    company = job.get("company", "the company")
    title = job.get("title", "the role")
    jd = job.get("description", "")
    profile = load_profile_cache()

    # Pull existing resume summary for context if available
    resume_summary = ""
    if temp_data and temp_data.get("summary"):
        resume_summary = f"\nCANDIDATE'S RESUME SUMMARY:\n{temp_data['summary']}"

    positioning = strategy.get("positioning_angle", "")
    themes = strategy.get("key_themes", [])
    keywords = strategy.get("keywords_to_emphasize", [])
    hooks = strategy.get("company_research_hooks", [])
    tone = strategy.get("tone_guidance", "")
    pitch = strategy.get("tailored_pitch", "")

    structured_llm = _llm.with_structured_output(CoverLetterOutput)

    prompt = f"""You are an expert cover letter writer. Write a tailored, authentic 4-5 paragraph cover letter body for the following application.

ROLE: {title} at {company}

POSITIONING ANGLE: {positioning}

KEY THEMES TO WEAVE IN: {themes}

COMPANY HOOKS (use at least one to open): {hooks}

TONE GUIDANCE: {tone}

CORE PITCH: {pitch}

ATS KEYWORDS TO INCLUDE: {keywords}

CANDIDATE PROFILE:
{profile}
{resume_summary}

JOB DESCRIPTION (first 1200 chars):
{jd[:1200]}

RULES:
1. 4-5 distinct paragraphs. One string per paragraph in 'paragraphs' list.
2. Open with a genuine, specific observation about {company} (use a hook from the list above).
3. Do NOT open with: "I am writing to apply", "I am thrilled", "passionate about", "fast-paced".
4. Each paragraph adds NEW information — no paragraph merely restates the resume.
5. Weave in at least 5 of the ATS keywords naturally.
6. Body only — no "Dear Hiring Team" header, no "Sincerely" sign-off.
7. Professional, confident, human. Not stiff or generic."""

    try:
        result = structured_llm.invoke(prompt)
        return result.paragraphs
    except Exception as e:
        print(f"  [cl_writer] LLM error: {e}")
        return None


def save_cover_letter(run_folder: str, paragraphs: list[str]) -> Optional[str]:
    """
    Save cover letter paragraphs back to temp_data.json so pdf_generator can render it.
    Returns the path to temp_data.json on success.
    """
    temp_data_path = os.path.join(run_folder, "temp_data.json")
    if not os.path.exists(temp_data_path):
        print(f"  [cl_writer] temp_data.json not found in {run_folder}")
        return None

    with open(temp_data_path, "r") as f:
        temp_data = json.load(f)

    temp_data["cover_letter_paragraphs"] = paragraphs

    with open(temp_data_path, "w") as f:
        json.dump(temp_data, f, indent=2)

    return temp_data_path


def generate_and_save(run_folder: str) -> bool:
    """
    Full flow: generate cover letter and save to temp_data.json, then regenerate PDF.

    Returns True on success.
    """
    paragraphs = generate_cover_letter(run_folder)
    if not paragraphs:
        return False

    temp_data_path = save_cover_letter(run_folder, paragraphs)
    if not temp_data_path:
        return False

    # Regenerate the cover letter PDF only
    try:
        from pdf_generator import generate_pdfs
        generate_pdfs(temp_data_path, run_folder, gen_resume=False, gen_cl=True)
        print(f"  [cl_writer] Cover letter PDF generated: {run_folder}")
        return True
    except Exception as e:
        print(f"  [cl_writer] PDF generation error: {e}")
        return False
