"""
scraper/pipeline_trigger.py

Triggers the LangGraph resume pipeline for a list of high-scoring jobs.
Creates output folders, writes job.json, calls app.invoke() sequentially.

Rules:
- Never run resume generation in parallel. Always sequential with 5-second delay.
- Never crash the scheduler on a single job failure. Catch all exceptions per job.
- metric_source and is_verified fields must NOT be auto-populated.
"""

import os
import sys
import json
import time
import datetime

from scraper.seen_jobs import mark_seen

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_ENGINE_DIR = os.path.join(_REPO_ROOT, "engine")
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

_OUTPUT_BASE = os.getenv("OUTPUT_BASE_PATH", os.path.expanduser("~/Documents/Resumes"))
_MASTER_DATA_PATH = os.path.join(_REPO_ROOT, "Ankush_Master_Data.json")
_DELAY_BETWEEN_JOBS = 5  # seconds — never parallel


def _load_master_data() -> dict:
    with open(_MASTER_DATA_PATH, "r") as f:
        return json.load(f)


def _make_folder_name(company: str, job_title: str) -> str:
    """Unique folder name: Company_DDMMYY_HHmm"""
    now = datetime.datetime.now()
    safe_company = "".join(c if c.isalnum() else "_" for c in company)[:20]
    return f"{safe_company}_{now.strftime('%d_%m_%H_%M')}"


def _build_initial_state(job: dict, master_data: dict) -> dict:
    return {
        "company_name": job["company"],
        "job_title": job["title"],
        "job_description": job["description"],
        "master_data": master_data,
        "generate_resume": True,
        "generate_cover_letter": False,  # CL generated separately via cover_letter/ module
        "one_page": False,
        "jd_keywords": [],
        "verified_keywords": [],
        "gap_keywords": [],
        "triage_score": None,
        "triage_reasoning": None,
        "proceed": False,
        "selected_content": None,
        "selection_report": None,
        "trust_report": None,
        "final_resume_data": None,
        "resume_strategy": None,
        "output_folder": "",
        "pipeline_status": "",
    }


def trigger_for_jobs(jobs: list[dict]) -> list[dict]:
    """
    Run the resume pipeline for each job in the list sequentially.

    Args:
        jobs: List of scored, filtered job dicts (should all have score >= threshold).

    Returns:
        List of result dicts with keys: job_id, company, title, score,
        output_folder, status, error (if any).
    """
    if not jobs:
        print("  [trigger] No jobs to process.")
        return []

    # Import here to avoid circular import and to defer heavy LangGraph setup
    from graph import app  # noqa: E402

    master_data = _load_master_data()
    results = []

    for i, job in enumerate(jobs):
        job_id = job.get("id", f"job_{i}")
        company = job.get("company", "Unknown")
        title = job.get("title", "Unknown")

        print(f"\n  [trigger] Job {i+1}/{len(jobs)}: {company} — {title} (score={job.get('score', '?')})")

        result_entry = {
            "job_id": job_id,
            "company": company,
            "title": title,
            "score": job.get("score", 0),
            "apply_link": job.get("apply_link", ""),
            "output_folder": "",
            "status": "failed",
            "error": None,
        }

        try:
            initial_state = _build_initial_state(job, master_data)
            result = app.invoke(initial_state)

            output_folder = result.get("output_folder", "")
            status = result.get("pipeline_status", "unknown")

            # Write job.json into the output folder for reference
            if output_folder and os.path.isdir(output_folder):
                job_json_path = os.path.join(output_folder, "job.json")
                with open(job_json_path, "w") as f:
                    # Do NOT write metric_source or is_verified — user fills these
                    safe_job = {k: v for k, v in job.items()
                                if k not in ("metric_source", "is_verified")}
                    json.dump(safe_job, f, indent=2)

                # Update metadata.json with apply_link for sheet integration
                metadata_path = os.path.join(output_folder, "metadata.json")
                if os.path.exists(metadata_path):
                    with open(metadata_path, "r") as f:
                        metadata = json.load(f)
                    metadata["apply_link"] = job.get("apply_link", "")
                    metadata["platform"] = job.get("platform", "")
                    metadata["jd_summary"] = job.get("jd_summary", "")
                    metadata["reasoning"] = job.get("reasoning", "")
                    with open(metadata_path, "w") as f:
                        json.dump(metadata, f, indent=2)

                # Mark URL as seen now that folder + metadata are confirmed written
                apply_link = job.get("apply_link", "")
                if apply_link:
                    mark_seen(apply_link)

            result_entry["output_folder"] = output_folder
            result_entry["status"] = status
            print(f"  [trigger] Done: {output_folder}")

        except Exception as e:
            result_entry["error"] = str(e)
            print(f"  [trigger] ERROR processing {company} — {title}: {e}")
            # Never crash the scheduler; log and continue

        results.append(result_entry)

        if i < len(jobs) - 1:
            print(f"  [trigger] Waiting {_DELAY_BETWEEN_JOBS}s before next job...")
            time.sleep(_DELAY_BETWEEN_JOBS)

    successful = sum(1 for r in results if r["status"] == "completed")
    print(f"\n  [trigger] Completed {successful}/{len(jobs)} jobs successfully.")
    return results
