#!/usr/bin/env python3
"""
apply.py — single CLI for all three entry modes over one core engine.

  python apply.py score    --jd-file jd.txt                     # free verdict only
  python apply.py manual   --company "Stripe" --title "SWE" --jd-file jd.txt [--cover-letter]
  python apply.py scrape   [--daemon]                           # SerpApi/jobspy cycle
  python apply.py autofill --url <application url> --folder <run output folder>
  python apply.py cache-stats                                   # JD cache summary

All modes share: the should-apply gate (regex red flags → JD cache → cheap-tier
LLM), the JD cache (a posting is never reprocessed twice), and the tailoring
graph (writer on Claude Sonnet; everything else on Groq free tier / Ollama).
"""

import os
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
for p in (ROOT, os.path.join(ROOT, "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))


def _read_jd(args) -> str:
    if getattr(args, "jd_file", None):
        with open(args.jd_file) as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print("Paste the job description, then press Ctrl+D:")
    return sys.stdin.read()


def cmd_score(args):
    from core.should_apply import evaluate, MIN_SCORE
    jd = _read_jd(args)
    verdict = evaluate(jd, company=args.company or "", title=args.title or "")
    print(json.dumps(verdict, indent=2))
    print(f"\n{'APPLY' if verdict['proceed'] else 'SKIP'} "
          f"(score {verdict['score']}/100, threshold {MIN_SCORE}, stage: {verdict['stage']})")


def cmd_manual(args):
    from modes.manual import run
    jd = _read_jd(args)
    run(
        company=args.company,
        title=args.title,
        jd=jd,
        generate_resume=not args.no_resume,
        generate_cover_letter=args.cover_letter,
    )


def cmd_scrape(args):
    from modes import scraper
    if args.daemon:
        scraper.run_daemon()
    else:
        scraper.run_once()


def cmd_autofill(args):
    from modes.autofill import run
    run(url=args.url, folder=args.folder)


def cmd_cache_stats(args):
    from core.jd_cache import stats
    print(json.dumps(stats(), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("score", help="Should-apply verdict only (free path, no tailoring)")
    p.add_argument("--jd-file")
    p.add_argument("--company", default="")
    p.add_argument("--title", default="")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("manual", help="Paste company + JD, run the full pipeline")
    p.add_argument("--company", required=True)
    p.add_argument("--title", default="Software Engineer")
    p.add_argument("--jd-file")
    p.add_argument("--cover-letter", action="store_true")
    p.add_argument("--no-resume", action="store_true")
    p.set_defaults(func=cmd_manual)

    p = sub.add_parser("scrape", help="Run one scrape cycle (or --daemon for the scheduler)")
    p.add_argument("--daemon", action="store_true")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("autofill", help="Playwright form fill — stops before submit")
    p.add_argument("--url", required=True)
    p.add_argument("--folder", help="Pipeline run folder containing the tailored PDFs")
    p.set_defaults(func=cmd_autofill)

    p = sub.add_parser("cache-stats", help="JD cache summary")
    p.set_defaults(func=cmd_cache_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
