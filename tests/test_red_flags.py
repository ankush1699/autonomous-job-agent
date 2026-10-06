"""
tests/test_red_flags.py

core/red_flags.py is the free, deterministic first stage of should_apply — a
false positive here silently kills a good-fit job at zero cost and zero
visibility, so this module is worth locking down with real tests.

Run: pytest tests/test_red_flags.py -v
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.red_flags import find_red_flags, has_red_flag


def test_explicit_no_sponsorship_flags():
    assert has_red_flag("We are unable to sponsor visas at this time.")
    assert has_red_flag("This role does not sponsor H-1B visas.")


def test_security_clearance_flags():
    assert has_red_flag("Requires an active TS/SCI clearance.")
    assert has_red_flag("Must hold a current DoD security clearance.")


def test_itar_flags_only_as_a_whole_word():
    """
    Regression test for a real bug: the ITAR pattern originally had no word
    boundary, so re.search(r"itar", ...) matched inside "military" and killed
    a real Microsoft posting with a false-positive red flag. Locked down here
    so it can't silently regress.
    """
    assert has_red_flag("This role is subject to ITAR restrictions.")
    assert not has_red_flag(
        "Equal opportunity employer without regard to age, ancestry, "
        "citizenship, color, family or medical care leave, gender identity, "
        "protected veteran or military status, race, ethnicity, religion."
    )


def test_sponsorship_friendly_jd_has_no_red_flags():
    assert not has_red_flag(
        "We welcome candidates on F-1 OPT and sponsor H-1B visas for "
        "exceptional candidates. No security clearance required."
    )


def test_negated_clearance_requirement_is_not_a_red_flag():
    """
    Regression test for a real bug found while writing this suite: "clearance
    (is )?(required|...)" matched inside "No security clearance required",
    which is actually a POSITIVE signal (clearance not needed), not a red
    flag. Fixed via a negation-window guard in find_red_flags().
    """
    assert not has_red_flag("No security clearance required for this role.")
    assert not has_red_flag("This position does not require security clearance.")
    # Sanity check the guard doesn't neuter genuine requirements:
    assert has_red_flag("Active security clearance required for this role.")


def test_neutral_jd_with_no_visa_mention_has_no_red_flags():
    assert not has_red_flag(
        "We are looking for a backend engineer with 3+ years of Python "
        "experience to join our platform team in Chicago."
    )


def test_find_red_flags_caps_at_max_hits():
    jd = ("Must be a US Citizen. " * 5) + "Requires active security clearance. " * 5
    hits = find_red_flags(jd, max_hits=2)
    assert len(hits) <= 2


def test_plain_work_authorization_boilerplate_is_not_a_red_flag():
    """
    Regression test for a real false positive: a Motion Recruitment posting
    was killed for the single sentence "Applicants must be authorized to
    work in the U.S. on a full-time basis now and in the future" — no
    sponsorship denial anywhere else in the JD. This is near-universal EEO
    boilerplate on US postings (F-1 OPT genuinely satisfies "authorized to
    work") and must NOT be treated as equivalent to "no sponsorship."
    """
    assert not has_red_flag(
        "Applicants must be authorized to work in the U.S. on a full-time "
        "basis now and in the future."
    )
    assert not has_red_flag("Must be legally authorized to work in the United States.")


def test_authorization_language_combined_with_real_denial_still_flags():
    """The bare phrase is no longer a signal on its own, but a genuine
    sponsorship denial in the same JD must still be caught by the
    more specific patterns."""
    assert has_red_flag(
        "Must be authorized to work in the U.S.; we are unable to sponsor "
        "employment visas of any kind."
    )
    assert has_red_flag(
        "Applicants must be authorized to work without sponsorship now or "
        "in the future."
    )
