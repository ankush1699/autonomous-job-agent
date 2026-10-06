"""
core/red_flags.py — deterministic, zero-cost JD rejection patterns.

Single source of truth for visa/sponsorship/clearance red flags, shared by
scraper/filter.py and core/should_apply.py. A red-flag hit means the pipeline
never spends a single LLM token on the posting.
"""

import re

NO_SPONSORSHIP_PATTERNS = [
    # "will not" variants
    r"will not (provide|offer|sponsor|support) (visa |work )?sponsorship",
    # "do not / does not" variants — common on LinkedIn/Indeed job posts
    r"(do|does) not (provide|offer|sponsor|support) (visa |work )?sponsorship",
    r"(do|does) not sponsor (visas?|work visas?|immigration|h[\-\s]?1b)",
    r"we (do|does) not offer (visa )?sponsorship",
    # "not currently" variants
    r"not currently (able to |in a position to )?(provide |offer |support )?sponsorship",
    r"not currently sponsoring",
    # "cannot / unable" variants
    r"cannot sponsor",
    r"unable to sponsor",
    r"not able to sponsor",
    r"we are not able to sponsor",
    # "not available / not provided / not offered"
    r"(visa )?sponsorship (is )?not (available|provided|offered|supported)",
    r"no visa sponsorship",
    r"sponsorship is not (available|provided|offered)",
    # "not eligible"
    r"not eligible for (visa )?sponsorship",
    r"(this (role|position|job) is )?not eligible for (visa )?sponsorship",
    r"applicants (who )?require (visa )?sponsorship will not be considered",
    # Citizenship / authorization requirements
    r"must be (a )?u\.?s\.? (citizen|national|permanent resident)",
    r"must have (the )?right to work",
    r"only (u\.?s\.? )?(citizens|nationals|permanent residents)",
    r"u\.?s\.? citizens? (and|or) permanent residents? only",
    r"without (visa )?sponsorship (now or in the future)?",
    # H-1B explicit denials
    r"no h[\-\s]?1b",
    r"h[\-\s]?1b (sponsorship )?(is )?not (available|offered|provided)",
    # NOTE: plain "must be authorized to work [in the US]" was REMOVED from
    # here — it's near-universal EEO boilerplate on US job postings and does
    # NOT imply no sponsorship (F-1 OPT genuinely satisfies it; anyone with
    # any valid work authorization does). Real false positive this caused:
    # killed a Motion Recruitment posting for the single sentence "Applicants
    # must be authorized to work in the U.S. on a full-time basis now and in
    # the future" — no sponsorship denial anywhere in the JD. Genuine denials
    # phrased this way are still caught by the more specific patterns above/
    # below (e.g. "cannot sponsor", "without sponsorship", "sponsorship is
    # not available") — this bare phrase added false positives, not coverage.
    r"work authorization (is )?required (without|and we (cannot|will not) sponsor)",
    # ITAR / export control restrictions (require US citizenship or permanent residency)
    r"u\.?s\.? persons?",
    r"22 cfr",
    r"\bitar\b",
    r"\bear compliance\b",
    r"export control",
    r"restricted to (u\.?s\.? )?(citizens?|nationals?|persons?)",
    # Security clearance requirements (F-1 OPT holders are ineligible)
    r"(active|current|valid|existing|must (have|hold|obtain|possess)) (u\.?s\.? )?(security )?clearance",
    r"secret (clearance|security clearance)",
    r"top secret",
    r"ts/sci",
    r"ts\s*/\s*sci",
    r"dod (secret|clearance|security)",
    r"security clearance (is )?(required|mandatory|necessary)",
    r"(requires?|must have|must hold) (a |an )?(active |current |valid )?(u\.?s\.? )?security clearance",
    r"clearance (is )?(required|necessary|mandatory)",
    r"must be clearable",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in NO_SPONSORSHIP_PATTERNS]

# A handful of patterns describe a REQUIREMENT ("clearance required",
# "must have security clearance") that reads backwards under negation —
# "no security clearance required" is a positive signal (clearance NOT
# needed), not a red flag, but the bare pattern matches it anyway. Guard
# against that by checking a short window immediately before the match for a
# negation word. Python's `re` only supports fixed-width lookbehind, so this
# is done as a plain substring check rather than baked into every pattern.
_NEGATION_WINDOW_CHARS = 20
_NEGATION_WORDS = ("no ", "not ", "without ", "n't ", "never ")


def _is_negated(text: str, match_start: int) -> bool:
    window = text[max(0, match_start - _NEGATION_WINDOW_CHARS):match_start].lower()
    return any(neg in window for neg in _NEGATION_WORDS)


def find_red_flags(jd_text: str, max_hits: int = 3) -> list[str]:
    """Return up to max_hits matched snippets, or [] if the JD is clean."""
    hits = []
    for pattern in COMPILED_PATTERNS:
        m = pattern.search(jd_text)
        if m and not _is_negated(jd_text, m.start()):
            hits.append(m.group(0).strip())
            if len(hits) >= max_hits:
                break
    return hits


def has_red_flag(jd_text: str) -> bool:
    return bool(find_red_flags(jd_text, max_hits=1))
