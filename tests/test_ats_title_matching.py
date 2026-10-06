"""
tests/test_ats_title_matching.py

Company career pages (Greenhouse/Lever/Ashby/Workday) return every posting, so
the role keywords are matched against job TITLES in code. Before Oct 2026 this
was a plain substring match, so "Full Stack Engineer" missed "Full-Stack
Engineer" and "Fullstack Engineer" — very common spellings on startup boards.
Both sides are now canonicalised; the change may only ADD matches.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scraper.ats_scrape import _matches_keywords as m


def test_full_stack_spellings_all_match():
    for title in ["Full Stack Engineer", "Full-Stack Engineer", "Fullstack Engineer",
                  "Senior Full-Stack Engineer", "Full stack engineer (Remote)"]:
        assert m(title, ["Full Stack Engineer"]), title
    assert m("Full Stack Engineer", ["Fullstack Engineer"])      # either spelling as the keyword


def test_front_end_and_back_end_spellings_match():
    assert m("Front-End Engineer", ["Frontend Engineer"])
    assert m("Front End Engineer, Design Systems", ["Frontend Engineer"])
    assert m("Back-End Engineer", ["Backend Engineer"])
    assert m("Senior Back End Engineer", ["Backend Engineer"])


def test_everything_the_old_matcher_matched_still_matches():
    assert m("Senior Software Engineer", ["Software Engineer"])
    assert m("Software Engineer II, Payments", ["Software Engineer"])
    assert m("Applied AI Engineer", ["AI Engineer"])
    assert m("software engineer", ["SOFTWARE ENGINEER"])


def test_unrelated_titles_still_do_not_match():
    assert not m("Sales Engineer", ["Software Engineer"])
    assert not m("Data Engineer", ["AI Engineer", "Full Stack Engineer"])
    assert not m("Machine Learning Engineer", ["AI Engineer"])
    assert not m("Product Manager", ["Software Engineer", "Backend Engineer"])


def test_no_keywords_means_no_title_filter():
    assert m("Anything", [])
