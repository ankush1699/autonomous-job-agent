"""
Repo-wide pytest hooks.

Tests marked @pytest.mark.personal_data (or a module-level
`pytestmark = pytest.mark.personal_data`) assert facts about the maintainer's
REAL master data (specific employers, bullets, skills). On a fresh clone the
project runs on the fictional Ankush_Master_Data.example.json (see README),
where those assertions can't hold, so they are skipped there instead of failing.
Everything else is pure logic and runs against any valid master data.
"""
import json
import os

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))

# engine/graph.py refuses to import without ANTHROPIC_API_KEY and builds its model clients at import
# time (no network call). The suite is deterministic logic only and must NEVER make a paid LLM call,
# so it always runs with a placeholder key: a fresh clone needs no .env, and a real key in your .env
# is never used by tests (load_dotenv() does not override a variable that is already set).
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-ci-placeholder")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "personal_data: asserts facts about the maintainer's real master data; skipped when the sample data is in use",
    )


def _using_sample_data() -> bool:
    try:
        with open(os.path.join(ROOT, "Ankush_Master_Data.json")) as f:
            return bool(json.load(f).get("_example"))
    except (OSError, json.JSONDecodeError):
        return True


def pytest_collection_modifyitems(config, items):
    if not _using_sample_data():
        return
    skip = pytest.mark.skip(reason="needs the maintainer's real master data; the sample data is in use")
    for item in items:
        if "personal_data" in item.keywords:
            item.add_marker(skip)
