"""
tests/test_sanitize_filename_component.py

engine/pdf_generator.sanitize_filename_component() is what makes resume
folder/file names include company AND job title safely — regression coverage
for the real problem it fixes (4 same-company applications indistinguishable
by filename) plus the filesystem-safety behavior it must preserve.

Run: pytest tests/test_sanitize_filename_component.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine")))

from pdf_generator import sanitize_filename_component


def test_strips_illegal_filesystem_characters():
    assert sanitize_filename_component('A/B\\C:D*E?F"G<H>I|J') == "ABCDEFGHIJ"


def test_slash_does_not_create_subdirectory_component():
    result = sanitize_filename_component("Senior/Staff Engineer")
    assert "/" not in result


def test_whitespace_collapsed_to_single_underscore():
    assert sanitize_filename_component("Software   Engineer") == "Software_Engineer"


def test_multiple_underscores_collapsed():
    assert sanitize_filename_component("A__B___C") == "A_B_C"


def test_leading_trailing_underscores_stripped():
    assert sanitize_filename_component("  Engineer  ") == "Engineer"


def test_empty_string_uses_fallback():
    assert sanitize_filename_component("", "Role") == "Role"


def test_none_uses_fallback():
    assert sanitize_filename_component(None, "Company") == "Company"


def test_illegal_chars_only_falls_back():
    assert sanitize_filename_component("///", "Role") == "Role"


def test_distinguishes_same_company_different_titles():
    a = sanitize_filename_component("Google") + "_" + sanitize_filename_component("Backend Engineer")
    b = sanitize_filename_component("Google") + "_" + sanitize_filename_component("Frontend Engineer")
    assert a != b
