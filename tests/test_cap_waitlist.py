"""
tests/test_cap_waitlist.py

A scraped job that cleared the score bar but missed the daily_cap cut is
written to Applications as a hidden waitlist entry (cap_missed=True) —
never discarded. The server-side entry builder must carry the flag, and a
normal (kept) job must come through with cap_missed=False.

Run: pytest tests/test_cap_waitlist.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import server


def _job(**extra):
    return {"company": "Acme", "title": "SWE", "description": "d" * 300, "apply_link": "https://x/1",
            "score": 80, "reasoning": "r", "rubric_version": 2, **extra}


def test_entry_builder_carries_cap_missed_flag():
    assert server._new_entry_from_scraped_job(_job(cap_missed=True))["cap_missed"] is True


def test_entry_builder_defaults_cap_missed_false():
    assert server._new_entry_from_scraped_job(_job())["cap_missed"] is False
