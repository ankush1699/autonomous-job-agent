"""
scripts/resolve_linkedin_company_id.py

One-off CLI to look up a company's LinkedIn numeric organization id from its
PUBLIC company page — no login, no cookie, no bypassing anything. LinkedIn's
own page HTML embeds it as `urn:li:organization:<id>` in ordinary page
markup; this just fetches the page like a browser would and pulls that id
out with a regex. Used to seed/extend scraper/linkedin_companies.py.

Usage:
    python scripts/resolve_linkedin_company_id.py amazon microsoft google
    # -> prints "amazon -> 1586", "microsoft -> 1035", "google -> 1441"

The argument is the company's LinkedIn URL slug (the part after
linkedin.com/company/), not its display name — check the company's LinkedIn
page URL if unsure.
"""
import re
import sys

import requests

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_ID_RE = re.compile(r"urn:li:organization:(\d+)")


def resolve(slug: str) -> int | None:
    url = f"https://www.linkedin.com/company/{slug}/"
    resp = requests.get(url, headers=_UA, timeout=15)
    if resp.status_code != 200:
        print(f"  ({slug}: HTTP {resp.status_code})", file=sys.stderr)
        return None
    m = _ID_RE.search(resp.text)
    return int(m.group(1)) if m else None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for slug in sys.argv[1:]:
        cid = resolve(slug)
        print(f"{slug} -> {cid if cid is not None else 'NOT FOUND'}")
