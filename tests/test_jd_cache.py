"""
tests/test_jd_cache.py

core/jd_cache.py is what makes "never reprocess the same posting twice" true
across scraper/manual/autofill entry points — it only works if the content
hash actually normalizes consistently and merges don't clobber prior fields.

Uses a temp OUTPUT_BASE_PATH per test so this never touches real cache data.

Run: pytest tests/test_jd_cache.py -v
"""
import os
import sys
import importlib
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _fresh_jd_cache(tmp_path):
    """Reimport jd_cache with OUTPUT_BASE_PATH pointed at a temp dir, so tests
    never read/write the real ~/Documents/Resumes/jd_cache.json."""
    os.environ["OUTPUT_BASE_PATH"] = str(tmp_path)
    import core.jd_cache as jd_cache
    return importlib.reload(jd_cache)


def test_hash_is_stable_across_case_and_whitespace(tmp_path):
    jd_cache = _fresh_jd_cache(tmp_path)
    jd1 = "Senior Python Engineer at Foo.\nMust know   Django."
    jd2 = "senior python engineer at foo. must know django."
    assert jd_cache.jd_hash(jd1) == jd_cache.jd_hash(jd2)


def test_different_content_hashes_differently(tmp_path):
    jd_cache = _fresh_jd_cache(tmp_path)
    assert jd_cache.jd_hash("Backend Engineer role") != jd_cache.jd_hash("Frontend Engineer role")


def test_update_entry_creates_and_merges_without_clobbering(tmp_path):
    jd_cache = _fresh_jd_cache(tmp_path)
    jd = "Staff Engineer at Acme."

    jd_cache.update_entry(jd, company="Acme", keywords=["Python", "Django"])
    entry = jd_cache.get_entry(jd)
    assert entry["keywords"] == ["Python", "Django"]

    # A second update with a different field must not erase the first one.
    jd_cache.update_entry(jd, should_apply={"score": 55, "proceed": False})
    entry = jd_cache.get_entry(jd)
    assert entry["keywords"] == ["Python", "Django"]
    assert entry["should_apply"]["score"] == 55


def test_get_entry_returns_none_for_unseen_jd(tmp_path):
    jd_cache = _fresh_jd_cache(tmp_path)
    assert jd_cache.get_entry("Never seen this posting before.") is None


def test_stats_reflects_pass_and_reject_counts(tmp_path):
    jd_cache = _fresh_jd_cache(tmp_path)
    jd_cache.update_entry("Job A", should_apply={"proceed": True})
    jd_cache.update_entry("Job B", should_apply={"proceed": False})
    s = jd_cache.stats()
    assert s["entries"] == 2
    assert s["passed"] == 1
    assert s["rejected"] == 1


def test_concurrent_writes_do_not_clobber_each_other(tmp_path):
    """
    Regression test for a real, live data-loss bug: a scrape cycle fires
    many should_apply calls back-to-back, each calling update_entry() for a
    DIFFERENT JD. Without locking, two near-simultaneous calls can both load
    the same pre-write snapshot of the cache file, then each save their own
    version — whichever saves last wins, silently discarding every entry
    the other call had just added. This is exactly what caused a real
    cache to drop from ~282 entries to 6 overnight. 50 threads each writing
    a distinct JD must all survive.
    """
    jd_cache = _fresh_jd_cache(tmp_path)
    n = 50

    def write(i):
        jd_cache.update_entry(f"Distinct job posting number {i}", company=f"Co{i}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert jd_cache.stats()["entries"] == n
