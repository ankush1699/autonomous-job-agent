"""
scraper/notify.py

Sends Telegram notifications for high-scoring job matches.

Uses the Telegram Bot API directly via requests (no extra dependency).
Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.

Only called for jobs with score >= MIN_ALIGNMENT_SCORE (default 75).
"""

import os
import sys
import requests


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# The apply threshold is NOT read here — core.should_apply.min_score() is the
# single source (UI-editable, env-defaulted); see notify_high_matches().
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _format_message(job: dict, output_folder: str = "", pipeline_failed: bool = False) -> str:
    score = job.get("score", 0)
    company = job.get("company", "Unknown")
    title = job.get("title", "Unknown")
    location = job.get("location", "")
    remote = "Remote" if job.get("is_remote") else location
    platform = job.get("platform", "").title()
    apply_link = job.get("apply_link", "")
    reasoning = job.get("reasoning", "")
    seniority = job.get("seniority_level", "")
    role_type = job.get("role_type", "")

    header = "Resume Ready" if output_folder else ("Match Found" if not pipeline_failed else "Match Found")
    emoji = "✅" if output_folder else ("⚠️" if pipeline_failed else "🎯")

    lines = [
        f"{emoji} *{header}* — Score: {score}/100",
        f"*{company}* — {title}",
        f"📍 {remote} | 🔗 {platform}",
    ]
    if seniority or role_type:
        meta = " | ".join(filter(None, [seniority, role_type]))
        lines.append(f"_{meta}_")
    if reasoning:
        lines.append(f"{reasoning}")

    if output_folder:
        folder_name = os.path.basename(output_folder)
        lines.append(f"📄 Resume: `{folder_name}`")
    elif pipeline_failed:
        lines.append("📄 Resume: generation failed — check logs")

    if apply_link:
        lines.append(f"[Apply here]({apply_link})")

    return "\n".join(lines)


def send_notification(job: dict, output_folder: str = "", pipeline_failed: bool = False) -> bool:
    """
    Send a Telegram notification for a single job.
    Call after the pipeline completes so the message includes the resume folder.

    Returns True on success, False on failure.
    Does NOT raise — caller must handle silently.
    """
    if not _BOT_TOKEN or not _CHAT_ID:
        print("  [notify] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping.")
        return False

    message = _format_message(job, output_folder=output_folder, pipeline_failed=pipeline_failed)
    url = _TELEGRAM_API.format(token=_BOT_TOKEN)

    try:
        response = requests.post(
            url,
            json={
                "chat_id": _CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
        if response.status_code == 200:
            return True
        else:
            print(f"  [notify] Telegram API error {response.status_code}: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"  [notify] Request failed: {e}")
        return False


def notify_high_matches(jobs: list[dict], min_score: int | None = None) -> int:
    """
    Send notifications for all jobs meeting the score threshold.

    Args:
        jobs: Scored job dicts.
        min_score: Minimum score to trigger notification. Defaults to the
            effective apply threshold (core.should_apply.min_score()).

    Returns:
        Number of notifications successfully sent.
    """
    if min_score is None:
        from core.should_apply import min_score as core_min_score  # lazy: keeps this module light
        min_score = core_min_score()
    eligible = [j for j in jobs if j.get("score", 0) >= min_score]
    if not eligible:
        print(f"  [notify] No jobs >= {min_score} — nothing to notify.")
        return 0

    sent = 0
    for job in eligible:
        if send_notification(job):
            sent += 1
            print(f"  [notify] Sent: {job['company']} — {job['title']} ({job['score']})")
        else:
            print(f"  [notify] Failed: {job['company']} — {job['title']}")

    print(f"  [notify] {sent}/{len(eligible)} notifications sent.")
    return sent
