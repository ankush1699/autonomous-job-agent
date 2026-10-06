"""
tests/test_email_overrides.py

Covers core/email_overrides.py — the persisted company-name -> email swap
("every Microsoft resume should use ankushchaudhary.microsoft@gmail.com
instead of the default") — and server.py's CRUD endpoints over it.

Run: pytest tests/test_email_overrides.py -v
"""
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import email_overrides


def _isolated_output_dir():
    tmp = tempfile.mkdtemp()
    return patch.dict(os.environ, {"OUTPUT_BASE_PATH": tmp})


def test_no_file_yields_empty_list_and_no_override():
    with _isolated_output_dir():
        assert email_overrides.load() == []
        assert email_overrides.get_email_override("Microsoft") is None


def test_save_then_load_roundtrips():
    with _isolated_output_dir():
        email_overrides.save([{"pattern": "microsoft", "email": "a@b.com"}])
        assert email_overrides.load() == [{"pattern": "microsoft", "email": "a@b.com"}]


def test_case_insensitive_substring_match():
    with _isolated_output_dir():
        email_overrides.save([{"pattern": "microsoft", "email": "ankushchaudhary.microsoft@gmail.com"}])
        assert email_overrides.get_email_override("Microsoft") == "ankushchaudhary.microsoft@gmail.com"
        assert email_overrides.get_email_override("Microsoft Corporation") == "ankushchaudhary.microsoft@gmail.com"
        assert email_overrides.get_email_override("MICROSOFT - Redmond") == "ankushchaudhary.microsoft@gmail.com"


def test_no_match_returns_none():
    with _isolated_output_dir():
        email_overrides.save([{"pattern": "microsoft", "email": "a@b.com"}])
        assert email_overrides.get_email_override("Google") is None


def test_first_matching_rule_wins():
    with _isolated_output_dir():
        email_overrides.save([
            {"pattern": "microsoft", "email": "specific@b.com"},
            {"pattern": "soft", "email": "generic@b.com"},
        ])
        assert email_overrides.get_email_override("Microsoft") == "specific@b.com"


# ---------------------------------------------------------------------------
# server.py CRUD endpoints
# ---------------------------------------------------------------------------
os.environ.setdefault("APP_PASSWORD", "test-only")
import server  # noqa: E402


def test_add_and_list_email_override():
    with _isolated_output_dir():
        result = server.add_email_override(server.EmailOverrideRequest(pattern="Microsoft", email="a@b.com"))
        assert result == [{"pattern": "microsoft", "email": "a@b.com"}]  # pattern normalized lowercase
        assert server.list_email_overrides() == result


def test_add_replaces_existing_rule_for_same_pattern():
    with _isolated_output_dir():
        server.add_email_override(server.EmailOverrideRequest(pattern="microsoft", email="old@b.com"))
        result = server.add_email_override(server.EmailOverrideRequest(pattern="microsoft", email="new@b.com"))
        assert result == [{"pattern": "microsoft", "email": "new@b.com"}]


def test_add_rejects_blank_pattern_or_email():
    with _isolated_output_dir():
        try:
            server.add_email_override(server.EmailOverrideRequest(pattern="  ", email="a@b.com"))
            assert False, "expected HTTPException"
        except server.HTTPException as e:
            assert e.status_code == 400


def test_delete_removes_matching_rule():
    with _isolated_output_dir():
        server.add_email_override(server.EmailOverrideRequest(pattern="microsoft", email="a@b.com"))
        result = server.delete_email_override("microsoft")
        assert result == []


def test_delete_unknown_pattern_404s():
    with _isolated_output_dir():
        try:
            server.delete_email_override("not-configured")
            assert False, "expected HTTPException"
        except server.HTTPException as e:
            assert e.status_code == 404
