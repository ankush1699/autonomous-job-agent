"""
core/profile_cache.py

Generates a <=500-token plain-text summary of Ankush_Master_Data.json and
writes it to profile_cache.txt in the repo root.

Usage:
    python -m core.profile_cache --regenerate   # rebuild cache from master data
    from core.profile_cache import load_profile_cache  # load in code
"""

import json
import os
import argparse

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MASTER_DATA_PATH = os.path.join(ROOT_DIR, "Ankush_Master_Data.json")
CACHE_PATH = os.path.join(ROOT_DIR, "profile_cache.txt")


def generate_profile_cache(master_data: dict) -> str:
    """
    Produce a compact, structured text summary from master data.
    Designed to fit within ~500 tokens while covering all scoring-relevant facts.
    """
    p = master_data.get("personal_info", {})
    # `availability` is optional (removed from master data Sep 2026 — a start
    # date is meaningless once the candidate is already on the market). No
    # default: the old "July 2026" fallback silently resurrected a stale date
    # whenever the field was absent. If present, "F-1 OPT | Available X" is
    # reduced to "X"; if absent, the sentence is simply omitted.
    availability_raw = (p.get("availability") or "").strip()
    available_from = availability_raw.split("Available")[-1].strip() if "Available" in availability_raw else availability_raw
    visa_line = "Visa: F-1 OPT. Authorized to work in the US immediately. Requires H-1B sponsorship for long-term employment."
    if available_from:
        visa_line += f" Available {available_from}."

    lines = [
        "CANDIDATE PROFILE CACHE",
        "=======================",
        visa_line,
        "",
        "EXPERIENCE",
        "----------",
    ]

    for exp in master_data.get("experience", []):
        techs: set = set()
        for b in exp.get("bullets", []):
            techs.update(b.get("technologies_used", []))

        tech_str = ", ".join(sorted(techs)) if techs else ""
        lines.append(
            f"[{exp['duration']}] {exp['title']} @ {exp['company']} — {exp['location']}"
        )
        if tech_str:
            lines.append(f"  Stack: {tech_str}")

        # Show up to 2 quantified bullets per employer — enough context for scoring
        quantified = [b for b in exp.get("bullets", []) if b.get("is_quantified")]
        unquantified = [b for b in exp.get("bullets", []) if not b.get("is_quantified")]
        shown = (quantified + unquantified)[:2]
        for b in shown:
            text = b["text"]
            if len(text) > 95:
                text = text[:92] + "..."
            lines.append(f"  • {text}")

    lines += ["", "EDUCATION", "---------"]
    for edu in master_data.get("education", []):
        gpa = edu.get("gpa") or edu.get("cgpa", "N/A")
        status = "Completed" if edu.get("graduation_status") == "completed" else "In progress"
        lines.append(
            f"- {edu['degree']}, {edu['university']}, GPA/CGPA: {gpa} ({edu['duration']}) — {status}"
        )
        courses = edu.get("coursework", [])
        if courses:
            lines.append(f"  Coursework: {', '.join(courses[:3])}")

    lines += ["", "SKILLS", "------"]
    for category, skills in master_data.get("technical_skills", {}).items():
        lines.append(f"{category}: {', '.join(skills)}")

    return "\n".join(lines)


def write_cache(master_data: dict | None = None) -> str:
    """Generate and write profile_cache.txt. Returns the generated text."""
    if master_data is None:
        with open(MASTER_DATA_PATH, "r") as f:
            master_data = json.load(f)
    text = generate_profile_cache(master_data)
    with open(CACHE_PATH, "w") as f:
        f.write(text)
    return text


def load_profile_cache() -> str:
    """
    Load profile_cache.txt from the repo root.
    If the file doesn't exist, regenerates it on the fly.
    """
    if not os.path.exists(CACHE_PATH):
        print("[profile_cache] Cache not found — regenerating from master data...")
        return write_cache()
    with open(CACHE_PATH, "r") as f:
        return f.read()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Profile cache generator")
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="Rebuild profile_cache.txt from Ankush_Master_Data.json",
    )
    args = parser.parse_args()

    if args.regenerate:
        text = write_cache()
        print(f"[profile_cache] Written to {CACHE_PATH}")
        print(f"[profile_cache] Approximate chars: {len(text)} (~{len(text)//4} tokens)")
        print("\n--- PREVIEW ---")
        print(text)
    else:
        parser.print_help()
