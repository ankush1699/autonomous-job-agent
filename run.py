"""
run.py — CLI entrypoint for the resume pipeline.
Used by GitHub Actions (or any terminal) to run the pipeline via environment variables.

Required env vars:
    ANTHROPIC_API_KEY
    COMPANY_NAME
    JOB_DESCRIPTION

Optional env vars:
    GROQ_API_KEY           free-tier cheap model (see core/llm.py); falls back to
                           paid Claude Haiku if unset
    JOB_TITLE              (default: "Software Engineer")
    GENERATE_RESUME        (default: "true")
    GENERATE_COVER_LETTER  (default: "false")

Output:
    PDFs are written to ./output/ for artifact upload.
"""

import os
import json
import shutil
import sys

# Allow imports from engine/
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT_DIR, "engine"))

from graph import app


def _bool_env(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in ("true", "1", "yes")


def main():
    # ── Validate required inputs ───────────────────────────────────────────────
    missing = [k for k in ("ANTHROPIC_API_KEY", "COMPANY_NAME", "JOB_DESCRIPTION")
               if not os.environ.get(k)]
    if missing:
        for k in missing:
            print(f"ERROR: {k} environment variable is required.")
        sys.exit(1)

    # ── Load master data ───────────────────────────────────────────────────────
    master_data_path = os.path.join(ROOT_DIR, "Ankush_Master_Data.json")
    if not os.path.exists(master_data_path):
        print("ERROR: Ankush_Master_Data.json not found in repo root.")
        sys.exit(1)

    with open(master_data_path) as f:
        master_data = json.load(f)

    # ── Build initial state ────────────────────────────────────────────────────
    initial_state = {
        "company_name":          os.environ["COMPANY_NAME"],
        "job_title":             os.environ.get("JOB_TITLE", "Software Engineer"),
        "job_description":       os.environ["JOB_DESCRIPTION"],
        "master_data":           master_data,
        "generate_resume":       _bool_env("GENERATE_RESUME", True),
        "generate_cover_letter": _bool_env("GENERATE_COVER_LETTER", False),
        "one_page":          False,
        "jd_keywords":       [],
        "verified_keywords": [],
        "gap_keywords":      [],
        "should_apply":      None,
        "triage_score":      None,
        "triage_reasoning":  None,
        "proceed":           False,
        "selected_content":  None,
        "selection_report":  None,
        "trust_report":      None,
        "final_resume_data": None,
        "resume_strategy":   None,
        "output_folder":     "",
        "pipeline_status":   "",
    }

    company   = initial_state["company_name"]
    gen_resume = initial_state["generate_resume"]
    gen_cl     = initial_state["generate_cover_letter"]
    docs = "resume" + (" + cover letter" if gen_cl else "")
    print(f"\nGenerating {docs} for {company}...")

    # ── Run pipeline ───────────────────────────────────────────────────────────
    result = app.invoke(initial_state)

    # ── Handle triage skip ─────────────────────────────────────────────────────
    if not result.get("proceed"):
        score = result.get("triage_score", "N/A")
        print(f"\nPipeline skipped — triage score {score}/100 (below 70).")
        print("No documents generated. Review the JD fit before re-submitting.")
        sys.exit(0)

    # ── Copy output to ./output/ for artifact upload ───────────────────────────
    output_folder = result.get("output_folder", "")
    if result.get("pipeline_status") == "completed" and output_folder:
        dest = os.path.join(ROOT_DIR, "output")
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(output_folder, dest)
        print(f"\nDone. PDFs available in ./output/")

        # Print run_summary for the Actions log
        summary_path = os.path.join(dest, "run_summary.txt")
        if os.path.exists(summary_path):
            print("\n" + "─" * 50)
            with open(summary_path) as f:
                print(f.read())
            print("─" * 50)
    else:
        print(f"\nPipeline ended with status: {result.get('pipeline_status')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
