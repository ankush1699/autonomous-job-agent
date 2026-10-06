"""
tests/test_score_entry_throttle.py

Regression test for a real gap: adding several JDs at once (or clicking
"Retry all failed") spawned one thread per entry that called the should_apply
LLM immediately — unlike scraper/score.py, which deliberately scores
sequentially with a delay ("never parallel") specifically to avoid tripping
provider rate limits. server._score_entry now goes through a shared lock +
minimum interval so the web UI has the same discipline.

Patches server._SCORE_MIN_INTERVAL down for test speed and asserts:
  1. No two should_apply calls ever run concurrently (the lock actually locks).
  2. Consecutive calls are spaced at least _SCORE_MIN_INTERVAL apart.

Run: pytest tests/test_score_entry_throttle.py -v
"""
import os
import sys
import time
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("APP_PASSWORD", "test-only")

import server  # noqa: E402


def test_concurrent_score_entry_calls_are_serialized_and_spaced():
    call_log = []  # (start, end) tuples
    concurrency_counter = {"active": 0, "max_seen": 0}
    lock = threading.Lock()

    def fake_evaluate(jd, company="", title=""):
        with lock:
            concurrency_counter["active"] += 1
            concurrency_counter["max_seen"] = max(
                concurrency_counter["max_seen"], concurrency_counter["active"]
            )
        start = time.monotonic()
        time.sleep(0.02)  # simulate LLM latency
        end = time.monotonic()
        with lock:
            concurrency_counter["active"] -= 1
        call_log.append((start, end))
        return {"score": 80, "proceed": True, "stage": "llm", "reasoning": "",
                "red_flags": []}

    entry_ids = [f"entry{i}" for i in range(4)]
    for eid in entry_ids:
        server.ENTRIES[eid] = {
            "id": eid, "company": "Co", "title": "SWE", "jd": "x" * 300,
            "score_status": "scoring",
        }

    with patch("server._SCORE_MIN_INTERVAL", 0.05), \
         patch("core.should_apply.evaluate", side_effect=fake_evaluate), \
         patch("core.entries_store.save"):
        threads = [threading.Thread(target=server._score_entry, args=(eid,)) for eid in entry_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    for eid in entry_ids:
        del server.ENTRIES[eid]

    assert len(call_log) == 4
    assert concurrency_counter["max_seen"] == 1, (
        "two should_apply calls ran at the same time — the lock did not serialize them"
    )

    call_log.sort(key=lambda pair: pair[0])
    gaps = [call_log[i + 1][0] - call_log[i][0] for i in range(len(call_log) - 1)]
    assert all(gap >= 0.05 - 0.01 for gap in gaps), f"calls fired closer together than the minimum interval: {gaps}"
